"""Tests for the wait=true polling helper added to app/api/v1/agent_router.py -- the
server-side "block until the job finishes" mode built specifically for callers (like a
no-code chatbot workflow builder with no retry/wait/loop node) that can't poll
GET /v1/jobs/{id} themselves. Uses the real InMemoryJobQueue (no DB needed -- this
helper only touches the JobQueue port).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from app.adapters.queue import InMemoryJobQueue, QueuedJob
from app.api.v1 import agent_router
from app.api.v1.agent_router import _wait_for_terminal_state


def _make_job(state: str) -> QueuedJob:
    return QueuedJob(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind="image_basic",
        state=state,
        priority=0,
        effective_priority=0.0,
        input_payload={"prompt": "a cat"},
        idempotency_key="k1",
        queued_at=datetime.now(timezone.utc),
    )


async def test_already_terminal_job_returns_immediately_without_polling():
    queue = InMemoryJobQueue()
    job = _make_job("succeeded")
    await queue.enqueue(job)

    result = await _wait_for_terminal_state(queue, job.id, timeout_s=10.0)

    assert result is not None
    assert result.state == "succeeded"


async def test_times_out_and_returns_last_known_non_terminal_state():
    queue = InMemoryJobQueue()
    job = _make_job("queued")
    await queue.enqueue(job)

    result = await _wait_for_terminal_state(queue, job.id, timeout_s=0.05)

    assert result is not None
    assert result.state == "queued"


async def test_polls_until_a_concurrent_transition_reaches_succeeded(monkeypatch):
    monkeypatch.setattr(agent_router, "WAIT_POLL_INTERVAL_S", 0.01)
    queue = InMemoryJobQueue()
    job = _make_job("running")
    await queue.enqueue(job)

    async def _finish_soon():
        await asyncio.sleep(0.03)
        await queue.mark_succeeded(job.id, {"outputs": [{"object_key": "x"}]})

    finisher = asyncio.create_task(_finish_soon())
    result = await _wait_for_terminal_state(queue, job.id, timeout_s=5.0)
    await finisher

    assert result is not None
    assert result.state == "succeeded"


async def test_returns_none_if_job_vanishes_from_queue():
    queue = InMemoryJobQueue()
    result = await _wait_for_terminal_state(queue, uuid.uuid4(), timeout_s=0.05)
    assert result is None


async def test_failed_job_is_terminal_and_carries_error_fields():
    queue = InMemoryJobQueue()
    job = _make_job("running")
    await queue.enqueue(job)
    await queue.mark_failed(job.id, "comfy_transient", "gemini_error:ClientError")

    result = await _wait_for_terminal_state(queue, job.id, timeout_s=10.0)

    assert result is not None
    assert result.state == "failed"
    assert result.error_code == "comfy_transient"
    assert result.error_detail == "gemini_error:ClientError"


def test_wait_timeout_is_clamped_between_min_and_max():
    assert agent_router.WAIT_MIN_TIMEOUT_S < agent_router.WAIT_DEFAULT_TIMEOUT_S
    assert agent_router.WAIT_DEFAULT_TIMEOUT_S < agent_router.WAIT_MAX_TIMEOUT_S


def test_agent_message_in_accepts_wait_fields_with_sane_defaults():
    from app.api.v1.agent_router import AgentMessageIn

    payload = AgentMessageIn(external_conversation_id="u1", text="hi")
    assert payload.wait is False
    assert payload.wait_timeout_s is None

    payload2 = AgentMessageIn(
        external_conversation_id="u1", text="hi", wait=True, wait_timeout_s=30
    )
    assert payload2.wait is True
    assert payload2.wait_timeout_s == 30
