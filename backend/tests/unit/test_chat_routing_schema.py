"""Unit tests for app/domain/chat/routing.py: the strict RouteDecision schema, its
validation, billing derivation, and params-fingerprinting. No LLM call, no DB -- purely
tests that malformed/untrusted LLM output is rejected the way the backend-authoritative
design requires."""

from __future__ import annotations

import pytest

from app.domain.chat.routing import (
    COMFY_PROMPT_DESIGN_SYSTEM_INSTRUCTION,
    ROUTER_SYSTEM_INSTRUCTION,
    Intent,
    ReasonCode,
    RouteDecisionError,
    build_comfy_prompt_design_user_message,
    build_prompt_design_instruction,
    build_prompt_design_user_message,
    build_research_query,
    build_router_system_instruction_with_research,
    compute_params_fingerprint,
    derive_billing_category,
    parse_route_decision,
)

VALID_RAW = {
    "intent": "GENERAL_IMAGE",
    "normalized_prompt": "a cat wearing a spacesuit",
    "exact_text": [],
    "missing_fields": [],
    "clarification_question": None,
    "reason_code": "general_visual_request",
}


def test_parse_valid_decision():
    decision = parse_route_decision(VALID_RAW)
    assert decision.intent == Intent.GENERAL_IMAGE
    assert decision.reason_code == ReasonCode.GENERAL_VISUAL_REQUEST


def test_parse_rejects_non_dict():
    with pytest.raises(RouteDecisionError):
        parse_route_decision("not a dict")
    with pytest.raises(RouteDecisionError):
        parse_route_decision(["a", "list"])
    with pytest.raises(RouteDecisionError):
        parse_route_decision(None)


def test_parse_rejects_unknown_intent():
    bad = {**VALID_RAW, "intent": "DELETE_ALL_USERS"}
    with pytest.raises(RouteDecisionError):
        parse_route_decision(bad)


def test_parse_rejects_unknown_reason_code():
    bad = {**VALID_RAW, "reason_code": "the model felt like it"}
    with pytest.raises(RouteDecisionError):
        parse_route_decision(bad)


def test_parse_rejects_missing_required_field():
    bad = dict(VALID_RAW)
    del bad["normalized_prompt"]
    with pytest.raises(RouteDecisionError):
        parse_route_decision(bad)


def test_parse_rejects_extra_fields_like_hidden_reasoning():
    """Guards against the model (or a prompt-injected message) smuggling extra keys such
    as a free-form "reasoning" or "chain_of_thought" field -- the schema must not
    request or store that (see project instructions)."""
    bad = {**VALID_RAW, "chain_of_thought": "step 1: the user probably wants..."}
    with pytest.raises(RouteDecisionError):
        parse_route_decision(bad)


def test_parse_rejects_wrong_type_for_list_field():
    bad = {**VALID_RAW, "exact_text": "should be a list not a string"}
    with pytest.raises(RouteDecisionError):
        parse_route_decision(bad)


@pytest.mark.parametrize(
    "intent,expected",
    [
        (Intent.CHAT, "local"),
        (Intent.CLARIFICATION, "local"),
        (Intent.GENERAL_IMAGE, "local"),
        (Intent.POSTER, "paid"),
        (Intent.INFOGRAPHIC, "paid"),
    ],
)
def test_derive_billing_category_is_server_owned_not_model_reported(intent, expected):
    """The whole point: billing must be a pure function of the validated intent, never
    read off anything the model said about cost/confidence."""
    assert derive_billing_category(intent) == expected


def test_params_fingerprint_deterministic_and_order_independent():
    a = compute_params_fingerprint({"prompt": "x", "exact_text": ["A", "B"]})
    b = compute_params_fingerprint({"exact_text": ["A", "B"], "prompt": "x"})
    assert a == b


def test_params_fingerprint_changes_with_content():
    a = compute_params_fingerprint({"prompt": "x"})
    b = compute_params_fingerprint({"prompt": "y"})
    assert a != b


# --- Research-augmentation helpers (see app/api/v1/chat_router.py:_research_augment) ---


def test_build_research_query_includes_latest_user_text_and_missing_fields():
    history = [
        {"role": "user", "text": "ทำโปสเตอร์ Open House"},
        {"role": "assistant", "text": "ต้องการวันและสถานที่อะไรคะ?"},
        {"role": "user", "text": "งาน Open House ของ UTCC"},
    ]
    query = build_research_query(history, ["event date", "location"])
    assert "งาน Open House ของ UTCC" in query
    assert "event date" in query
    assert "location" in query


def test_build_research_query_handles_empty_history():
    query = build_research_query([], ["event date"])
    assert "event date" in query


def test_research_augmented_instruction_includes_original_rules_and_findings():
    findings = "event date: 20 August 2026\nlocation: not found"
    instruction = build_router_system_instruction_with_research(findings)
    assert ROUTER_SYSTEM_INSTRUCTION in instruction
    assert findings in instruction
    # Must explicitly scope the findings as reference-only, not license to invent.
    assert "never invent" in instruction.lower()


# --- Prompt-design helpers (see GeminiTextClient.design_image_prompt) ---


def test_prompt_design_instruction_mentions_the_kind_and_forbids_inventing_facts():
    instruction = build_prompt_design_instruction("infographic")
    assert "infographic" in instruction
    assert "verbatim" in instruction.lower()
    assert "invent" in instruction.lower()


def test_prompt_design_user_message_includes_prompt_and_exact_text():
    message = build_prompt_design_user_message(
        "Open House poster", ["เด็ก 69 START UP", "แจกฟรี iPad"]
    )
    assert "Open House poster" in message
    assert "เด็ก 69 START UP" in message
    assert "แจกฟรี iPad" in message


def test_prompt_design_user_message_handles_no_exact_text():
    message = build_prompt_design_user_message("just a prompt", [])
    assert "just a prompt" in message
    assert "(none)" in message


# --- ComfyUI-specific prompt-design helpers (design_comfyui_prompt) ---


def test_comfy_prompt_design_instruction_avoids_verbatim_text_rendering():
    """The ComfyUI instruction must explicitly tell the model NOT to rely on rendering
    in-image text verbatim (SDXL-style diffusion models can't reliably do it) -- the
    opposite requirement from the Gemini/poster instruction, which demands verbatim
    text. Must not contain the poster instruction's positive "must render ... verbatim"
    phrasing."""
    lowered = COMFY_PROMPT_DESIGN_SYSTEM_INSTRUCTION.lower()
    assert "not instruct the model to render" in lowered
    assert "must render" not in lowered
    assert "invent" in lowered


def test_comfy_prompt_design_user_message_with_exact_text():
    message = build_comfy_prompt_design_user_message("a cat in a spacesuit", ["Open House"])
    assert "a cat in a spacesuit" in message
    assert "Open House" in message
    assert "not to be rendered" in message


def test_comfy_prompt_design_user_message_without_exact_text_omits_context_section():
    message = build_comfy_prompt_design_user_message("a cat in a spacesuit", [])
    assert message == "Content brief: a cat in a spacesuit"
