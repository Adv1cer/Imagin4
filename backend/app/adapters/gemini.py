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
import json
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
    """Real chat-completion backend for POST /v1/conversations/{id}/assistant-reply, and
    (via route_intent) the semantic router for the agentic chat intent layer -- see
    app/domain/chat/routing.py and app/api/v1/chat_router.py. Both use the same
    underlying model/client on purpose: "use the existing conversational LLM as the
    semantic router" per project instructions, rather than standing up a second model."""

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

    def _route_sync(self, contents: list[dict], system_instruction: str) -> str:
        from google.genai import types

        from app.domain.chat.routing import ROUTE_DECISION_JSON_SCHEMA

        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=ROUTE_DECISION_JSON_SCHEMA,
            ),
        )
        return (getattr(response, "text", None) or "").strip()

    async def route_intent(
        self, history: list[dict[str, str]], extra_system_instruction: str | None = None
    ) -> dict:
        """Classifies the latest turn in `history` (same chronological shape as
        complete()) into a raw dict the caller must validate via
        app.domain.chat.routing.parse_route_decision -- this method does NOT validate,
        it only gets Gemini's structured-output response back as parsed JSON. Raises on
        any failure (unreachable API, malformed JSON, timeout); callers must fail safe
        (fall back to CLARIFICATION), never treat an exception here as permission to
        guess a tool -- see app/api/v1/chat_router.py.

        `extra_system_instruction`, when given, REPLACES the default
        ROUTER_SYSTEM_INSTRUCTION wholesale (callers pass the full instruction, e.g. via
        app.domain.chat.routing.build_router_system_instruction_with_research) rather
        than being appended here -- keeps this method dumb/pure plumbing and the actual
        policy text auditable in one place (routing.py)."""
        from app.domain.chat.routing import ROUTER_SYSTEM_INSTRUCTION

        contents = [
            {"role": _ROLE_TO_GEMINI[h["role"]], "parts": [{"text": h["text"]}]}
            for h in history
            if h["role"] in _ROLE_TO_GEMINI and h["text"].strip()
        ]
        if not contents:
            raise RuntimeError("gemini_error:EmptyHistory")
        system_instruction = extra_system_instruction or ROUTER_SYSTEM_INSTRUCTION
        try:
            raw_text = await asyncio.wait_for(
                asyncio.to_thread(self._route_sync, contents, system_instruction),
                timeout=self._timeout_s,
            )
            return json.loads(raw_text)
        except Exception as exc:
            logger.exception("gemini intent routing failed")
            raise RuntimeError(_sanitized_error(exc)) from exc

    def _research_sync(self, contents: list[dict]) -> str:
        from google.genai import types

        from app.domain.chat.routing import RESEARCH_SYSTEM_INSTRUCTION

        # NOTE: response_schema/response_mime_type are deliberately NOT set here --
        # Gemini's API rejects combining structured output with the google_search tool
        # (verified against the Gemini API docs/forum, not assumed). This call returns
        # free text; the caller re-classifies via a SEPARATE structured route_intent()
        # call using build_router_system_instruction_with_research().
        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=RESEARCH_SYSTEM_INSTRUCTION,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        return (getattr(response, "text", None) or "").strip()

    async def research_missing_fields(
        self, history: list[dict[str, str]], missing_fields: list[str]
    ) -> str:
        """Best-effort grounded web search for POSTER/INFOGRAPHIC missing_fields (e.g.
        an event date the user didn't give but is publicly announced). Raises on any
        failure -- callers MUST treat this as optional/non-fatal and fall back to asking
        the user normally (see app/api/v1/chat_router.py), never block or fail the whole
        request just because research didn't work. This never fills in facts on its
        own -- it only returns findings text; a second, separately-validated
        route_intent() call decides whether those findings actually resolve any
        missing_fields."""
        from app.domain.chat.routing import build_research_query

        contents = [
            {"role": _ROLE_TO_GEMINI[h["role"]], "parts": [{"text": h["text"]}]}
            for h in history
            if h["role"] in _ROLE_TO_GEMINI and h["text"].strip()
        ]
        contents.append(
            {
                "role": "user",
                "parts": [{"text": build_research_query(history, missing_fields)}],
            }
        )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._research_sync, contents), timeout=self._timeout_s
            )
        except Exception as exc:
            logger.warning("gemini research call failed: %s", type(exc).__name__)
            raise RuntimeError(_sanitized_error(exc)) from exc


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

    async def submit(self, workflow_payload: dict, kind: str | None = None) -> ComfySubmitResult:
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
