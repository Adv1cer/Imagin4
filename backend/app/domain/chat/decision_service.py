"""Pure server-side decision layer: given a validated RouteDecision (already schema- and
enum-checked by app.domain.chat.routing.parse_route_decision), decides exactly what the
backend is authoritatively allowed to do next.

No I/O here on purpose -- app/api/v1/chat_router.py executes the returned NextStep
against real dependencies (DB session, job queue). Keeping this pure is what makes it
possible to unit test "does a CHAT message ever start a job" or "does a POSTER request
with missing fields ever create a pending (billable) action" as plain assertions on a
function's return value, without a database or a live Gemini call -- see
tests/unit/test_chat_decision_service.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from app.domain.chat.routing import Intent, RouteDecision, derive_billing_category


@dataclass(frozen=True)
class RespondChat:
    """CHAT: reply normally. Never starts a generation job."""


@dataclass(frozen=True)
class RespondClarification:
    """CLARIFICATION: the LLM's own choice when the requested deliverable type itself is
    ambiguous (e.g. could plausibly be either a free GENERAL_IMAGE or a paid POSTER, and
    guessing wrong changes billing). Never starts any job, paid or otherwise.

    NOTE: as of the explicit product decision to stop asking for missing POSTER/
    INFOGRAPHIC details in chat (2026-08), this is no longer also used as a
    missing_fields safety net -- see EnqueuePaidImage below. It still exists purely for
    genuinely ambiguous intent."""

    question: str


@dataclass(frozen=True)
class EnqueueGeneralImage:
    """GENERAL_IMAGE: safe to enqueue immediately through the existing local ComfyUI
    path -- no confirmation needed, billing_category is always "local"."""

    prompt: str
    exact_text: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EnqueuePaidImage:
    """POSTER or INFOGRAPHIC: enqueue immediately through the Gemini image pipeline, no
    chat clarification and no pending-action confirmation step.

    Explicit product decision (2026-08): the earlier design asked the user a chat
    question when RouteDecision.missing_fields was non-empty, and required a separate
    POST /pending-actions/{id}/confirm click before spending money at all. Both were
    removed by request -- "if the latest message is clearly a poster/infographic
    request, send the prompt straight to Gemini; let Gemini's own research fill in
    gaps and generate immediately." The best-effort grounded-research step
    (app/api/v1/chat_router.py:_research_augment) still runs first to enrich
    normalized_prompt/exact_text with real facts when possible, and the prompt-design
    step (GeminiTextClient.design_image_prompt) still runs before the actual image
    call -- neither of those gates execution anymore, they just improve its quality.

    This intentionally trades the "never spend money without an explicit confirm
    click" invariant from the original spec for the "just do it" UX the project owner
    asked for. billing_category is still recorded (always "paid") for observability/
    audit purposes even though nothing gates on it anymore."""

    action_type: str  # "poster" | "infographic"
    prompt: str
    exact_text: list[str]
    billing_category: str


NextStep = Union[RespondChat, RespondClarification, EnqueueGeneralImage, EnqueuePaidImage]

_DEFAULT_CLARIFICATION_QUESTION = "ช่วยอธิบายเพิ่มเติมได้ไหมคะว่าต้องการอะไร?"


def decide_next_step(decision: RouteDecision) -> NextStep:
    """The single authoritative mapping from a validated LLM proposal to a backend
    action. Every branch is closed and explicit -- there is no default/fallthrough path,
    which is what actually enforces "LLM output is an untrusted proposal" structurally:
    every intent maps to exactly one well-defined action, never an implicit guess.
    """
    if decision.intent == Intent.CHAT:
        return RespondChat()

    if decision.intent == Intent.CLARIFICATION:
        return RespondClarification(
            question=decision.clarification_question or _DEFAULT_CLARIFICATION_QUESTION
        )

    if decision.intent == Intent.GENERAL_IMAGE:
        return EnqueueGeneralImage(prompt=decision.normalized_prompt, exact_text=decision.exact_text)

    # Intent.POSTER or Intent.INFOGRAPHIC: enqueue directly, no confirmation gate (see
    # EnqueuePaidImage's docstring for the product decision behind this).
    action_type = "poster" if decision.intent == Intent.POSTER else "infographic"
    return EnqueuePaidImage(
        action_type=action_type,
        prompt=decision.normalized_prompt,
        exact_text=decision.exact_text,
        billing_category=derive_billing_category(decision.intent),
    )
