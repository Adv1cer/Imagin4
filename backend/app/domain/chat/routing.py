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
intent and language (do not translate Thai to English or vice versa). Describe ONLY what \
the LATEST message itself asks for. Earlier turns are context for interpreting ambiguous \
references in the latest message (e.g. "make it bigger", "same style as before") -- they \
are NEVER material to combine, merge, or summarize into the same image. If this \
conversation contains several earlier, unrelated image/poster/infographic requests \
(common in a long testing session), do not fold their subjects into this one: a request \
for "a red panda" stays a request for exactly one red panda, never a collage/portfolio/\
grid that also includes a cat, coffee, or people from earlier unrelated turns. Only \
combine multiple subjects into one composition if the LATEST message explicitly asks for \
that (e.g. "put the cat and the coffee cup in the same photo").
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


# Used for the optional best-effort research step (see
# GeminiTextClient.research_missing_fields in app/adapters/gemini.py and
# app/api/v1/chat_router.py): when a POSTER/INFOGRAPHIC has missing_fields, we first try
# a real Google Search grounded call to find the facts before asking the user.
#
# NOTE: Gemini's API does not allow response_schema (structured output) and the
# google_search tool in the same call (confirmed via the Gemini API docs/forum -- "Search
# Grounding can't be used with JSON/YAML/XML mode"), so this has to be two separate calls:
# 1) a free-text grounded search call using this instruction, then
# 2) a second structured route_intent() call with the findings injected into the system
#    instruction (see build_router_system_instruction_with_research below) to
#    re-classify with the same strict schema.
RESEARCH_SYSTEM_INSTRUCTION = """\
You are a fact-finding assistant for a university image-generation chat tool. The user \
asked for a POSTER or INFOGRAPHIC, but some required factual fields are still unknown. \
Use Google Search to try to find REAL, CURRENT, VERIFIABLE facts for the missing fields \
listed below, based on the conversation so far.

Rules:
- Only report facts you actually found via search, with enough specificity to be useful \
(e.g. an exact date, not "sometime this year"). Never guess, estimate, or infer a fact \
that search did not actually surface.
- If you cannot find a confident, current answer for a field, say so explicitly for that \
field instead of guessing.
- Keep the answer short and factual: one line per missing field, in the format \
"<field>: <finding or 'not found'>". Do not add commentary, opinions, or extra formatting.
"""


def build_research_query(history: list[dict[str, str]], missing_fields: list[str]) -> str:
    """Builds the user-turn text for the research call from the missing_fields list --
    kept as a pure function (no I/O) so it's unit-testable independently of the actual
    Gemini call in app/adapters/gemini.py."""
    latest_user_texts = [h["text"] for h in history if h.get("role") == "user"]
    latest = latest_user_texts[-1] if latest_user_texts else ""
    fields = "\n".join(f"- {f}" for f in missing_fields)
    return (
        f"Conversation context (latest user request): {latest}\n\n"
        f"Missing fields to research:\n{fields}"
    )


def build_router_system_instruction_with_research(research_findings: str) -> str:
    """Appends grounded research findings to the router's system instruction for a
    re-classification call. The findings are clearly scoped as reference material the
    model may use to fill previously-missing fields -- never as license to invent
    anything beyond what's stated here."""
    return (
        ROUTER_SYSTEM_INSTRUCTION
        + "\n\nVerified research findings (from a real Google Search call made for this "
        "conversation -- use ONLY these to fill in previously-missing fields if they "
        "directly answer them; if a finding says 'not found' or doesn't clearly answer "
        "a field, keep that field in missing_fields; never invent beyond what's written "
        "here):\n"
        + research_findings
    )


# Used by GeminiTextClient.design_image_prompt (app/adapters/gemini.py), a best-effort
# step that runs before the actual Gemini image-generation call (POSTER/INFOGRAPHIC --
# workflow "poster_infographic", backend="gemini" in workflow_registry.py): have the
# text model act as a prompt engineer and write a detailed, well-composed
# image-generation prompt (layout, color, typography direction) instead of sending the
# router's short normalized_prompt straight to the image model as-is. Never a source of
# new facts -- explicitly forbidden from inventing anything beyond what it's given, and
# required to preserve exact_text verbatim, same invariant as the router itself.
PROMPT_DESIGN_SYSTEM_INSTRUCTION_TEMPLATE = """\
You are an expert prompt engineer for an AI image generation model that creates {kind}s. \
Given a content brief and a list of text that MUST appear verbatim in the final image, \
write a single, detailed image-generation prompt that will produce a visually excellent, \
professional result.

Your prompt must:
- Describe a clear layout and visual hierarchy appropriate for a {kind} (headline \
placement, supporting text, imagery, logo/QR placement as relevant).
- Specify a coherent color palette, typography style, and overall visual mood that fits \
the subject matter.
- Explicitly instruct that the given text must be rendered exactly as provided, \
verbatim, without translation, paraphrasing, or alteration.
- NOT invent any factual content (dates, prices, names, statistics, organizations) \
beyond what is given to you -- only add visual/design direction, never new facts.
- Be written as plain natural-language prompt text (a paragraph or a few short \
paragraphs) -- not a list, not JSON, not markdown -- since it is sent directly to the \
image model.
- Describe exactly the ONE {kind} the content brief asks for -- never expand it into a \
collage, portfolio, contact sheet, or multiple separate designs, even if earlier \
unrelated requests are visible elsewhere in context.

Respond with ONLY the final image-generation prompt text. No preamble, no explanation, \
no surrounding quotation marks.
"""


def build_prompt_design_instruction(kind: str) -> str:
    return PROMPT_DESIGN_SYSTEM_INSTRUCTION_TEMPLATE.format(kind=kind)


def build_prompt_design_user_message(prompt: str, exact_text: list[str]) -> str:
    exact_lines = "\n".join(f"- {t}" for t in exact_text) if exact_text else "(none)"
    return (
        f"Content brief: {prompt}\n\n"
        f"Text that must appear verbatim in the image:\n{exact_lines}"
    )


# Used by GeminiTextClient.design_comfyui_prompt, the equivalent best-effort refinement
# step for the ORDINARY image path (GENERAL_IMAGE -- workflow "image_basic",
# backend="comfyui"). Deliberately a SEPARATE instruction from
# PROMPT_DESIGN_SYSTEM_INSTRUCTION_TEMPLATE above, not a shared one with a flag: ComfyUI
# diffusion models (SDXL / Qwen-Image, see app/adapters/comfyui/live.py) respond well to
# dense, comma-separated descriptive/style keywords and are unreliable at rendering
# in-image text at all -- asking for the poster instruction's "render this text
# verbatim" framing would be actively counterproductive here, unlike for Gemini's image
# model. GENERAL_IMAGE requests also rarely carry exact_text in the first place (see
# routing rules: "a visual asset without structured information"), so this treats it as
# optional supporting context rather than something requiring verbatim rendering.
COMFY_PROMPT_DESIGN_SYSTEM_INSTRUCTION = """\
You are an expert prompt engineer for a diffusion-based AI image generation model \
(similar to Stable Diffusion / SDXL). Given a content brief, write a single, detailed \
positive prompt that will produce a visually excellent result.

Your prompt must:
- Be a dense, comma-separated list of descriptive phrases and keywords: subject, \
setting, composition, lighting, color palette, art style/medium, mood, and relevant \
quality modifiers (e.g. "highly detailed", "sharp focus") -- this is the format \
diffusion models respond best to, NOT a narrative paragraph.
- Stay faithful to the content brief's actual subject and intent -- do not change what \
the image is of. The brief describes exactly ONE subject/scene for ONE single image -- \
never expand it into a collage, portfolio, contact sheet, multi-panel, or grid layout \
containing other subjects, even if earlier unrelated requests are visible elsewhere in \
context. Only describe multiple subjects together if the brief itself explicitly asks \
for them to appear in the same image.
- NOT invent specific factual content (dates, names, statistics, real organizations) --
only add visual/style/composition direction.
- NOT instruct the model to render specific in-image text verbatim -- this generation \
backend is unreliable at rendering readable text, so avoid relying on it; if the brief \
mentions text or wording, treat it only as thematic context, not text to render.

Respond with ONLY the final positive prompt text (comma-separated keywords/phrases). No \
preamble, no explanation, no surrounding quotation marks.
"""


def build_comfy_prompt_design_user_message(prompt: str, exact_text: list[str]) -> str:
    if not exact_text:
        return f"Content brief: {prompt}"
    context_lines = "\n".join(f"- {t}" for t in exact_text)
    return (
        f"Content brief: {prompt}\n\n"
        f"Related thematic context (not to be rendered as text):\n{context_lines}"
    )


def compute_params_fingerprint(params: dict) -> str:
    """Deterministic hash of a pending action's normalized parameters, used to detect
    "parameters changed since this confirmation was issued" (see PendingAction.
    params_fingerprint / app/api/v1/chat_router.py's confirm endpoint)."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
