"""Self-hosted Qwen3.8-27B "brain" adapter, served over vLLM's OpenAI-compatible API
(see docker-compose.yml's `qwen-brain` service) -- selected via `Settings.brain_backend
== "qwen"` (see app/core/config.py's qwen_* settings block for the quantization/VRAM
budget reasoning). Mirrors `app.adapters.gemini.GeminiTextClient`'s public method surface
exactly (same signatures/return types) so it drops into `app.state.gemini_text_client`
(app/main.py's `_build_state`) without touching app/api/v1/chat_router.py's call sites --
there is no formal Protocol either module implements (see GeminiTextClient's docstring),
so "matches by convention" is the actual contract; keep this file's methods in sync with
GeminiTextClient's if that one changes.

Uses `httpx` (already a dependency, see pyproject.toml) rather than adding the `openai`
SDK -- vLLM's OpenAI-compatible surface is small enough (POST /v1/chat/completions) that a
raw client is simpler than pulling in and configuring a whole second SDK alongside
google-genai.

KNOWN GAP (see Settings.brain_backend's docstring): `research_missing_fields` always
raises `RuntimeError("qwen_error:NotImplemented")` -- there is no grounded web-search tool
wired up for the self-hosted model. app/api/v1/chat_router.py already treats this call as
best-effort/optional (falls back to asking the user directly on any failure), so this is a
deliberate, graceful feature reduction under brain_backend="qwen", not a bug to silently
paper over with a fake response.
"""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger("imaginv.qwen")

# Chat roles this backend understands, mirroring app.adapters.gemini's _ROLE_TO_GEMINI --
# vLLM's OpenAI-compatible endpoint speaks plain "user"/"assistant"/"system", so no
# translation is actually needed here beyond filtering to roles our own history uses.
_KNOWN_ROLES = {"user", "assistant"}

# Mirrors app.adapters.gemini.GEMINI_OVERLOAD_ERROR_CODES exactly in spirit (see that
# module's docstring for the full reasoning) -- distinct string values (qwen_* not
# gemini_*) so a caller can tell which backend actually produced the error from logs/
# job_events, but the same semantic meaning: "temporarily busy / timed out, safe to retry
# shortly, not a config or input problem." app/api/v1/chat_router.py and
# app/api/v1/conversations.py union this with GEMINI_OVERLOAD_ERROR_CODES so both backends
# are recognized regardless of which one is actually active.
QWEN_OVERLOAD_ERROR_CODES = frozenset({"qwen_overloaded", "qwen_rate_limited", "qwen_timeout"})


def _sanitized_error(exc: Exception) -> str:
    """Same "never echo raw exception text" rule as app.adapters.gemini._sanitized_error
    -- keep only a controlled, safe code."""
    if isinstance(exc, httpx.TimeoutException):
        return "qwen_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 503:
            return "qwen_overloaded"
        if status == 429:
            return "qwen_rate_limited"
    return f"qwen_error:{type(exc).__name__}"


def _history_to_messages(history: list[dict[str, str]], system: str | None = None) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(
        {"role": h["role"], "content": h["text"]}
        for h in history
        if h["role"] in _KNOWN_ROLES and h["text"].strip()
    )
    return messages


class QwenTextClient:
    """See module docstring. `base_url` is the vLLM server's root (e.g.
    "http://qwen-brain:8000" inside docker-compose, or "http://localhost:8100" from the
    host) -- this client appends "/v1/chat/completions" itself, matching the
    OpenAI-compatible path vLLM serves."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = 30.0,
        research_timeout_s: float = 20.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        self._research_timeout_s = research_timeout_s

    async def _chat(
        self,
        messages: list[dict[str, str]],
        timeout_s: float,
        response_format: dict | None = None,
        extra_body: dict | None = None,
    ) -> str:
        payload: dict = {"model": self._model, "messages": messages, "stream": False}
        if response_format is not None:
            payload["response_format"] = response_format
        if extra_body:
            payload.update(extra_body)
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(f"{self._base_url}/v1/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()

    async def complete(self, history: list[dict[str, str]]) -> str:
        """Same contract as GeminiTextClient.complete: chronological
        [{"role": "user"|"assistant", "text": "..."}, ...], raises RuntimeError on
        failure -- callers turn that into a sanitized 503, never a raw exception."""
        messages = _history_to_messages(history)
        if not messages:
            return "(nothing to reply to)"
        try:
            text = await self._chat(messages, self._timeout_s)
        except Exception as exc:
            logger.exception("qwen text completion failed")
            raise RuntimeError(_sanitized_error(exc)) from exc
        return text or "(empty response)"

    async def route_intent(
        self, history: list[dict[str, str]], extra_system_instruction: str | None = None
    ) -> dict:
        """Same contract as GeminiTextClient.route_intent -- returns a raw dict the
        caller must validate via app.domain.chat.routing.parse_route_decision. Uses
        vLLM's `guided_json` extra_body param (structured-output/constrained decoding,
        the vLLM-specific equivalent of Gemini's response_schema) so the model is forced
        to emit JSON matching ROUTE_DECISION_JSON_SCHEMA rather than merely being asked
        nicely to via the prompt.

        DELIBERATELY does NOT also set `response_format={"type": "json_object"}`
        alongside `guided_json` (an earlier version of this method sent both).
        Confirmed live against qwen-brain (2026-08): sending both together made vLLM
        silently honor only the generic "any valid JSON" constraint from
        `response_format` and drop the actual `guided_json` schema constraint entirely
        -- the call still returned 200 OK with parseable JSON, but with wrong shape
        (e.g. `exact_text`/`missing_fields` as null instead of `[]`, `reason_code` set
        to a value outside ROUTE_DECISION_JSON_SCHEMA's enum) causing
        parse_route_decision to raise RouteDecisionError on every single call. vLLM's
        own structured-outputs examples only ever pass `guided_json` alone (see
        https://docs.vllm.ai/en/latest/features/structured_outputs/), which is
        sufficient by itself to force valid-JSON-matching-schema output -- no
        `response_format` needed on top.

        Uses ROUTE_DECISION_JSON_SCHEMA_VLLM (not the Gemini-flavored
        ROUTE_DECISION_JSON_SCHEMA) -- see that constant's docstring in routing.py:
        vLLM's guided-decoding grammar compiler doesn't understand Gemini's `nullable`
        keyword, and an unrecognized keyword can trigger a slow fallback path
        (xgrammar -> outlines) that blows past this client's own timeout. A generous
        `route_timeout_s` (well above the general-purpose `self._timeout_s`) is used
        for this call specifically to give first-time grammar compilation room even so
        -- vLLM caches the compiled grammar after the first call, so subsequent
        route_intent calls are fast."""
        from app.domain.chat.routing import ROUTE_DECISION_JSON_SCHEMA_VLLM, ROUTER_SYSTEM_INSTRUCTION

        system_instruction = extra_system_instruction or ROUTER_SYSTEM_INSTRUCTION
        messages = _history_to_messages(history, system=system_instruction)
        if len(messages) <= 1:  # only the system message, no real turns
            raise RuntimeError("qwen_error:EmptyHistory")
        route_timeout_s = max(self._timeout_s, 90.0)
        try:
            raw_text = await self._chat(
                messages,
                route_timeout_s,
                extra_body={"guided_json": ROUTE_DECISION_JSON_SCHEMA_VLLM},
            )
            return json.loads(raw_text)
        except Exception as exc:
            logger.exception("qwen intent routing failed")
            raise RuntimeError(_sanitized_error(exc)) from exc

    async def research_missing_fields(
        self, history: list[dict[str, str]], missing_fields: list[str]
    ) -> str:
        """See module docstring's KNOWN GAP -- no grounded search tool exists for the
        self-hosted model, so this always raises. app/api/v1/chat_router.py's
        _research_augment already treats any exception here as "skip research, ask the
        user normally," so this fails safe rather than fabricating an answer."""
        raise RuntimeError("qwen_error:NotImplemented")

    async def design_image_prompt(
        self, prompt: str, exact_text: list[str], kind: str = "poster"
    ) -> str:
        """Same contract as GeminiTextClient.design_image_prompt."""
        from app.domain.chat.routing import build_prompt_design_instruction, build_prompt_design_user_message

        messages = _history_to_messages(
            [{"role": "user", "text": build_prompt_design_user_message(prompt, exact_text)}],
            system=build_prompt_design_instruction(kind),
        )
        try:
            return await self._chat(messages, self._timeout_s)
        except Exception as exc:
            logger.warning("qwen prompt-design call failed: %s", type(exc).__name__)
            raise RuntimeError(_sanitized_error(exc)) from exc

    async def design_comfyui_prompt(self, prompt: str, exact_text: list[str]) -> str:
        """Same contract as GeminiTextClient.design_comfyui_prompt."""
        from app.domain.chat.routing import (
            COMFY_PROMPT_DESIGN_SYSTEM_INSTRUCTION,
            build_comfy_prompt_design_user_message,
        )

        messages = _history_to_messages(
            [{"role": "user", "text": build_comfy_prompt_design_user_message(prompt, exact_text)}],
            system=COMFY_PROMPT_DESIGN_SYSTEM_INSTRUCTION,
        )
        try:
            return await self._chat(messages, self._timeout_s)
        except Exception as exc:
            logger.warning("qwen comfy prompt-design call failed: %s", type(exc).__name__)
            raise RuntimeError(_sanitized_error(exc)) from exc
