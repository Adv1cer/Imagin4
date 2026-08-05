"""Google Gemini (AI Studio) adapters:

- `GeminiTextClient` powers real chat replies (POST /v1/conversations/{id}/assistant-reply).
- `GeminiImageComfyUIClient` implements the same `ComfyUIClient` port as
  `app.adapters.comfyui.MockComfyUIClient` / a real ComfyUI adapter would, so it drops
  into the existing async job pipeline (scheduler claims the job, calls `submit()`,
  reconciler calls `get_status()`) without changing any queue/scheduler/reconciler code.

Both use the `google-genai` SDK, which is synchronous, so calls are offloaded via
`asyncio.to_thread` to avoid blocking the event loop -- this is the same pattern a real
network-bound ComfyUI HTTP client would use if it were sync.

Model names (see Settings.gemini_text_model / gemini_image_model, app/core/config.py)
and free-tier quotas both drift over time -- Google retired gemini-2.0-flash and then
gemini-2.5-flash for new API keys/projects while this adapter was being built (confirmed
via live 404 "no longer available" responses, not assumed). As of 2026-08 the current
generation is 3.x: gemini-3.6-flash for text, gemini-3.1-flash-image ("Nano Banana 2")
for images -- see https://ai.google.dev/gemini-api/docs/models for the live list. If
generation starts failing with a 404, that's a model-name-rotation issue to fix via env
var, not a bug here. If it fails with 429 RESOURCE_EXHAUSTED, that's a genuine rate/quota
limit -- both surface as a normal job `failed` state / 502 on the chat endpoint, not a
crash.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.adapters.comfyui import ComfyStatus, ComfySubmitResult
from app.adapters.storage import ObjectStorage

logger = logging.getLogger("imaginv.gemini")

# Chat roles the frontend/DB actually use. Gemini only understands "user" and "model";
# "assistant" is our synonym for "model" and "system"/"tool" turns are dropped (Gemini's
# system_instruction is a separate config field, not a content-history entry, and we
# don't have any interactive tool-use flow generating "tool" messages yet).
_ROLE_TO_GEMINI = {"user": "user", "assistant": "model"}


def _sanitized_error(exc: Exception) -> str:
    """Never echo raw exception text (which can include request payload fragments)
    back to clients or into job_events; keep only the exception class name."""
    return f"gemini_error:{type(exc).__name__}"


class GeminiTextClient:
    """Real chat-completion backend for POST /v1/conversations/{id}/assistant-reply."""

    def __init__(self, api_key: str, model: str, timeout_s: float = 30.0) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._timeout_s = timeout_s

    def _generate_sync(self, contents: list[dict]) -> str:
        response = self._client.models.generate_content(model=self._model, contents=contents)
        return (getattr(response, "text", None) or "").strip()

    async def complete(self, history: list[dict[str, str]]) -> str:
        """`history` is chronological [{"role": "user"|"assistant", "text": "..."}, ...].
        Raises on failure -- callers turn that into a sanitized 503, they don't get a
        raw exception message back to the client."""
        contents = [
            {"role": _ROLE_TO_GEMINI[h["role"]], "parts": [{"text": h["text"]}]}
            for h in history
            if h["role"] in _ROLE_TO_GEMINI and h["text"].strip()
        ]
        if not contents:
            return "(nothing to reply to)"
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(self._generate_sync, contents), timeout=self._timeout_s
            )
        except Exception as exc:
            logger.exception("gemini text completion failed")
            raise RuntimeError(_sanitized_error(exc)) from exc
        return text or "(empty response)"


class GeminiImageComfyUIClient:
    """Drop-in replacement for MockComfyUIClient/a real ComfyUI adapter: implements the
    `ComfyUIClient` port but generates the image via Gemini instead of ComfyUI.

    Unlike ComfyUI (submit -> poll prompt_id -> fetch output over multiple round trips),
    Gemini's image API is a single synchronous call, so `submit()` does the entire
    generate-and-store-to-object-storage sequence and caches the terminal `ComfyStatus`;
    `get_status()` just looks it up. This keeps the scheduler/reconciler dispatch loop
    (app/services/scheduler.py, app/services/reconciler.py) completely unaware that
    there's no real multi-step ComfyUI protocol underneath.
    """

    def __init__(
        self, api_key: str, model: str, storage: ObjectStorage, timeout_s: float = 30.0
    ) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._storage = storage
        self._timeout_s = timeout_s
        self._results: dict[str, ComfyStatus] = {}

    def _generate_sync(self, prompt_text: str) -> tuple[str, bytes]:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt_text,
            config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
        )
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            parts = getattr(getattr(candidate, "content", None), "parts", None) or []
            for part in parts:
                inline = getattr(part, "inline_data", None)
                if inline is not None and getattr(inline, "data", None):
                    mime = inline.mime_type or "image/png"
                    return mime, inline.data
        raise RuntimeError("gemini_no_image_in_response")

    async def submit(self, workflow_payload: dict) -> ComfySubmitResult:
        prompt_id = str(uuid.uuid4())
        prompt_text = str(workflow_payload.get("prompt") or "").strip()

        if not prompt_text:
            self._results[prompt_id] = ComfyStatus(
                prompt_id=prompt_id, state="failed", error="empty_prompt"
            )
            return ComfySubmitResult(prompt_id=prompt_id)

        try:
            mime, data = await asyncio.wait_for(
                asyncio.to_thread(self._generate_sync, prompt_text), timeout=self._timeout_s
            )
            ext = (mime.split("/")[-1] or "png").split("+")[0]
            object_key = f"generated/{prompt_id}.{ext}"
            await self._storage.put_object(object_key, data, mime)
            self._results[prompt_id] = ComfyStatus(
                prompt_id=prompt_id,
                state="succeeded",
                outputs=[{"object_key": object_key, "mime_type": mime}],
            )
            logger.info("gemini: image generated prompt_id=%s object_key=%s", prompt_id, object_key)
        except Exception as exc:
            logger.exception("gemini image generation failed prompt_id=%s", prompt_id)
            self._results[prompt_id] = ComfyStatus(
                prompt_id=prompt_id, state="failed", error=_sanitized_error(exc)
            )

        return ComfySubmitResult(prompt_id=prompt_id)

    async def get_status(self, prompt_id: str) -> ComfyStatus:
        cached = self._results.get(prompt_id)
        if cached is not None:
            return cached
        # submit() always populates a result (success or failure) before returning, so
        # reaching here means an unknown prompt_id -- treat it as failed rather than
        # hanging the reconciler in a "running" loop forever.
        return ComfyStatus(prompt_id=prompt_id, state="failed", error="unknown_prompt_id")

    async def cancel(self, prompt_id: str) -> None:
        # Gemini's call has already completed synchronously by the time any client
        # could observe the job as cancellable, so there's nothing in-flight to cancel.
        self._results.pop(prompt_id, None)

    async def health(self) -> bool:
        # Deliberately does not call the Gemini API (readiness checks need to be cheap
        # and shouldn't burn free-tier request quota) -- just confirms the client was
        # constructed with a key, which app/main.py only does when one is configured.
        return True
