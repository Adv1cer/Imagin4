"""Regression test for the 2026-08-20 fix (found via the /admin load-test tool's table
showing state=succeeded next to a red comfy_transient error): mark_succeeded must clear
error_code/error_detail left over from an earlier FAILED attempt of the same job, not
just flip state -- otherwise a job that failed once then succeeded on retry keeps
showing a stale error forever."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.adapters.queue import InMemoryJobQueue, QueuedJob


def _job():
    return QueuedJob(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind="image_basic",
        state="queued",
        priority=0,
        effective_priority=0.0,
        input_payload={},
        idempotency_key=str(uuid.uuid4()),
        queued_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_mark_succeeded_clears_stale_error_from_earlier_retry():
    queue = InMemoryJobQueue()
    job = _job()
    await queue.enqueue(job)

    # Simulate: claimed, failed transiently, retried.
    await queue.mark_running(job.id)
    await queue.mark_retry_wait(job.id, "comfy_transient", "comfy_live_error:ConnectError")
    stored = await queue.get(job.id)
    assert stored.error_code == "comfy_transient"
    assert stored.error_detail == "comfy_live_error:ConnectError"

    # Retried attempt succeeds.
    await queue.mark_running(job.id)
    await queue.mark_succeeded(job.id, {"outputs": [{"object_key": "x"}]})

    final = await queue.get(job.id)
    assert final.state == "succeeded"
    assert final.error_code is None
    assert final.error_detail is None


@pytest.mark.asyncio
async def test_mark_succeeded_is_still_a_noop_once_terminal():
    queue = InMemoryJobQueue()
    job = _job()
    await queue.enqueue(job)
    await queue.mark_running(job.id)
    await queue.mark_failed(job.id, "comfy_permanent", "bad prompt")

    # A late/duplicate mark_succeeded call must NOT resurrect a failed job or touch its
    # error fields -- same terminal-state guard as before this fix.
    await queue.mark_succeeded(job.id, {"outputs": []})
    final = await queue.get(job.id)
    assert final.state == "failed"
    assert final.error_code == "comfy_permanent"
