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
    """CLARIFICATION -- either the LLM's own choice, or the backend downgrading a
    POSTER/INFOGRAPHIC decision that still had unresolved missing_fields (see
    decide_next_step below). Never starts any job, paid or otherwise."""

    question: str


@dataclass(frozen=True)
class EnqueueGeneralImage:
    """GENERAL_IMAGE: safe to enqueue immediately through the existing local ComfyUI
    path -- no confirmation needed, billing_category is always "local"."""

    prompt: str
    exact_text: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CreatePendingAction:
    """POSTER or INFOGRAPHIC with no unresolved missing_fields: create a server-side
    pending action and return a confirmation card. This NEVER calls the paid API
    directly -- that only happens from POST /v1/pending-actions/{id}/confirm, and only
    for the authenticated owner, before expiry, exactly once."""

    action_type: str  # "poster" | "infographic"
    prompt: str
    exact_text: list[str]
    billing_category: str


NextStep = Union[RespondChat, RespondClarification, EnqueueGeneralImage, CreatePendingAction]

_DEFAULT_CLARIFICATION_QUESTION = "ช่วยอธิบายเพิ่มเติมได้ไหมคะว่าต้องการอะไร?"
_MISSING_FIELDS_QUESTION_TEMPLATE = "ก่อนสร้างให้ ขอข้อมูลเพิ่มอีกนิดนะคะ: {fields} คืออะไรคะ?"


def decide_next_step(decision: RouteDecision) -> NextStep:
    """The single authoritative mapping from a validated LLM proposal to a backend
    action. Every branch is closed and explicit -- there is no default/fallthrough path
    that executes a paid operation, which is what actually enforces "LLM output is an
    untrusted proposal, not permission to execute an operation" structurally rather than
    just as a comment.
    """
    if decision.intent == Intent.CHAT:
        return RespondChat()

    if decision.intent == Intent.CLARIFICATION:
        return RespondClarification(
            question=decision.clarification_question or _DEFAULT_CLARIFICATION_QUESTION
        )

    if decision.intent == Intent.GENERAL_IMAGE:
        return EnqueueGeneralImage(prompt=decision.normalized_prompt, exact_text=decision.exact_text)

    # Intent.POSTER or Intent.INFOGRAPHIC from here on.
    #
    # Backend safety net, not solely the LLM's judgment: if critical fields are still
    # missing, do NOT create a pending action -- that would let a single confirm click
    # spend money on a request we know is incomplete (a missing event date/location,
    # etc.). Downgrade to a clarification asking for exactly those fields instead. This
    # also implements "conversation follow-ups continue the pending intent" for free: the
    # user's next message gets appended to conversation history and routed again, and by
    # then missing_fields is empty (the user just supplied it), so it naturally resolves
    # to CreatePendingAction on the next turn -- no separate "resume state" bookkeeping
    # needed beyond the chat history that already exists.
    if decision.missing_fields:
        question = decision.clarification_question or _MISSING_FIELDS_QUESTION_TEMPLATE.format(
            fields=", ".join(decision.missing_fields)
        )
        return RespondClarification(question=question)

    action_type = "poster" if decision.intent == Intent.POSTER else "infographic"
    return CreatePendingAction(
        action_type=action_type,
        prompt=decision.normalized_prompt,
        exact_text=decision.exact_text,
        billing_category=derive_billing_category(decision.intent),
    )
