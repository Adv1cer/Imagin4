"""Contract test for the ComfyUIClient port: asserts the mock adapter satisfies the same
submit -> poll -> terminal-state contract the real HTTP adapter must honor (deterministic
output keyed by input payload, forced-failure keyword, cancellation)."""

from __future__ import annotations

import pytest

from app.adapters.comfyui import MockComfyUIClient


@pytest.mark.asyncio
async def test_submit_then_immediate_success() -> None:
    client = MockComfyUIClient(polls_to_complete=0)
    result = await client.submit({"prompt": "a cat"})
    status = await client.get_status(result.prompt_id)
    assert status.state == "succeeded"
    assert status.outputs and status.outputs[0]["object_key"].startswith("generated/")


@pytest.mark.asyncio
async def test_same_payload_yields_same_output_key() -> None:
    client = MockComfyUIClient(polls_to_complete=0)
    r1 = await client.submit({"prompt": "same"})
    r2 = await client.submit({"prompt": "same"})
    s1 = await client.get_status(r1.prompt_id)
    s2 = await client.get_status(r2.prompt_id)
    assert s1.outputs[0]["object_key"] == s2.outputs[0]["object_key"]


@pytest.mark.asyncio
async def test_polls_to_complete_delays_terminal_state() -> None:
    client = MockComfyUIClient(polls_to_complete=2)
    result = await client.submit({"prompt": "slow"})
    first = await client.get_status(result.prompt_id)
    second = await client.get_status(result.prompt_id)
    third = await client.get_status(result.prompt_id)
    assert first.state == "running"
    assert second.state == "running"
    assert third.state == "succeeded"


@pytest.mark.asyncio
async def test_forced_failure_keyword() -> None:
    client = MockComfyUIClient()
    result = await client.submit({"prompt": "__force_fail__"})
    status = await client.get_status(result.prompt_id)
    assert status.state == "failed"
    assert status.error


@pytest.mark.asyncio
async def test_cancel_marks_failed() -> None:
    client = MockComfyUIClient()
    result = await client.submit({"prompt": "cancel me"})
    await client.cancel(result.prompt_id)
    status = await client.get_status(result.prompt_id)
    assert status.state == "failed"
    assert status.error == "cancelled"


@pytest.mark.asyncio
async def test_health_always_true() -> None:
    client = MockComfyUIClient()
    assert await client.health() is True
