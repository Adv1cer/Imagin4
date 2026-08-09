"""Unit tests for app/domain/chat/decision_service.py -- the pure, backend-authoritative
mapping from a validated RouteDecision to exactly one NextStep.

Per project instructions ("prefer testing validated structured decisions rather than
asserting exact natural-language model output"), these tests model each of the spec's
example messages as the RouteDecision a correctly-behaving router would produce for it,
then assert the DOWNSTREAM decision/execution behavior is correct and safe. Testing
whether Gemini itself classifies Thai/English natural language correctly is not something
a deterministic, offline unit test can do -- that would require a live model call and
would be flaky by nature; the strict schema + this pure decision layer is what's
unit-testable.

PRODUCT DECISION (2026-08): POSTER/INFOGRAPHIC used to downgrade to RespondClarification
when missing_fields was non-empty, and otherwise produce a CreatePendingAction requiring
a separate confirm click before any paid call. Both were removed by explicit request --
POSTER/INFOGRAPHIC now always produces EnqueuePaidImage (immediate enqueue), regardless
of missing_fields. See EnqueuePaidImage's docstring in decision_service.py for the full
rationale. These tests were updated accordingly rather than left asserting the old
behavior."""

from __future__ import annotations

from app.domain.chat.decision_service import (
    EnqueueGeneralImage,
    EnqueuePaidImage,
    RespondChat,
    RespondClarification,
    decide_next_step,
)
from app.domain.chat.routing import Intent, ReasonCode, RouteDecision


def _decision(intent: Intent, **overrides) -> RouteDecision:
    base = dict(
        intent=intent,
        normalized_prompt="x",
        exact_text=[],
        missing_fields=[],
        clarification_question=None,
        reason_code=ReasonCode.GENERAL_VISUAL_REQUEST,
    )
    base.update(overrides)
    return RouteDecision(**base)


# --- The spec's 10 classification examples, modeled at the decision-consequence level ---


def test_chat_idea_request_never_starts_a_job():
    """'ช่วยคิดไอเดียโปสเตอร์รับสมัครนักศึกษา' -- discussion/ideation only -> CHAT."""
    step = decide_next_step(
        _decision(Intent.CHAT, reason_code=ReasonCode.QUESTION_OR_DISCUSSION)
    )
    assert isinstance(step, RespondChat)


def test_chat_copywriting_request_never_starts_a_job():
    """'เขียน headline โปสเตอร์ให้สามแบบ' -- copywriting only, no execution -> CHAT."""
    step = decide_next_step(
        _decision(Intent.CHAT, reason_code=ReasonCode.QUESTION_OR_DISCUSSION)
    )
    assert isinstance(step, RespondChat)


def test_general_image_cat_in_spacesuit_uses_local_path():
    """'สร้างภาพแมวนั่งในยานอวกาศ' -> GENERAL_IMAGE, local/free, no confirmation."""
    step = decide_next_step(
        _decision(
            Intent.GENERAL_IMAGE,
            normalized_prompt="a cat sitting in a spaceship",
            reason_code=ReasonCode.GENERAL_VISUAL_REQUEST,
        )
    )
    assert isinstance(step, EnqueueGeneralImage)


def test_general_image_background_for_poster_is_still_local():
    """'สร้างภาพนักศึกษาใช้เป็นพื้นหลังโปสเตอร์' -- a visual asset without structured
    info, even though it mentions "poster" -> still GENERAL_IMAGE, not upgraded to paid
    just because a poster was mentioned (spec: never upgrade merely for mentioning
    marketing/an event/an organization)."""
    step = decide_next_step(
        _decision(Intent.GENERAL_IMAGE, reason_code=ReasonCode.GENERAL_VISUAL_REQUEST)
    )
    assert isinstance(step, EnqueueGeneralImage)


def test_poster_with_all_fields_enqueues_immediately():
    """'ทำโปสเตอร์ Open House พร้อมวันเวลาและ QR' -> POSTER, complete -> enqueued
    immediately as a paid job (no confirmation step per the 2026-08 product decision)."""
    step = decide_next_step(
        _decision(
            Intent.POSTER,
            exact_text=["Open House"],
            reason_code=ReasonCode.STRUCTURED_PROMOTIONAL_LAYOUT,
        )
    )
    assert isinstance(step, EnqueuePaidImage)
    assert step.action_type == "poster"
    assert step.billing_category == "paid"


def test_infographic_with_all_fields_enqueues_immediately():
    """'ทำอินโฟกราฟิกขั้นตอนสมัครเรียนห้าขั้นตอน' -> INFOGRAPHIC, complete -> enqueued
    immediately."""
    step = decide_next_step(
        _decision(Intent.INFOGRAPHIC, reason_code=ReasonCode.STRUCTURED_INFORMATION_DESIGN)
    )
    assert isinstance(step, EnqueuePaidImage)
    assert step.action_type == "infographic"
    assert step.billing_category == "paid"


def test_ambiguous_promotional_image_request_asks_for_clarification():
    """'ทำภาพโปรโมต Open House ให้หน่อย' -- ambiguous whether GENERAL_IMAGE or POSTER,
    and choosing wrong would change billing -> CLARIFICATION, no job of any kind. This is
    the LLM's OWN classification choice (Intent.CLARIFICATION), not the removed
    missing_fields safety net -- genuinely ambiguous deliverable type still asks."""
    step = decide_next_step(
        _decision(
            Intent.CLARIFICATION,
            clarification_question=(
                "ต้องการเป็นภาพทั่วไปจาก ComfyUI หรือเป็นโปสเตอร์ที่มีหัวข้อและข้อมูลกิจกรรมอ่านได้?"
            ),
            reason_code=ReasonCode.AMBIGUOUS_VISUAL_DELIVERABLE,
        )
    )
    assert isinstance(step, RespondClarification)
    assert step.question


def test_english_poster_with_venue_and_cta_enqueues_immediately():
    """'Generate an event poster with venue and CTA' -> POSTER -> enqueued immediately."""
    step = decide_next_step(
        _decision(Intent.POSTER, reason_code=ReasonCode.STRUCTURED_PROMOTIONAL_LAYOUT)
    )
    assert isinstance(step, EnqueuePaidImage) and step.action_type == "poster"


def test_english_illustrated_background_uses_local_path():
    """'Create an illustrated background for an event' -> GENERAL_IMAGE (visual asset,
    no readable info hierarchy required) -> local."""
    step = decide_next_step(
        _decision(Intent.GENERAL_IMAGE, reason_code=ReasonCode.GENERAL_VISUAL_REQUEST)
    )
    assert isinstance(step, EnqueueGeneralImage)


def test_explain_what_an_infographic_is_stays_chat():
    """'Explain what an infographic is' -- a question about the concept, not a request
    to generate one -> CHAT, never starts an INFOGRAPHIC job."""
    step = decide_next_step(
        _decision(Intent.CHAT, reason_code=ReasonCode.QUESTION_OR_DISCUSSION)
    )
    assert isinstance(step, RespondChat)


# --- Structural invariants ---


def test_chat_and_clarification_never_produce_any_generation_step():
    for intent in (Intent.CHAT, Intent.CLARIFICATION):
        step = decide_next_step(_decision(intent))
        assert not isinstance(step, (EnqueueGeneralImage, EnqueuePaidImage))


def test_general_image_never_produces_a_paid_step():
    step = decide_next_step(_decision(Intent.GENERAL_IMAGE))
    assert not isinstance(step, EnqueuePaidImage)


def test_poster_or_infographic_never_produce_direct_local_enqueue():
    """Backend must never "downgrade" a poster/infographic into a free local
    generation to dodge cost -- it must always be billed as "paid", even though it now
    enqueues immediately rather than waiting for a confirm click."""
    for intent in (Intent.POSTER, Intent.INFOGRAPHIC):
        step = decide_next_step(_decision(intent))
        assert not isinstance(step, EnqueueGeneralImage)
        assert isinstance(step, EnqueuePaidImage)
        assert step.billing_category == "paid"


def test_poster_with_missing_fields_still_enqueues_immediately():
    """2026-08 product decision: even with non-empty missing_fields, POSTER/INFOGRAPHIC
    no longer downgrades to a chat clarification -- it enqueues immediately. The
    best-effort research step (app/api/v1/chat_router.py:_research_augment) is what's
    responsible for trying to fill missing_fields with real facts BEFORE
    decide_next_step ever runs; decide_next_step itself no longer gates on the outcome."""
    step = decide_next_step(
        _decision(
            Intent.POSTER,
            missing_fields=["event date", "location"],
            reason_code=ReasonCode.STRUCTURED_PROMOTIONAL_LAYOUT,
        )
    )
    assert isinstance(step, EnqueuePaidImage)
    assert step.action_type == "poster"


def test_infographic_with_missing_fields_still_enqueues_immediately():
    step = decide_next_step(
        _decision(
            Intent.INFOGRAPHIC,
            missing_fields=["number of steps"],
            reason_code=ReasonCode.STRUCTURED_INFORMATION_DESIGN,
        )
    )
    assert isinstance(step, EnqueuePaidImage)


def test_clarification_falls_back_to_default_question_when_llm_omits_one():
    step = decide_next_step(_decision(Intent.CLARIFICATION, clarification_question=None))
    assert isinstance(step, RespondClarification)
    assert step.question  # never empty/None -- always something to show the user
