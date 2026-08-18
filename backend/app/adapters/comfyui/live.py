"""Live ComfyUI HTTP adapter: implements the same `ComfyUIClient` port as
`MockComfyUIClient` / `app.adapters.gemini.GeminiImageComfyUIClient`, but talks to a real
ComfyUI instance's HTTP API (POST /prompt, GET /history/{id}, GET /queue, GET /view,
POST /interrupt, POST /queue).

Security invariant (see project instructions / AGENTS-equivalent doc, "Client-supplied
arbitrary ComfyUI workflows are forbidden"): this adapter NEVER accepts a raw ComfyUI
graph from the caller. `submit()` only receives the already-validated `input_payload`
dict admitted by POST /v1/generations (prompt text + a small allowlisted set of
aspect_ratio/resolution/prompt_enhancer/model_profile keys -- see
app/api/v1/generations.py and frontend/src/types/imageGen.ts), and builds the actual
node graph itself from a fixed server-side template (`_build_prompt_graph`) using
settings for anything model-specific (checkpoint filename, sampler, steps, cfg). There
is no code path that forwards client-supplied JSON into the graph.

`model_profile` and `model_overrides` (2026-08-19, see app/domain/jobs/comfy_profiles.py
and app/domain/jobs/comfy_overrides.py) are the exceptions to "settings, not request
input" above, and both are narrow: `model_profile` selects a profile by NAME from a
small server-side allowlist (e.g. "student"/"personnel"); `model_overrides` layers
individual field overrides (checkpoint/clip/vae/sampler/scheduler/steps/cfg/negative
prompt -- never model_family, see comfy_overrides.py's docstring for why) on top of
that, each checked against a server-side allowlist/range. Both are already validated by
app.domain.jobs.admission.admit_generation_job before this adapter ever sees them --
this class re-validates defensively (see _resolve_active_profile) rather than trusting
that unconditionally, in case a caller ever reaches submit() without going through
admission (e.g. a direct/test caller). The end user still never sends a raw filename,
sampler name, or numeric step/cfg value that bypasses the allowlist/range either way.

ComfyUI API reference used here (no official Python SDK; this is the documented HTTP
surface -- see https://docs.comfy.org/):
  - POST /prompt              {"prompt": <graph>, "client_id": <str>} -> {"prompt_id": ...}
  - GET  /history/{prompt_id} -> {} while not yet finished, else {prompt_id: {...}}
  - GET  /queue                -> {"queue_running": [...], "queue_pending": [...]}
  - GET  /view?filename=&subfolder=&type= -> raw image bytes
  - POST /interrupt            -> interrupts whatever is CURRENTLY executing (no target
                                    id -- ComfyUI has no per-job "cancel this one if it's
                                    already running" API; see cancel()'s docstring)
  - POST /queue {"delete": [prompt_id]} -> removes a still-QUEUED (not yet running) item
  - GET  /system_stats         -> used as a cheap health check
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from app.adapters.comfyui import ComfyStatus, ComfySubmitResult
from app.adapters.storage import ObjectStorage
from app.domain.jobs.comfy_overrides import (
    InvalidComfyOverrideError,
    OverrideAllowlists,
    apply_overrides,
    validate_overrides,
)
from app.domain.jobs.comfy_profiles import DEFAULT_PROFILE_KEY, ComfyProfile

logger = logging.getLogger("imaginv.comfyui_live")

# SDXL-friendly base sizes (multiples of 64, ~1024 long edge) per common aspect ratio,
# scaled up for 2K/4K. Used when comfy_model_family == "checkpoint".
_BASE_DIMENSIONS_1K: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "9:16": (768, 1344),
    "2:3": (832, 1216),
    "3:4": (896, 1152),
    "4:5": (896, 1088),
    "5:4": (1088, 896),
    "4:3": (1152, 896),
    "3:2": (1216, 832),
    "16:9": (1344, 768),
    "21:9": (1536, 640),
}
# Qwen-Image's own recommended resolutions per aspect ratio, taken directly from the
# official workflow template (docs.comfy.org/tutorials/image/qwen/qwen-image, "Aspect
# Ratio Resolutions" table, 2026-08) rather than guessed -- Qwen-Image was trained at
# these specific sizes and departing from them can degrade quality/composition more than
# a typical SDXL checkpoint tolerates. Used when comfy_model_family == "qwen_image".
_QWEN_IMAGE_BASE_DIMENSIONS_1K: dict[str, tuple[int, int]] = {
    "1:1": (1328, 1328),
    "16:9": (1664, 928),
    "9:16": (928, 1664),
    "4:3": (1472, 1140),
    "3:4": (1140, 1472),
    "3:2": (1584, 1056),
    "2:3": (1056, 1584),
    # Not in Qwen-Image's official table -- fall back to the closest documented ratio's
    # proportions rather than omitting these entirely (frontend allows selecting them).
    "5:4": (1472, 1140),
    "4:5": (1140, 1472),
    "21:9": (1664, 704),
}
_RESOLUTION_SCALE = {"1K": 1, "2K": 2, "4K": 4}
_MAX_DIM = 4096


def _resolve_dimensions(
    aspect_ratio: str | None, resolution: str | None, family: str = "checkpoint"
) -> tuple[int, int]:
    table = _QWEN_IMAGE_BASE_DIMENSIONS_1K if family == "qwen_image" else _BASE_DIMENSIONS_1K
    base = table.get(aspect_ratio or "1:1", table["1:1"])
    scale = _RESOLUTION_SCALE.get(resolution or "1K", 1)
    width = min(base[0] * scale, _MAX_DIM)
    height = min(base[1] * scale, _MAX_DIM)
    # KSampler/VAE require multiples of 8.
    return (width // 8) * 8, (height // 8) * 8


def _sanitized_error(exc: Exception) -> str:
    """Mirrors app.adapters.gemini._sanitized_error: never echo raw exception text
    (which can include request/response fragments) back to clients or job_events."""
    return f"comfy_live_error:{type(exc).__name__}"


class LiveComfyUIClient:
    """`ComfyUIClient` implementation backed by a real ComfyUI HTTP server."""

    def __init__(
        self,
        base_url: str,
        storage: ObjectStorage,
        checkpoint_name: str = "",
        model_family: str = "checkpoint",
        diffusion_model_name: str = "",
        clip_name: str = "",
        vae_name: str = "",
        model_sampling_shift: float = 3.1,
        sampler_name: str = "euler",
        scheduler: str = "normal",
        steps: int = 20,
        cfg_scale: float = 7.0,
        negative_prompt: str = "",
        request_timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        profiles: dict[str, ComfyProfile] | None = None,
        override_allowlists: OverrideAllowlists | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._storage = storage
        # The scalar args above become the built-in "student" (default) fallback profile, used
        # whenever a submission's model_profile is missing or (defensively) not found
        # in `profiles` -- this is exactly the constructor shape every caller/test used
        # before profiles existed, so nothing that never heard of model_profile breaks.
        self._default_profile = ComfyProfile(
            key=DEFAULT_PROFILE_KEY,
            checkpoint_name=checkpoint_name,
            model_family=model_family,
            diffusion_model_name=diffusion_model_name,
            clip_name=clip_name,
            vae_name=vae_name,
            model_sampling_shift=model_sampling_shift,
            sampler_name=sampler_name,
            scheduler=scheduler,
            steps=steps,
            cfg_scale=cfg_scale,
            negative_prompt=negative_prompt,
        )
        # Additional named profiles (e.g. "personnel") a submission can select via
        # workflow_payload["model_profile"] -- see app/domain/jobs/comfy_profiles.py
        # and app/adapters/comfyui/factory.py, which builds this dict from Settings.
        self._profiles = dict(profiles or {})
        # Allowlist/range used to defensively re-validate workflow_payload["model_overrides"]
        # -- see this class's docstring and app/domain/jobs/comfy_overrides.py. None (the
        # default, e.g. every pre-2026-08-19 caller/test) means every *_CSV allowlist is
        # empty, which validate_overrides treats as "no field is overridable" -- so a
        # payload with model_overrides set is safely rejected/ignored, not silently
        # forwarded, if this client wasn't explicitly wired with allowlists.
        self._override_allowlists = override_allowlists
        self._timeout_s = request_timeout_s
        # Only ever set in tests, to substitute a fake HTTP transport instead of making
        # real network calls -- see tests/contract/test_live_comfyui_client.py.
        self._transport = transport
        self._client_id = f"imaginv-{uuid.uuid4().hex[:12]}"
        # Cache of fully-resolved terminal outcomes so repeated get_status() polls for an
        # already-succeeded/failed job don't re-fetch /history or re-download images.
        self._resolved: dict[str, ComfyStatus] = {}

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport)

    def _resolve_active_profile(self, workflow_payload: dict) -> ComfyProfile:
        """Picks which ComfyProfile this submission uses, then layers any
        `model_overrides` on top. `model_profile`/`model_overrides` have already been
        validated by admit_generation_job (see this class's docstring) -- this method
        re-validates defensively (e.g. for a direct/test caller that bypassed
        admission) and DROPS anything invalid rather than raising, matching this
        method's callers' "never fail on unexpected input" posture elsewhere in this
        file (e.g. _resolve_dimensions' own fallback). A dropped override silently
        falls back to the resolved profile's own value for that field -- never a raw
        pass-through of unvalidated input."""
        key = workflow_payload.get("model_profile")
        profile = self._profiles.get(key) if key else None
        if profile is None:
            profile = self._default_profile

        overrides = workflow_payload.get("model_overrides")
        if overrides and self._override_allowlists is not None:
            try:
                validated = validate_overrides(overrides, self._override_allowlists)
            except InvalidComfyOverrideError as exc:
                logger.warning(
                    "comfyui_live: dropping invalid model_overrides (%s) -- this should "
                    "have been caught by admission already", exc
                )
                validated = {}
            profile = apply_overrides(profile, validated)
        elif overrides:
            logger.warning(
                "comfyui_live: model_overrides present but this client has no "
                "override_allowlists configured -- ignoring"
            )
        return profile

    def _build_prompt_graph(self, workflow_payload: dict, filename_prefix: str) -> dict[str, Any]:
        """Builds a standard txt2img node graph from validated inputs only -- see module
        docstring. Unknown/extra keys in workflow_payload are ignored, not forwarded.
        Branches on the resolved profile's model_family since Qwen-Image's split-file
        architecture needs a structurally different graph than a single-file checkpoint
        (see class docstring and Settings.comfy_model_family).

        `filename_prefix` MUST be unique per submission (see submit()) -- every job used
        to share the literal prefix "imaginv", which let ComfyUI's own asset-browser
        group/collage same-prefix outputs into one composite thumbnail on at least one
        real deployment; a later /history lookup then resolved to that composite instead
        of this job's own single image. Root-caused 2026-08 against a live ComfyUI
        instance (confirmed: the returned file exactly matched what ComfyUI's "Media
        Assets" panel displayed for that shared-prefix group, not a bug in this file's
        fetch/store logic). A unique prefix per job removes the collision outright,
        regardless of the exact grouping mechanism on any given ComfyUI build/frontend."""
        profile = self._resolve_active_profile(workflow_payload)
        prompt_text = str(workflow_payload.get("prompt") or "").strip()
        width, height = _resolve_dimensions(
            workflow_payload.get("aspect_ratio"),
            workflow_payload.get("resolution"),
            family=profile.model_family,
        )
        seed = int(workflow_payload.get("seed") or uuid.uuid4().int % (2**32))

        if profile.model_family == "qwen_image":
            return self._build_qwen_image_graph(profile, prompt_text, width, height, seed, filename_prefix)
        return self._build_checkpoint_graph(profile, prompt_text, width, height, seed, filename_prefix)

    def _build_checkpoint_graph(
        self,
        profile: ComfyProfile,
        prompt_text: str,
        width: int,
        height: int,
        seed: int,
        filename_prefix: str,
    ) -> dict[str, Any]:
        """Single-file checkpoint (e.g. classic SDXL) via CheckpointLoaderSimple, which
        bundles MODEL+CLIP+VAE in one file/node."""
        return {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": profile.steps,
                    "cfg": profile.cfg_scale,
                    "sampler_name": profile.sampler_name,
                    "scheduler": profile.scheduler,
                    "denoise": 1.0,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
            },
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": profile.checkpoint_name},
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt_text, "clip": ["4", 1]},
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": profile.negative_prompt, "clip": ["4", 1]},
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]},
            },
        }

    def _build_qwen_image_graph(
        self,
        profile: ComfyProfile,
        prompt_text: str,
        width: int,
        height: int,
        seed: int,
        filename_prefix: str,
    ) -> dict[str, Any]:
        """Qwen-Image's split-file architecture: separate diffusion model (UNETLoader),
        text encoder (CLIPLoader with type="qwen_image"), and VAE (VAELoader), plus a
        required ModelSamplingAuraFlow node between the loaded model and KSampler.

        Graph structure verified node-for-node against the official Comfy-Org workflow
        template (https://docs.comfy.org/tutorials/image/qwen/qwen-image, fetched
        2026-08 -- not guessed): UNETLoader -> ModelSamplingAuraFlow -> KSampler, with
        CLIPLoader feeding both CLIPTextEncode nodes and EmptySD3LatentImage (not the
        generic EmptyLatentImage) providing the initial latent.
        """
        return {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": profile.steps,
                    "cfg": profile.cfg_scale,
                    "sampler_name": profile.sampler_name,
                    "scheduler": profile.scheduler,
                    "denoise": 1.0,
                    "model": ["66", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["58", 0],
                },
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt_text, "clip": ["38", 0]},
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": profile.negative_prompt, "clip": ["38", 0]},
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["3", 0], "vae": ["39", 0]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]},
            },
            "37": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": profile.diffusion_model_name, "weight_dtype": "default"},
            },
            "38": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": profile.clip_name,
                    "type": "qwen_image",
                    "device": "default",
                },
            },
            "39": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": profile.vae_name},
            },
            "58": {
                "class_type": "EmptySD3LatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
            "66": {
                "class_type": "ModelSamplingAuraFlow",
                "inputs": {"model": ["37", 0], "shift": profile.model_sampling_shift},
            },
        }

    async def submit(self, workflow_payload: dict, kind: str | None = None) -> ComfySubmitResult:
        # Unique per submission -- see _build_prompt_graph's docstring for why every job
        # sharing the literal "imaginv" prefix was a real bug, not just noise. Generated
        # here (rather than reusing ComfyUI's own prompt_id, which doesn't exist yet at
        # graph-build time) so it's guaranteed unique before the graph is even built.
        filename_prefix = f"imaginv-{uuid.uuid4().hex[:12]}"
        graph = self._build_prompt_graph(workflow_payload, filename_prefix)
        prompt_text = str(workflow_payload.get("prompt") or "").strip()
        if not prompt_text:
            prompt_id = str(uuid.uuid4())
            self._resolved[prompt_id] = ComfyStatus(
                prompt_id=prompt_id, state="failed", error="empty_prompt"
            )
            return ComfySubmitResult(prompt_id=prompt_id)

        try:
            async with self._http_client() as client:
                resp = await client.post(
                    f"{self._base_url}/prompt",
                    json={"prompt": graph, "client_id": self._client_id},
                )
                resp.raise_for_status()
                data = resp.json()
            prompt_id = str(data["prompt_id"])
            logger.info(
                "comfyui_live: submitted prompt_id=%s kind=%s base_url=%s prompt=%r",
                prompt_id,
                kind,
                self._base_url,
                prompt_text[:300],
            )
            return ComfySubmitResult(prompt_id=prompt_id)
        except Exception as exc:
            # Submission itself failed (ComfyUI unreachable, graph rejected, etc.) --
            # ComfyUI never even queued this, so there's no real prompt_id. Mint a local
            # one purely so the rest of the pipeline (which is keyed on prompt_id) has
            # something to look up; get_status() below serves the cached failure for it.
            logger.exception("comfyui_live: submit failed")
            prompt_id = str(uuid.uuid4())
            self._resolved[prompt_id] = ComfyStatus(
                prompt_id=prompt_id, state="failed", error=_sanitized_error(exc)
            )
            return ComfySubmitResult(prompt_id=prompt_id)

    async def _fetch_and_store_outputs(self, history_entry: dict) -> list[dict]:
        outputs: list[dict] = []
        node_outputs = history_entry.get("outputs") or {}
        async with self._http_client() as client:
            for node in node_outputs.values():
                for image in node.get("images") or []:
                    filename = image.get("filename")
                    if not filename:
                        continue
                    params = {
                        "filename": filename,
                        "subfolder": image.get("subfolder", ""),
                        "type": image.get("type", "output"),
                    }
                    resp = await client.get(f"{self._base_url}/view", params=params)
                    resp.raise_for_status()
                    data = resp.content
                    ext = (filename.rsplit(".", 1)[-1] or "png").lower()
                    mime = f"image/{'jpeg' if ext == 'jpg' else ext}"
                    object_key = f"generated/{uuid.uuid4().hex}.{ext}"
                    await self._storage.put_object(object_key, data, mime)
                    outputs.append({"object_key": object_key, "mime_type": mime})
        return outputs

    async def get_status(self, prompt_id: str) -> ComfyStatus:
        cached = self._resolved.get(prompt_id)
        if cached is not None:
            return cached

        try:
            async with self._http_client() as client:
                history_resp = await client.get(f"{self._base_url}/history/{prompt_id}")
                history_resp.raise_for_status()
                history = history_resp.json()

                if prompt_id in history:
                    entry = history[prompt_id]
                    status_info = entry.get("status") or {}
                    if status_info.get("status_str") == "error":
                        # ComfyUI's own /history response carries the real node-level
                        # exception under status.messages (e.g. OOM, missing model file,
                        # a bad custom-node) -- log it server-side (never sanitized/
                        # surfaced to the customer, who only ever sees the fixed
                        # "comfy_execution_error" code below) so a failure like this is
                        # diagnosable straight from `docker compose logs api` instead of
                        # needing to separately dig through the ComfyUI worker's own
                        # container logs after the fact.
                        logger.warning(
                            "comfyui_live: execution error prompt_id=%s base_url=%s messages=%s",
                            prompt_id,
                            self._base_url,
                            status_info.get("messages"),
                        )
                        status = ComfyStatus(
                            prompt_id=prompt_id, state="failed", error="comfy_execution_error"
                        )
                        self._resolved[prompt_id] = status
                        return status
                    outputs = await self._fetch_and_store_outputs(entry)
                    if not outputs:
                        status = ComfyStatus(
                            prompt_id=prompt_id, state="failed", error="comfy_no_output_image"
                        )
                    else:
                        status = ComfyStatus(prompt_id=prompt_id, state="succeeded", outputs=outputs)
                    self._resolved[prompt_id] = status
                    return status

                # Not in history yet: check the queue to distinguish "still working" from
                # "ComfyUI has no idea what this is" (e.g. it restarted and lost state).
                queue_resp = await client.get(f"{self._base_url}/queue")
                queue_resp.raise_for_status()
                queue = queue_resp.json()
                all_queued_ids = {
                    entry[1]
                    for entry in (queue.get("queue_running") or []) + (queue.get("queue_pending") or [])
                }
                if prompt_id in all_queued_ids:
                    return ComfyStatus(prompt_id=prompt_id, state="running")
                # Unknown to both history and queue -- treat as a transient miss (e.g. a
                # request landed between submit() returning and ComfyUI registering the
                # job) rather than a hard failure; the reconciler will poll again and,
                # if this persists past the job's lease, eventually time it out via
                # worker_lease_expired rather than us guessing wrong here.
                return ComfyStatus(prompt_id=prompt_id, state="running")
        except Exception as exc:
            logger.exception("comfyui_live: get_status failed prompt_id=%s", prompt_id)
            # A transport/HTTP failure while polling is treated as still-running rather
            # than failed -- ComfyUI may be momentarily unreachable (network blip) while
            # the job itself is fine; the reconciler's lease-expiry path is what should
            # ultimately fail a job that never recovers, not a single flaky poll.
            logger.warning(
                "comfyui_live: treating poll failure as still-running (%s)", _sanitized_error(exc)
            )
            return ComfyStatus(prompt_id=prompt_id, state="running")

    async def cancel(self, prompt_id: str) -> None:
        """Best-effort cancellation. ComfyUI's HTTP API distinguishes "still queued"
        (removable via POST /queue {"delete": [id]}) from "currently executing" (only
        POST /interrupt, which has no target id and interrupts whatever ComfyUI happens
        to be running right now -- there is no API to interrupt a *specific* prompt_id).
        So: if it's still pending, this cancels precisely; if it's already running, this
        may interrupt a *different* job if this ComfyUI instance is shared/concurrent.
        Per project instructions: document the limitation rather than pretend precision
        we don't have.
        """
        try:
            async with self._http_client() as client:
                queue_resp = await client.get(f"{self._base_url}/queue")
                queue_resp.raise_for_status()
                queue = queue_resp.json()
                pending_ids = {entry[1] for entry in (queue.get("queue_pending") or [])}
                running_ids = {entry[1] for entry in (queue.get("queue_running") or [])}

                if prompt_id in pending_ids:
                    await client.post(f"{self._base_url}/queue", json={"delete": [prompt_id]})
                elif prompt_id in running_ids:
                    logger.warning(
                        "comfyui_live: cancel(%s) is currently RUNNING -- ComfyUI has no "
                        "per-prompt interrupt, so POST /interrupt will stop whatever this "
                        "instance is executing right now, which may not be this job if "
                        "another one started concurrently",
                        prompt_id,
                    )
                    await client.post(f"{self._base_url}/interrupt")
        except Exception:
            logger.exception("comfyui_live: cancel failed prompt_id=%s", prompt_id)
        self._resolved.pop(prompt_id, None)

    async def health(self) -> bool:
        try:
            async with self._http_client() as client:
                resp = await client.get(f"{self._base_url}/system_stats")
                return resp.status_code == 200
        except Exception:
            return False
