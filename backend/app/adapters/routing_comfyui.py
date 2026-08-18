"""Routes each generation job to the correct backend (ComfyUI vs Gemini) based on its
workflow's `backend` field in the server-side allowlist (app/domain/jobs/workflow_registry.py),
while presenting a single `ComfyUIClient` to the scheduler/reconciler -- neither of them
need to know more than one backend exists.

Per explicit product decision: "Image" generation (workflow `image_basic`, plus the
legacy `txt2img_basic`/`img2img_basic`) goes to ComfyUI (mock or live); "Poster /
Infographic" generation (workflow `poster_infographic`) always goes to Gemini, because
Gemini is significantly better at in-image text/layout than a typical SDXL ComfyUI
workflow. This is a routing decision, not a fallback -- if Gemini isn't configured, a
poster request fails clearly rather than silently degrading to ComfyUI (which would
produce a poster-shaped image with garbled or missing text).
"""

from __future__ import annotations

import logging
import uuid

from app.adapters.comfyui import ComfyStatus, ComfySubmitResult, ComfyUIClient
from app.domain.jobs.workflow_registry import resolve_workflow

logger = logging.getLogger("imaginv.routing_comfyui")

_SEP = ":"
_NOT_CONFIGURED_TAG = "notconfigured"
_COMFYUI_TAG = "comfyui"
_GEMINI_TAG = "gemini"


class CompositeComfyUIClient:
    """`ComfyUIClient` implementation that dispatches to one of several underlying
    adapters based on the job's `kind` (== the workflow name resolved at admission
    time in app/api/v1/generations.py, e.g. "image_basic" or "poster_infographic")."""

    def __init__(
        self,
        comfyui_client: ComfyUIClient,
        gemini_client: ComfyUIClient | None,
        comfy_prompt_designer=None,
        image_provider_name: str = "gemini",
    ) -> None:
        self._comfyui = comfyui_client
        self._gemini = gemini_client
        # Name of whichever provider `gemini_client` actually holds -- "gemini" (default,
        # preserves prior behavior/error strings) or "openrouter" (see
        # Settings.image_provider, app/core/config.py). Only used to phrase the
        # not-configured failure below correctly; routing logic itself is unchanged
        # (still keyed on workflow.backend == "gemini", an internal workflow-registry
        # label unrelated to which real provider is wired).
        self._image_provider_name = image_provider_name
        # Optional async callable (prompt: str, exact_text: list[str]) -> str -- normally
        # GeminiTextClient.design_comfyui_prompt, wired from app/main.py. Best-effort
        # refinement of the ComfyUI-bound prompt before delegating to the underlying
        # adapter; see submit() below. None when Gemini isn't configured, in which case
        # the original prompt is used unmodified (same as before this existed).
        self._comfy_prompt_designer = comfy_prompt_designer

    def _resolve_backend(self, kind: str | None) -> tuple[ComfyUIClient | None, str]:
        """Returns (adapter_or_None, backend_name). adapter is None only when the
        resolved backend is "gemini" but no Gemini client is configured."""
        if kind is None:
            return self._comfyui, "comfyui"
        try:
            # kind is the workflow *name*; version isn't carried on QueuedJob, but the
            # registry only has one version per name today, so try "v1" then fall back
            # to scanning for any matching name if that ever changes.
            workflow = resolve_workflow(kind, "v1")
        except Exception:
            return self._comfyui, "comfyui"

        if workflow.backend == "gemini":
            return self._gemini, "gemini"
        return self._comfyui, "comfyui"

    async def submit(self, workflow_payload: dict, kind: str | None = None) -> ComfySubmitResult:
        adapter, backend_name = self._resolve_backend(kind)

        if adapter is None:
            # backend_name == "gemini" (the workflow-registry label) but no image-
            # generation client is actually configured -- error string names whichever
            # real provider was supposed to be wired (see _image_provider_name above),
            # not always literally "gemini", so the failure reads correctly under
            # Settings.image_provider="openrouter" too.
            #
            # The reason is encoded directly in the prompt_id (not a self._local_failures
            # dict) so get_status() can decode it statelessly -- under
            # queue_backend=postgres, submit() and get_status() run in separate OS
            # processes (scheduler dispatches, reconciler polls), each with its own
            # CompositeComfyUIClient instance; a dict populated here would be invisible
            # to a get_status() call from the other process. See module-level _SEP/
            # _NOT_CONFIGURED_TAG and this class's get_status() for the decode side, and
            # MultiWorkerComfyUIClient (app/adapters/comfyui/multi_worker.py) for the
            # identically-shaped fix one layer down.
            prompt_id = (
                f"{_NOT_CONFIGURED_TAG}{_SEP}{self._image_provider_name}{_SEP}{uuid.uuid4().hex}"
            )
            logger.warning(
                "routing_comfyui: kind=%s requires the %s image backend but it is not "
                "configured; failing job instead of silently falling back to ComfyUI",
                kind,
                self._image_provider_name,
            )
            return ComfySubmitResult(prompt_id=prompt_id)

        payload = workflow_payload
        if backend_name == "comfyui" and self._comfy_prompt_designer is not None:
            base_prompt = str(workflow_payload.get("prompt") or "").strip()
            exact_text = [
                t.strip()
                for t in (workflow_payload.get("exact_text") or [])
                if isinstance(t, str) and t.strip()
            ]
            if base_prompt or exact_text:
                try:
                    designed = await self._comfy_prompt_designer(base_prompt, exact_text)
                    if designed and designed.strip():
                        payload = {**workflow_payload, "prompt": designed.strip()}
                except Exception as exc:
                    logger.info(
                        "routing_comfyui: prompt-design step failed (%s), using original "
                        "prompt kind=%s",
                        type(exc).__name__,
                        kind,
                    )

        result = await adapter.submit(payload, kind=kind)
        composite_id = f"{backend_name}{_SEP}{result.prompt_id}"
        logger.info(
            "routing_comfyui: kind=%s routed to backend=%s prompt_id=%s",
            kind,
            backend_name,
            composite_id,
        )
        return ComfySubmitResult(prompt_id=composite_id)

    def _decode(self, prompt_id: str) -> tuple[ComfyUIClient | None, str, str | None]:
        """Returns (adapter_or_None, tag, real_id_or_None). See submit()'s comment on why
        this is decoded from the prompt_id itself rather than looked up in a dict."""
        tag, sep, rest = prompt_id.partition(_SEP)
        if not sep:
            return None, tag, None
        if tag == _NOT_CONFIGURED_TAG:
            return None, tag, rest  # rest here is "<provider_name>:<uuid>", not a real id
        if tag == _COMFYUI_TAG:
            return self._comfyui, tag, rest
        if tag == _GEMINI_TAG:
            return self._gemini, tag, rest
        return None, tag, None

    async def get_status(self, prompt_id: str) -> ComfyStatus:
        adapter, tag, rest = self._decode(prompt_id)
        if tag == _NOT_CONFIGURED_TAG:
            provider_name = (rest or "").split(_SEP, 1)[0] or self._image_provider_name
            return ComfyStatus(
                prompt_id=prompt_id, state="failed", error=f"{provider_name}_not_configured"
            )
        if adapter is None or rest is None:
            return ComfyStatus(prompt_id=prompt_id, state="failed", error="unknown_prompt_id")
        status = await adapter.get_status(rest)
        return ComfyStatus(
            prompt_id=prompt_id, state=status.state, outputs=status.outputs, error=status.error
        )

    async def cancel(self, prompt_id: str) -> None:
        adapter, _tag, rest = self._decode(prompt_id)
        if adapter is not None and rest is not None:
            await adapter.cancel(rest)

    async def health(self) -> bool:
        # Cheap check: ComfyUI adapter is always present; Gemini's own health() is
        # already cheap (see GeminiImageComfyUIClient.health) and only checked if
        # configured.
        ok = await self._comfyui.health()
        if self._gemini is not None:
            ok = ok and await self._gemini.health()
        return ok
