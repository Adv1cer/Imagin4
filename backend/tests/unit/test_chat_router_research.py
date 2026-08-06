"""Unit tests for app/api/v1/chat_router.py:_research_augment -- the best-effort
"research before asking" step: when a POSTER/INFOGRAPHIC classification has
missing_fields, try a grounded Google Search call to fill them in with real facts before
falling back to clarification.

_research_augment only takes a gemini-like object + plain data (no DB session), so it's
fully unit-testable with a fake client -- no real Gemini call, no DB."""

from __future__ import annotations

import uuid

import pytest

from app.api.v1.chat_router import _research_augment
from app.domain.chat.routing import Intent, ReasonCode, RouteDecision

CONV_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
HISTORY = [{"role": "user", "text": "ทำโปสเตอร์ Open House ของ UTCC"}]


def _decision(**overrides) -> RouteDecision:
    base = dict(
        intent=Intent.POSTER,
        normalized_prompt="Open House poster",
        exact_text=["Open House"],
        missing_fields=["event date", "location"],
        clarification_question=None,
        reason_code=ReasonCode.STRUCTURED_PROMOTIONAL_LAYOUT,
    )
    base.update(overrides)
    return RouteDecision(**base)


class FakeGemini:
    def __init__(self, *, research_result=None, research_raises=None, route_result=None, route_raises=None):
        self._research_result = research_result
        self._research_raises = research_raises
        self._route_result = route_result
        self._route_raises = route_raises
        self.research_calls = []
        self.route_calls = []

    async def research_missing_fields(self, history, missing_fields):
        self.research_calls.append((history, missing_fields))
        if self._research_raises:
            raise self._research_raises
        return self._research_result

    async def route_intent(self, history, extra_system_instruction=None):
        self.route_calls.append((history, extra_system_instruction))
        if self._route_raises:
            raise self._route_raises
        return self._route_result


@pytest.mark.asyncio
async def test_chat_intent_never_triggers_research():
    gemini = FakeGemini()
    decision = _decision(intent=Intent.CHAT, missing_fields=[])
    result = await _research_augment(gemini, HISTORY, decision, CONV_ID, USER_ID)
    assert result is decision
    assert gemini.research_calls == []


@pytest.mark.asyncio
async def test_no_missing_fields_never_triggers_research():
    gemini = FakeGemini()
    decision = _decision(missing_fields=[])
    result = await _research_augment(gemini, HISTORY, decision, CONV_ID, USER_ID)
    assert result is decision
    assert gemini.research_calls == []


@pytest.mark.asyncio
async def test_successful_research_and_reclassification_narrows_missing_fields():
    refined_raw = {
        "intent": "POSTER",
        "normalized_prompt": "Open House poster, 20 August at the auditorium",
        "exact_text": ["Open House", "20 สิงหาคม", "หอประชุม"],
        "missing_fields": [],
        "clarification_question": None,
        "reason_code": "structured_promotional_layout",
    }
    gemini = FakeGemini(
        research_result="event date: 20 August 2026\nlocation: หอประชุม (auditorium)",
        route_result=refined_raw,
    )
    decision = _decision()
    result = await _research_augment(gemini, HISTORY, decision, CONV_ID, USER_ID)
    assert result.missing_fields == []
    assert result.intent == Intent.POSTER
    assert len(gemini.research_calls) == 1
    assert len(gemini.route_calls) == 1
    # The re-classification call must pass a research-augmented instruction, not the
    # bare default (None would mean "use ROUTER_SYSTEM_INSTRUCTION as-is").
    assert gemini.route_calls[0][1] is not None
    assert "20 August 2026" in gemini.route_calls[0][1]


@pytest.mark.asyncio
async def test_research_call_failure_falls_back_to_original_decision():
    gemini = FakeGemini(research_raises=RuntimeError("gemini_error:TimeoutError"))
    decision = _decision()
    result = await _research_augment(gemini, HISTORY, decision, CONV_ID, USER_ID)
    assert result is decision
    assert gemini.route_calls == []  # never even attempts re-classification


@pytest.mark.asyncio
async def test_reclassification_call_failure_falls_back_to_original_decision():
    gemini = FakeGemini(
        research_result="event date: 20 August 2026",
        route_raises=RuntimeError("gemini_error:TimeoutError"),
    )
    decision = _decision()
    result = await _research_augment(gemini, HISTORY, decision, CONV_ID, USER_ID)
    assert result is decision


@pytest.mark.asyncio
async def test_malformed_reclassification_output_falls_back_to_original_decision():
    gemini = FakeGemini(
        research_result="event date: 20 August 2026",
        route_result={"intent": "NOT_A_REAL_INTENT"},
    )
    decision = _decision()
    result = await _research_augment(gemini, HISTORY, decision, CONV_ID, USER_ID)
    assert result is decision


@pytest.mark.asyncio
async def test_reclassification_changing_intent_is_rejected():
    """Safety invariant: the research step must never be the mechanism that changes
    intent (and therefore billing category) -- only the original classification call
    may decide that. If a re-classification somehow flips POSTER -> GENERAL_IMAGE (e.g.
    a prompt-injection attempt via search results), it must be ignored."""
    flipped_raw = {
        "intent": "GENERAL_IMAGE",
        "normalized_prompt": "a generic poster-like image",
        "exact_text": [],
        "missing_fields": [],
        "clarification_question": None,
        "reason_code": "general_visual_request",
    }
    gemini = FakeGemini(research_result="event date: 20 August 2026", route_result=flipped_raw)
    decision = _decision()
    result = await _research_augment(gemini, HISTORY, decision, CONV_ID, USER_ID)
    assert result is decision
    assert result.intent == Intent.POSTER
