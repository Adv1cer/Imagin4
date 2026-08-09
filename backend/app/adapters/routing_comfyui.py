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


class CompositeComfyUIClient:
    """`ComfyUIClient` implementation that dispatches to one of several underlying
    adapters based on the job's `kind` (== the workflow name resolved at admission
    time in app/api/v1/generations.py, e.g. "image_basic" or "poster_infographic")."""

    def __init__(
        self,
        comfyui_client: ComfyUIClient,
        gemini_client: ComfyUIClient | None,
        comfy_prompt_designer=None,
    ) -> None:
        self._comfyui = comfyui_client
        self._gemini = gemini_client
        # Optional async callable (prompt: str, exact_text: list[str]) -> str -- normally
        # GeminiTextClient.design_comfyui_prompt, wired from app/main.py. Best-effort
        # refinement of the ComfyUI-bound prompt before delegating to the underlying
        # adapter; see submit() below. None when Gemini isn't configured, in which case
        # the original prompt is used unmodified (same as before this existed).
        self._comfy_prompt_designer = comfy_prompt_designer
        # Tracks which underlying adapter owns each prompt_id so get_status/cancel can
        # be routed consistently without re-deriving the workflow's backend later.
        self._owner: dict[str, ComfyUIClient] = {}
        self._local_failures: dict[str, ComfyStatus] = {}

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
            # backend_name == "gemini" but no Gemini client configured.
            prompt_id = str(uuid.uuid4())
            self._local_failures[prompt_id] = ComfyStatus(
                prompt_id=prompt_id,
                state="failed",
                error="gemini_not_configured",
            )
            logger.warning(
                "routing_comfyui: kind=%s requires gemini backend but APP_GEMINI_API_KEY "
                "is unset; failing job instead of silently falling back to ComfyUI",
                kind,
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
        self._owner[result.prompt_id] = adapter
        logger.info(
            "routing_comfyui: kind=%s routed to backend=%s prompt_id=%s",
            kind,
            backend_name,
            result.prompt_id,
        )
        return result

    async def get_status(self, prompt_id: str) -> ComfyStatus:
        if prompt_id in self._local_failures:
            return self._local_failures[prompt_id]
        adapter = self._owner.get(prompt_id)
        if adapter is None:
            return ComfyStatus(prompt_id=prompt_id, state="failed", error="unknown_prompt_id")
        return await adapter.get_status(prompt_id)

    async def cancel(self, prompt_id: str) -> None:
        adapter = self._owner.get(prompt_id)
        if adapter is not None:
            await adapter.cancel(prompt_id)
        self._local_failures.pop(prompt_id, None)

    async def health(self) -> bool:
        # Cheap check: ComfyUI adapter is always present; Gemini's own health() is
        # already cheap (see GeminiImageComfyUIClient.health) and only checked if
        # configured.
        ok = await self._comfyui.health()
        if self._gemini is not None:
            ok = ok and await self._gemini.health()
        return ok
