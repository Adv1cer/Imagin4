"""Unit test for GeminiTextClient.route_intent() -- mocks the google-genai SDK call
itself (models.generate_content) so we exercise the real adapter code path (contents
mapping, GenerateContentConfig wiring, response.text -> json.loads) without any network
call or real API key.

There was no pre-existing pattern in this repo for mocking the Gemini SDK (grepped
tests/ for "GeminiTextClient|google.genai|genai.Client" before writing this -- no
matches), so this establishes one. GeminiTextClient.__init__ only constructs a
genai.Client object (no network I/O at construction time), so a fake API key is safe to
use here."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.gemini import GeminiTextClient


def _make_client() -> GeminiTextClient:
    return GeminiTextClient(api_key="fake-test-key", model="gemini-3.6-flash", timeout_s=5.0)


@pytest.mark.asyncio
async def test_route_intent_parses_structured_json_response(monkeypatch):
    raw_json = (
        '{"intent": "GENERAL_IMAGE", "normalized_prompt": "a cat in a spacesuit", '
        '"exact_text": [], "missing_fields": [], "clarification_question": null, '
        '"reason_code": "general_visual_request"}'
    )
    captured: dict = {}

    def fake_generate_content(*, model, contents, config):
        captured["model"] = model
        captured["contents"] = contents
        captured["config"] = config
        return SimpleNamespace(text=raw_json)

    client = _make_client()
    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)

    result = await client.route_intent([{"role": "user", "text": "สร้างภาพแมวในยานอวกาศ"}])

    assert result["intent"] == "GENERAL_IMAGE"
    assert result["reason_code"] == "general_visual_request"
    # Structured-output config must actually be requested -- this is what makes the LLM
    # output a strict schema rather than free text we'd have to parse leniently.
    assert captured["config"].response_mime_type == "application/json"
    assert captured["config"].response_schema is not None
    assert captured["contents"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_route_intent_raises_sanitized_error_on_sdk_failure(monkeypatch):
    def fake_generate_content(*, model, contents, config):
        raise RuntimeError("some raw internal detail that must not leak")

    client = _make_client()
    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)

    with pytest.raises(RuntimeError) as exc_info:
        await client.route_intent([{"role": "user", "text": "hello"}])

    # Sanitized: only the exception class name, never the raw message text.
    assert str(exc_info.value) == "gemini_error:RuntimeError"
    assert "raw internal detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_route_intent_raises_on_empty_history():
    client = _make_client()
    with pytest.raises(RuntimeError, match="EmptyHistory"):
        await client.route_intent([])


@pytest.mark.asyncio
async def test_route_intent_raises_on_malformed_json(monkeypatch):
    def fake_generate_content(*, model, contents, config):
        return SimpleNamespace(text="not valid json{{{")

    client = _make_client()
    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)

    with pytest.raises(RuntimeError):
        await client.route_intent([{"role": "user", "text": "hi"}])
