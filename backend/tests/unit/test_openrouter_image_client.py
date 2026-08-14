"""Unit/contract tests for app/adapters/openrouter.py:OpenRouterImageComfyUIClient --
mirrors tests/unit/test_gemini_image_client.py's coverage (exact_text handling, prompt
designer integration/fallback) plus OpenRouter's own request/response shape (JSON POST to
{base_url}/images, base64 data[].b64_json response), using httpx.MockTransport instead of
monkeypatching an SDK client (OpenRouter has no SDK here, just plain httpx)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.adapters.openrouter import OpenRouterImageComfyUIClient
from app.adapters.storage import InMemoryObjectStorage

BASE_URL = "https://openrouter.test/api/v1"
FAKE_PNG_B64 = base64.b64encode(b"fake-png-bytes").decode("ascii")


def _client(handler, storage: InMemoryObjectStorage | None = None, **kwargs) -> OpenRouterImageComfyUIClient:
    return OpenRouterImageComfyUIClient(
        api_key="fake-test-key",
        model="google/gemini-3-pro-image-preview",
        storage=storage or InMemoryObjectStorage(),
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _image_response(b64_json: str = FAKE_PNG_B64, media_type: str = "image/png") -> httpx.Response:
    return httpx.Response(
        200, json={"created": 1234567890, "data": [{"b64_json": b64_json, "media_type": media_type}]}
    )


@pytest.mark.asyncio
async def test_submit_posts_prompt_with_bearer_auth_and_stores_decoded_image() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/images"
        assert request.headers["authorization"] == "Bearer fake-test-key"
        captured["body"] = json.loads(request.content)
        return _image_response()

    storage = InMemoryObjectStorage()
    client = _client(handler, storage=storage)

    result = await client.submit({"prompt": "Open House poster for UTCC"})
    status = await client.get_status(result.prompt_id)

    assert captured["body"]["model"] == "google/gemini-3-pro-image-preview"
    assert "Open House poster for UTCC" in captured["body"]["prompt"]
    assert status.state == "succeeded"
    assert len(status.outputs) == 1
    stored = await storage.get_object(status.outputs[0]["object_key"])
    assert stored == b"fake-png-bytes"


@pytest.mark.asyncio
async def test_submit_includes_exact_text_verbatim_instruction() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _image_response()

    client = _client(handler)
    await client.submit(
        {
            "prompt": "Open House poster for UTCC",
            "exact_text": ["เด็ก 69 START UP", "แจกฟรี iPad พร้อม Canva Pro"],
        }
    )

    prompt_sent = captured["body"]["prompt"]
    assert "เด็ก 69 START UP" in prompt_sent
    assert "แจกฟรี iPad พร้อม Canva Pro" in prompt_sent
    assert "verbatim" in prompt_sent.lower()


@pytest.mark.asyncio
async def test_submit_empty_prompt_and_no_exact_text_fails_without_calling_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should never be called for an empty prompt")

    client = _client(handler)
    result = await client.submit({"prompt": ""})
    status = await client.get_status(result.prompt_id)

    assert status.state == "failed"
    assert status.error == "empty_prompt"


@pytest.mark.asyncio
async def test_submit_uses_prompt_designer_output_when_available() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _image_response()

    async def fake_prompt_designer(prompt: str, exact_text: list[str], kind: str) -> str:
        captured["designer_call"] = (prompt, exact_text, kind)
        return "A beautifully designed layout with bold typography."

    client = _client(handler, prompt_designer=fake_prompt_designer)
    await client.submit(
        {"prompt": "Open House poster", "exact_text": ["เด็ก 69 START UP"], "action_type": "poster"}
    )

    assert "beautifully designed layout" in captured["body"]["prompt"]
    # Belt-and-suspenders: exact_text survives even though the designer already produced
    # its own text.
    assert "เด็ก 69 START UP" in captured["body"]["prompt"]
    assert captured["designer_call"] == ("Open House poster", ["เด็ก 69 START UP"], "poster")


@pytest.mark.asyncio
async def test_submit_falls_back_to_baseline_prompt_when_designer_fails() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _image_response()

    async def failing_designer(prompt, exact_text, kind):
        raise RuntimeError("openrouter_error:TimeoutError")

    client = _client(handler, prompt_designer=failing_designer)
    result = await client.submit({"prompt": "Open House poster", "exact_text": ["เด็ก 69 START UP"]})
    status = await client.get_status(result.prompt_id)

    assert status.state == "succeeded"
    assert "Open House poster" in captured["body"]["prompt"]
    assert "เด็ก 69 START UP" in captured["body"]["prompt"]


@pytest.mark.asyncio
async def test_submit_502_response_maps_to_openrouter_overloaded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": {"message": "upstream provider overloaded"}})

    client = _client(handler)
    result = await client.submit({"prompt": "a poster"})
    status = await client.get_status(result.prompt_id)

    assert status.state == "failed"
    assert status.error == "openrouter_overloaded"


@pytest.mark.asyncio
async def test_submit_no_image_in_response_fails_cleanly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"created": 1, "data": []})

    client = _client(handler)
    result = await client.submit({"prompt": "a poster"})
    status = await client.get_status(result.prompt_id)

    assert status.state == "failed"
    assert status.error == "openrouter_no_image_in_response"


@pytest.mark.asyncio
async def test_get_status_unknown_prompt_id_reports_failed() -> None:
    client = _client(lambda r: _image_response())
    status = await client.get_status("never-submitted")
    assert status.state == "failed"
    assert status.error == "unknown_prompt_id"


@pytest.mark.asyncio
async def test_health_does_not_call_the_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("health() must not call the network")

    client = _client(handler)
    assert await client.health() is True


@pytest.mark.asyncio
async def test_cancel_clears_cached_result_without_calling_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _image_response()

    client = _client(handler)
    result = await client.submit({"prompt": "a poster"})
    await client.cancel(result.prompt_id)
    status = await client.get_status(result.prompt_id)
    assert status.error == "unknown_prompt_id"
