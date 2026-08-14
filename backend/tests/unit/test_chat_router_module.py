"""Unit tests for the pure/importable parts of app/api/v1/chat_router.py that don't
require a database connection: the fail-safe fallback decision, and the workflow/error
mapping tables. Importing the module itself requires no live DB (SQLAlchemy statements
are only executed when a route handler actually runs against a session)."""

from __future__ import annotations

from app.api.v1.chat_router import (
    _CONFIRM_ERROR_DETAIL,
    _CONFIRM_ERROR_STATUS,
    _WORKFLOW_FOR_ACTION_TYPE,
    _fallback_clarification,
)
from app.domain.chat.decision_service import RespondClarification, decide_next_step
from app.domain.chat.pending_actions import ConfirmOutcome
from app.domain.chat.routing import Intent


def test_fallback_clarification_never_guesses_a_tool():
    """Invalid/failed LLM output must fail safe to CLARIFICATION -- never to
    GENERAL_IMAGE/POSTER/INFOGRAPHIC, and never with a guessed prompt."""
    decision = _fallback_clarification("invalid_schema")
    assert decision.intent == Intent.CLARIFICATION
    assert decision.clarification_question

    step = decide_next_step(decision)
    assert isinstance(step, RespondClarification)


def test_fallback_clarification_result_starts_no_job_via_decision_layer():
    decision = _fallback_clarification("llm_call_failed")
    step = decide_next_step(decision)
    # RespondClarification is the only safe outcome; anything else would mean invalid
    # LLM output could somehow still trigger a generation.
    assert type(step).__name__ == "RespondClarification"


def test_fallback_clarification_for_gemini_overload_says_busy_not_unclear():
    """Regression: route_intent failing with a 503 UNAVAILABLE (Gemini overloaded --
    see app.adapters.gemini._sanitized_error, which route_intent wraps into
    RuntimeError("gemini_overloaded")) used to fall back to the same generic "I
    couldn't understand you, please clarify" question as a genuinely malformed/
    unclassifiable message -- confusing to a customer who typed something perfectly
    clear while Gemini itself was just temporarily busy. Same class of fix as the
    earlier image-generation error-classification change, applied to the chat/routing
    path too."""
    decision = _fallback_clarification("gemini_overloaded")
    assert decision.intent == Intent.CLARIFICATION
    assert "หนาแน่น" in decision.clarification_question
    assert "แยกแยะคำขอไม่ได้" not in decision.clarification_question

    step = decide_next_step(decision)
    assert type(step).__name__ == "RespondClarification"


def test_fallback_clarification_for_gemini_rate_limit_also_says_busy():
    decision = _fallback_clarification("gemini_rate_limited")
    assert "หนาแน่น" in decision.clarification_question


def test_fallback_clarification_generic_reasons_keep_the_original_question():
    for reason in ("invalid_schema", "llm_call_failed"):
        decision = _fallback_clarification(reason)
        assert "แยกแยะคำขอไม่ได้" in decision.clarification_question


def test_poster_and_infographic_both_map_to_an_allowlisted_workflow():
    assert _WORKFLOW_FOR_ACTION_TYPE["poster"] == "poster_infographic"
    assert _WORKFLOW_FOR_ACTION_TYPE["infographic"] == "poster_infographic"


def test_every_non_ok_confirm_outcome_has_an_http_mapping():
    non_ok_outcomes = [o for o in ConfirmOutcome if o != ConfirmOutcome.OK]
    for outcome in non_ok_outcomes:
        assert outcome in _CONFIRM_ERROR_STATUS, f"missing status mapping for {outcome}"
        assert outcome in _CONFIRM_ERROR_DETAIL, f"missing detail mapping for {outcome}"


def test_wrong_owner_and_not_found_return_the_same_status():
    """Don't leak whether a pending_action_id exists to a non-owner -- both must 404
    identically."""
    assert (
        _CONFIRM_ERROR_STATUS[ConfirmOutcome.WRONG_OWNER]
        == _CONFIRM_ERROR_STATUS[ConfirmOutcome.NOT_FOUND]
    )
