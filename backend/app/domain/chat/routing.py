"""Agentic intent-routing schema and pure classification/validation logic for the chat
interface. This module has no I/O -- it only defines the structured contract the LLM
router must produce and the server-side rules for interpreting it safely. The actual LLM
call lives in app.adapters.gemini.GeminiTextClient.route_intent(); orchestration
(persisting messages, creating pending actions, enqueueing jobs) lives in
app/api/v1/chat_router.py.

Security posture (see project instructions, "backend must remain authoritative"): the LLM
output is treated as an UNTRUSTED PROPOSAL. Every field is validated here before any
caller is allowed to act on it, and billing category is derived from the validated intent
by our own code (`derive_billing_category`), never taken from the model.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum

from pydantic import BaseModel, Field, ValidationError


class Intent(str, Enum):
    CHAT = "CHAT"
    GENERAL_IMAGE = "GENERAL_IMAGE"
    POSTER = "POSTER"
    INFOGRAPHIC = "INFOGRAPHIC"
    CLARIFICATION = "CLARIFICATION"


class ReasonCode(str, Enum):
    QUESTION_OR_DISCUSSION = "question_or_discussion"
    GENERAL_VISUAL_REQUEST = "general_visual_request"
    STRUCTURED_PROMOTIONAL_LAYOUT = "structured_promotional_layout"
    STRUCTURED_INFORMATION_DESIGN = "structured_information_design"
    AMBIGUOUS_VISUAL_DELIVERABLE = "ambiguous_visual_deliverable"
    EXPLICIT_USER_MODE = "explicit_user_mode"


# Intents that, if executed, spend real money against an external paid API. Derived here
# -- a single server-owned source of truth -- and never read off the LLM's own output.
_PAID_INTENTS = frozenset({Intent.POSTER, Intent.INFOGRAPHIC})


def derive_billing_category(intent: Intent) -> str:
    """GENERAL_IMAGE -> "local" (ComfyUI); POSTER/INFOGRAPHIC -> "paid" (Gemini image
    API, billed per generation -- see the Gemini pricing note in .env.example). CHAT and
    CLARIFICATION never reach this function since they don't produce a generation."""
    return "paid" if intent in _PAID_INTENTS else "local"


class RouteDecision(BaseModel):
    """Server-validated equivalent of the spec's `RouteDecision` TypeScript type. Field
    names are snake_case per this project's Python conventions (see AGENTS-equivalent
    project instructions: "Create a strict typed schema appropriate for the project
    language") -- the semantics are identical to the spec's normalizedPrompt/exactText/
    missingFields/clarificationQuestion/reasonCode.

    `extra="forbid"` rejects any additional keys the model might emit (e.g. accidental
    chain-of-thought fields) rather than silently accepting them.
    """

    model_config = {"extra": "forbid"}

    intent: Intent
    normalized_prompt: str
    exact_text: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    clarification_question: str | None = None
    reason_code: ReasonCode


class RouteDecisionError(ValueError):
    """Raised when the LLM's raw output doesn't conform to RouteDecision -- callers must
    fail safe (see chat_router.py: falls back to CLARIFICATION, never guesses a tool)."""


def parse_route_decision(raw: object) -> RouteDecision:
    if not isinstance(raw, dict):
        raise RouteDecisionError(f"expected a JSON object, got {type(raw).__name__}")
    try:
        return RouteDecision.model_validate(raw)
    except ValidationError as exc:
        raise RouteDecisionError(str(exc)) from exc


# JSON Schema handed to Gemini's structured-output config (response_schema) so the model
# is constrained to only ever emit this shape -- this is the "strict tool schema"
# requirement; google-genai's GenerateContentConfig(response_mime_type="application/json",
# response_schema=...) accepts a plain JSON-Schema-shaped dict. Keys intentionally mirror
# RouteDecision's field names exactly so parse_route_decision can validate the response
# with no key-translation step (translation would be one more place to get wrong).
ROUTE_DECISION_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [i.value for i in Intent],
        },
        "normalized_prompt": {"type": "string"},
        "exact_text": {"type": "array", "items": {"type": "string"}},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
        "clarification_question": {"type": "string", "nullable": True},
        "reason_code": {
            "type": "string",
            "enum": [r.value for r in ReasonCode],
        },
    },
    "required": [
        "intent",
        "normalized_prompt",
        "exact_text",
        "missing_fields",
        "clarification_question",
        "reason_code",
    ],
}

# Condensed, auditable version of the classification rules -- NOT chain-of-thought (the
# model isn't asked to explain itself, only to pick reason_code from the fixed enum
# above). Kept here (not inline in the adapter) so the classification policy is reviewable
# independently of the HTTP/SDK plumbing.
ROUTER_SYSTEM_INSTRUCTION = """\
You are the intent router for a university image-generation chat assistant. Classify the \
user's latest message (using the conversation so far for context) into exactly one \
intent, and respond ONLY with JSON matching the required schema.

Routes:
- CHAT: user asks a question, wants ideas/analysis/advice/copywriting only, discusses a \
design without asking to generate it, or mentions an image/poster/infographic without \
requesting execution. Never starts a generation job.
- GENERAL_IMAGE: user explicitly requests a primarily visual image -- a photo, \
illustration, artwork, character, object, background, or concept art; a visual asset \
without structured information; a promotional-looking visual that does NOT require a \
readable information hierarchy.
- POSTER: the requested result is a promotional/announcement layout with structured \
communication: headline, event title, date/time/location, organizer/logo, call to \
action, multiple typography levels, deliberate poster composition.
- INFOGRAPHIC: the requested result explains structured information: steps/process, \
timeline, comparison, categories, statistics, educational sections, charts/diagrams/\
labeled components.
- CLARIFICATION: use ONLY when choosing incorrectly would materially change the output \
or trigger a paid API (POSTER/INFOGRAPHIC are paid; GENERAL_IMAGE is local/free). Ask \
exactly one concise question. Do not ask about optional details that can safely default.

Field rules:
- normalized_prompt: the visual direction, cleaned up, but preserving the user's actual \
intent and language (do not translate Thai to English or vice versa).
- exact_text: any literal text/copy that must appear verbatim in the generated image \
(headlines, dates, place names the user actually typed) -- preserve exact wording, \
especially Thai. Do not invent additional text.
- missing_fields: critical factual values the user did not provide that a POSTER or \
INFOGRAPHIC cannot be correctly generated without (e.g. event date, location, specific \
statistics). Never invent dates, times, locations, prices, statistics, organization \
names, or URLs -- list them as missing instead.
- clarification_question: required (non-null) only when intent is CLARIFICATION; null \
otherwise.
- reason_code: pick the single best-matching enum value, not free text.

Do not classify a GENERAL_IMAGE request as POSTER merely because it mentions marketing, \
an event, or an organization. Do not downgrade a POSTER/INFOGRAPHIC request to \
GENERAL_IMAGE to avoid cost. If the user has explicitly selected a mode elsewhere in the \
conversation, respect it (reason_code: explicit_user_mode) unless it is incompatible or \
unsafe.
"""


def compute_params_fingerprint(params: dict) -> str:
    """Deterministic hash of a pending action's normalized parameters, used to detect
    "parameters changed since this confirmation was issued" (see PendingAction.
    params_fingerprint / app/api/v1/chat_router.py's confirm endpoint)."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
