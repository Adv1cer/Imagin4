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
    back to clients or into job_events; keep only the exception class name -- EXCEPT for
    a couple of specific, high-value cases below, where we surface a distinct, safe
    (not raw-text) code so the customer-facing message doesn't read as "our system is
    broken" when it's actually Google's API temporarily refusing the request.

    google-genai's APIError (parent of ServerError/ClientError) exposes a structured
    `.code` (HTTP status int) and `.status` (Google's string enum, e.g. "UNAVAILABLE")
    -- see google.genai.errors.APIError. These are controlled, non-arbitrary fields
    (not the raw exception message/response body), so surfacing them is safe under the
    same "no raw exception text" rule the generic fallback below follows.

    Reported 2026-08 (Chet): a poster job failed with the generic `gemini_error:
    ServerError`, which reads to a customer like a bug in OUR system, when the actual
    cause was `503 UNAVAILABLE: This model is currently experiencing high demand` --
    Google's own image model being temporarily overloaded, nothing wrong on our end."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if code == 503 or status == "UNAVAILABLE":
        return "gemini_overloaded"
    if code == 429 or status == "RESOURCE_EXHAUSTED":
        return "gemini_rate_limited"
    return f"gemini_error:{type(exc).__name__}"


def _exact_text_block(exact_text: list[str]) -> str:
    """The verbatim-rendering instruction block appended to whatever base prompt is
    used, whether that's the plain normalized_prompt (_build_image_prompt) or a
    designed prompt from GeminiTextClient.design_image_prompt. Deliberately applied
    BOTH places (belt-and-suspenders): the design step's own system instruction already
    asks it to preserve exact_text, but appending this explicit block again afterward
    guarantees the literal text survives even if that instruction-following slips --
    exactly the class of bug this function exists to prevent from recurring."""
    if not exact_text:
        return ""
    exact_lines = "\n".join(f"- {t}" for t in exact_text)
    return (
        "\n\nThe generated image MUST render the following text exactly as written, "
        "verbatim -- do not translate, paraphrase, omit, or alter it in any way:\n"
        f"{exact_lines}"
    )


def _build_image_prompt(workflow_payload: dict) -> str:
    """Builds the actual text sent to Gemini's image model from a job's input_payload.

    BUG FIXED HERE: this previously only read workflow_payload["prompt"] and silently
    dropped "exact_text" entirely -- so a POSTER/INFOGRAPHIC's literal copy (campaign
    name, offer details, dates, contact info -- exactly the fields the chat router's
    RouteDecision.exact_text and the research step exist to get right, see
    app/domain/chat/routing.py) never actually reached the image generation call. The
    model then had nothing to go on but a generic normalized_prompt and produced a
    generic, factually-wrong poster even when routing correctly extracted the real
    details. Pure function (no I/O) so this is unit-testable without hitting the SDK."""
    prompt = str(workflow_payload.get("prompt") or "").strip()
    exact_text = [
        t.strip() for t in (workflow_payload.get("exact_text") or []) if isinstance(t, str) and t.strip()
    ]
    return prompt + _exact_text_block(exact_text)


class GeminiTextClient:
    """Real chat-completion backend for POST /v1/conversations/{id}/assistant-reply, and
    (via route_intent) the semantic router for the agentic chat intent layer -- see
    app/domain/chat/routing.py and app/api/v1/chat_router.py. Both use the same
    underlying model/client on purpose: "use the existing conversational LLM as the
    semantic router" per project instructions, rather than standing up a second model."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_s: float = 30.0,
        research_timeout_s: float = 20.0,
    ) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._timeout_s = timeout_s
        # Separate, tighter budget for the best-effort grounded-search research call --
        # see Settings.gemini_research_timeout_s for why this is its own knob.
        self._research_timeout_s = research_timeout_s

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
                asyncio.to_thread(self._research_sync, contents),
                timeout=self._research_timeout_s,
            )
        except Exception as exc:
            logger.warning("gemini research call failed: %s", type(exc).__name__)
            raise RuntimeError(_sanitized_error(exc)) from exc

    def _design_prompt_sync(self, contents: list[dict], kind: str) -> str:
        from google.genai import types

        from app.domain.chat.routing import build_prompt_design_instruction

        # Plain text, no schema, no tools -- fast, ordinary generate_content call.
        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=build_prompt_design_instruction(kind)
            ),
        )
        return (getattr(response, "text", None) or "").strip()

    async def design_image_prompt(
        self, prompt: str, exact_text: list[str], kind: str = "poster"
    ) -> str:
        """Best-effort: have the text model act as a prompt engineer and write a
        detailed, well-composed image-generation prompt (layout, color, typography
        direction) before the actual image call, instead of sending the router's short
        normalized_prompt straight through. See
        app.adapters.gemini.GeminiImageComfyUIClient.submit(), which calls this and
        falls back to the plain _build_image_prompt() on any failure -- this step is
        purely a quality enhancement, never a hard dependency, and never a source of new
        facts (the system instruction explicitly forbids inventing content)."""
        from app.domain.chat.routing import build_prompt_design_user_message

        contents = [
            {
                "role": "user",
                "parts": [{"text": build_prompt_design_user_message(prompt, exact_text)}],
            }
        ]
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._design_prompt_sync, contents, kind),
                timeout=self._timeout_s,
            )
        except Exception as exc:
            logger.warning("gemini prompt-design call failed: %s", type(exc).__name__)
            raise RuntimeError(_sanitized_error(exc)) from exc

    def _design_comfy_prompt_sync(self, contents: list[dict]) -> str:
        from google.genai import types

        from app.domain.chat.routing import COMFY_PROMPT_DESIGN_SYSTEM_INSTRUCTION

        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=COMFY_PROMPT_DESIGN_SYSTEM_INSTRUCTION
            ),
        )
        return (getattr(response, "text", None) or "").strip()

    async def design_comfyui_prompt(self, prompt: str, exact_text: list[str]) -> str:
        """Best-effort refinement for the ORDINARY image path (GENERAL_IMAGE, ComfyUI
        backend) -- the equivalent of design_image_prompt above but with a distinct
        instruction tailored to diffusion-model prompt style (dense descriptive
        keywords, not narrative prose; no verbatim-text-rendering instruction, since
        ComfyUI/SDXL-style models can't reliably render in-image text -- see
        app.domain.chat.routing.COMFY_PROMPT_DESIGN_SYSTEM_INSTRUCTION). Called from
        app.adapters.routing_comfyui.CompositeComfyUIClient.submit() before delegating
        to the ComfyUI adapter; callers MUST treat this as optional and fall back to the
        original prompt on any failure, same fail-safe pattern as every other
        best-effort Gemini step in this module."""
        from app.domain.chat.routing import build_comfy_prompt_design_user_message

        contents = [
            {
                "role": "user",
                "parts": [{"text": build_comfy_prompt_design_user_message(prompt, exact_text)}],
            }
        ]
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._design_comfy_prompt_sync, contents),
                timeout=self._timeout_s,
            )
        except Exception as exc:
            logger.warning("gemini comfy prompt-design call failed: %s", type(exc).__name__)
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
        self,
        api_key: str,
        model: str,
        storage: ObjectStorage,
        timeout_s: float = 30.0,
        prompt_designer=None,
    ) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._storage = storage
        self._timeout_s = timeout_s
        self._results: dict[str, ComfyStatus] = {}
        # Optional async callable (prompt: str, exact_text: list[str], kind: str) -> str
        # -- normally GeminiTextClient.design_image_prompt, wired from app/main.py. Runs
        # before every generation to turn the router's short prompt into a detailed,
        # well-composed image-generation prompt. Best-effort: submit() falls back to
        # _build_image_prompt() (which still preserves exact_text) if this is None, or
        # if the call fails for any reason.
        self._prompt_designer = prompt_designer

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
        base_prompt = str(workflow_payload.get("prompt") or "").strip()
        exact_text = [
            t.strip() for t in (workflow_payload.get("exact_text") or []) if isinstance(t, str) and t.strip()
        ]

        if not base_prompt and not exact_text:
            self._results[prompt_id] = ComfyStatus(
                prompt_id=prompt_id, state="failed", error="empty_prompt"
            )
            return ComfySubmitResult(prompt_id=prompt_id)

        # "kind" (== job.kind == workflow_name, e.g. "poster_infographic") is a workflow
        # identifier, not a natural-language label -- prefer the job's own
        # "action_type" input ("poster"/"infographic") for phrasing the design
        # instruction, falling back to "poster" if genuinely absent (e.g. an older job
        # enqueued before this field existed).
        design_kind = str(workflow_payload.get("action_type") or "poster")

        # Baseline: prompt + a verbatim-text instruction block (always preserves
        # exact_text even if the design step below is unavailable or fails).
        prompt_text = base_prompt
        if self._prompt_designer is not None:
            try:
                designed = await self._prompt_designer(base_prompt, exact_text, design_kind)
                if designed and designed.strip():
                    prompt_text = designed.strip()
            except Exception as exc:
                logger.info(
                    "gemini image: prompt-design step failed (%s), using baseline prompt "
                    "prompt_id=%s",
                    type(exc).__name__,
                    prompt_id,
                )
        # Applied unconditionally (whether or not the design step ran) -- see
        # _exact_text_block's docstring for why this is deliberately redundant with what
        # the design step is separately instructed to do.
        prompt_text = prompt_text + _exact_text_block(exact_text)

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
