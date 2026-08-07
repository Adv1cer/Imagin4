"""Unit tests for app/adapters/gemini.py:_build_image_prompt and
GeminiImageComfyUIClient.submit() -- covers a real bug found in production: the image
generation call only ever read workflow_payload["prompt"] and silently dropped
"exact_text" (the literal copy -- campaign name, offer details, dates, contact info --
that app/domain/chat/routing.py's RouteDecision.exact_text and the research step exist
to get right). The generated poster/infographic then had nothing to go on but a generic
prompt and came out generic/factually wrong even when routing correctly extracted the
real details."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.gemini import GeminiImageComfyUIClient, _build_image_prompt
from app.adapters.storage import InMemoryObjectStorage


def test_build_image_prompt_with_no_exact_text_returns_bare_prompt():
    assert _build_image_prompt({"prompt": "a poster about an event"}) == "a poster about an event"


def test_build_image_prompt_with_missing_exact_text_key_returns_bare_prompt():
    assert _build_image_prompt({"prompt": "x", "exact_text": []}) == "x"


def test_build_image_prompt_includes_exact_text_verbatim_instruction():
    result = _build_image_prompt(
        {
            "prompt": "Open House poster for UTCC",
            "exact_text": ["เด็ก 69 START UP", "แจกฟรี iPad พร้อม Canva Pro"],
        }
    )
    assert "Open House poster for UTCC" in result
    assert "เด็ก 69 START UP" in result
    assert "แจกฟรี iPad พร้อม Canva Pro" in result
    assert "verbatim" in result.lower()


def test_build_image_prompt_filters_blank_and_non_string_exact_text_entries():
    result = _build_image_prompt(
        {"prompt": "x", "exact_text": ["real text", "", "   ", None, 123]}
    )
    assert "real text" in result
    # No stray blank bullet lines from filtered-out entries.
    assert "-\n" not in result and not result.rstrip().endswith("- ")


def test_build_image_prompt_handles_missing_prompt_key():
    result = _build_image_prompt({"exact_text": ["only this"]})
    assert "only this" in result


@pytest.mark.asyncio
async def test_submit_sends_prompt_and_exact_text_to_the_sdk(monkeypatch):
    captured: dict = {}

    def fake_generate_content(*, model, contents, config):
        captured["contents"] = contents
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                inline_data=SimpleNamespace(data=b"fake-png-bytes", mime_type="image/png")
                            )
                        ]
                    )
                )
            ]
        )

    client = GeminiImageComfyUIClient(
        api_key="fake-test-key",
        model="gemini-3.1-flash-image",
        storage=InMemoryObjectStorage(),
        timeout_s=5.0,
    )
    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)

    result = await client.submit(
        {
            "prompt": "Open House poster for UTCC",
            "exact_text": ["เด็ก 69 START UP", "แจกฟรี iPad พร้อม Canva Pro"],
        },
        kind="poster_infographic",
    )

    status = await client.get_status(result.prompt_id)
    assert status.state == "succeeded"
    assert "เด็ก 69 START UP" in captured["contents"]
    assert "แจกฟรี iPad พร้อม Canva Pro" in captured["contents"]
    assert "Open House poster for UTCC" in captured["contents"]


# --- prompt_designer integration: the "have Gemini design a good prompt before sending
# to the image model" step. See GeminiTextClient.design_image_prompt in gemini.py and
# app/domain/chat/routing.py's PROMPT_DESIGN_SYSTEM_INSTRUCTION_TEMPLATE. ---


def _fake_image_response() -> SimpleNamespace:
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            inline_data=SimpleNamespace(data=b"fake-png-bytes", mime_type="image/png")
                        )
                    ]
                )
            )
        ]
    )


@pytest.mark.asyncio
async def test_submit_uses_prompt_designer_output_when_available(monkeypatch):
    captured: dict = {}

    def fake_generate_content(*, model, contents, config):
        captured["contents"] = contents
        return _fake_image_response()

    async def fake_prompt_designer(prompt: str, exact_text: list[str], kind: str) -> str:
        captured["designer_call"] = (prompt, exact_text, kind)
        return "A beautifully designed layout with bold typography and a warm color palette."

    client = GeminiImageComfyUIClient(
        api_key="fake-test-key",
        model="gemini-3.1-flash-image",
        storage=InMemoryObjectStorage(),
        timeout_s=5.0,
        prompt_designer=fake_prompt_designer,
    )
    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)

    await client.submit(
        {
            "prompt": "Open House poster",
            "exact_text": ["เด็ก 69 START UP"],
            "action_type": "poster",
        }
    )

    assert "beautifully designed layout" in captured["contents"]
    # Belt-and-suspenders: exact_text must survive even though the designer already
    # produced its own text -- the verbatim block is appended unconditionally.
    assert "เด็ก 69 START UP" in captured["contents"]
    assert captured["designer_call"] == ("Open House poster", ["เด็ก 69 START UP"], "poster")


@pytest.mark.asyncio
async def test_submit_falls_back_to_baseline_prompt_when_designer_fails(monkeypatch):
    captured: dict = {}

    def fake_generate_content(*, model, contents, config):
        captured["contents"] = contents
        return _fake_image_response()

    async def failing_designer(prompt, exact_text, kind):
        raise RuntimeError("gemini_error:TimeoutError")

    client = GeminiImageComfyUIClient(
        api_key="fake-test-key",
        model="gemini-3.1-flash-image",
        storage=InMemoryObjectStorage(),
        timeout_s=5.0,
        prompt_designer=failing_designer,
    )
    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)

    result = await client.submit(
        {"prompt": "Open House poster", "exact_text": ["เด็ก 69 START UP"]}
    )
    status = await client.get_status(result.prompt_id)

    # Never fatal -- falls back to the baseline prompt and still succeeds.
    assert status.state == "succeeded"
    assert "Open House poster" in captured["contents"]
    assert "เด็ก 69 START UP" in captured["contents"]


@pytest.mark.asyncio
async def test_submit_uses_action_type_for_design_kind_not_workflow_name(monkeypatch):
    """job.kind (passed as the `kind` positional arg) is the workflow name
    ("poster_infographic") -- the design step must use the more specific
    workflow_payload["action_type"] ("poster"/"infographic") instead, not the generic
    workflow identifier."""
    captured: dict = {}

    def fake_generate_content(*, model, contents, config):
        return _fake_image_response()

    async def fake_prompt_designer(prompt, exact_text, kind):
        captured["kind"] = kind
        return "designed prompt"

    client = GeminiImageComfyUIClient(
        api_key="fake-test-key",
        model="gemini-3.1-flash-image",
        storage=InMemoryObjectStorage(),
        timeout_s=5.0,
        prompt_designer=fake_prompt_designer,
    )
    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)

    await client.submit(
        {"prompt": "x", "exact_text": [], "action_type": "infographic"},
        kind="poster_infographic",
    )
    assert captured["kind"] == "infographic"


@pytest.mark.asyncio
async def test_design_image_prompt_sends_content_brief_and_exact_text(monkeypatch):
    from app.adapters.gemini import GeminiTextClient

    captured: dict = {}

    def fake_generate_content(*, model, contents, config):
        captured["contents"] = contents
        captured["system_instruction"] = config.system_instruction
        return SimpleNamespace(text="a lovely designed prompt")

    client = GeminiTextClient(api_key="fake-test-key", model="gemini-3.6-flash", timeout_s=5.0)
    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)

    result = await client.design_image_prompt(
        "Open House poster", ["เด็ก 69 START UP"], kind="poster"
    )
    assert result == "a lovely designed prompt"
    assert "Open House poster" in captured["contents"][0]["parts"][0]["text"]
    assert "เด็ก 69 START UP" in captured["contents"][0]["parts"][0]["text"]
    assert "poster" in captured["system_instruction"]
    # No response_schema/tools -- this is a plain text call.
    assert captured["system_instruction"] is not None


@pytest.mark.asyncio
async def test_design_image_prompt_raises_sanitized_error_on_failure(monkeypatch):
    from app.adapters.gemini import GeminiTextClient

    def fake_generate_content(*, model, contents, config):
        raise RuntimeError("raw internal detail")

    client = GeminiTextClient(api_key="fake-test-key", model="gemini-3.6-flash", timeout_s=5.0)
    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)

    with pytest.raises(RuntimeError) as exc_info:
        await client.design_image_prompt("x", [], kind="poster")
    assert str(exc_info.value) == "gemini_error:RuntimeError"
