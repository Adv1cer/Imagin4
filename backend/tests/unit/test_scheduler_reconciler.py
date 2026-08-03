"""Unit tests for the scheduler/reconciler process loops against the in-memory fakes."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.comfyui import MockComfyUIClient
from app.adapters.queue import InMemoryJobQueue, QueuedJob
from app.services.reconciler import Reconciler
from app.services.scheduler import Scheduler


def _job(**overrides) -> QueuedJob:
    defaults = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind="txt2img_basic",
        state="queued",
        priority=0,
        effective_priority=0.0,
        input_payload={"prompt": "a cat"},
        idempotency_key=str(uuid.uuid4()),
        queued_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return QueuedJob(**defaults)


@pytest.mark.asyncio
async def test_scheduler_claims_and_dispatches_queued_job():
    queue = InMemoryJobQueue()
    job = _job()
    await queue.enqueue(job)
    comfy = MockComfyUIClient(polls_to_complete=0)
    scheduler = Scheduler(job_queue=queue, comfy_client=comfy, poll_interval_s=0.01)

    await scheduler._tick()
    # dispatch is fire-and-forget via asyncio.create_task; wait for it to finish.
    if scheduler._inflight:
        import asyncio

        await asyncio.gather(*list(scheduler._inflight))

    updated = await queue.get(job.id)
    assert updated.state == "running"
    assert updated.prompt_id is not None
    assert updated.lease_owner == scheduler.owner_id


@pytest.mark.asyncio
async def test_scheduler_capacity_defaults_to_settings_without_session_factory():
    queue = InMemoryJobQueue()
    comfy = MockComfyUIClient()
    scheduler = Scheduler(job_queue=queue, comfy_client=comfy)
    capacity = await scheduler._reserve_capacity()
    assert capacity == scheduler.settings.default_comfy_active_slots


@pytest.mark.asyncio
async def test_scheduler_graceful_shutdown_drains_inflight():
    queue = InMemoryJobQueue()
    job = _job()
    await queue.enqueue(job)
    comfy = MockComfyUIClient()
    scheduler = Scheduler(job_queue=queue, comfy_client=comfy, poll_interval_s=0.01)

    await scheduler._tick()
    scheduler.request_shutdown()
    await scheduler.run_forever()  # should return promptly, draining in-flight dispatch

    updated = await queue.get(job.id)
    assert updated.state == "running"


@pytest.mark.asyncio
async def test_reconciler_finalizes_succeeded_job_with_known_prompt_id():
    queue = InMemoryJobQueue()
    job = _job(state="running")
    await queue.enqueue(job)
    comfy = MockComfyUIClient(polls_to_complete=0)
    submit = await comfy.submit(job.input_payload)
    job.prompt_id = submit.prompt_id

    reconciler = Reconciler(job_queue=queue, comfy_client=comfy, poll_interval_s=0.01)
    examined = await reconciler.run_once()

    assert examined == 1
    updated = await queue.get(job.id)
    assert updated.state == "succeeded"
    assert updated.result is not None


@pytest.mark.asyncio
async def test_reconciler_retries_expired_lease_with_no_prompt_id():
    queue = InMemoryJobQueue()
    job = _job(state="dispatched", current_attempt=0, max_attempts=3)
    job.lease_owner = "scheduler-x"
    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await queue.enqueue(job)
    comfy = MockComfyUIClient()

    reconciler = Reconciler(job_queue=queue, comfy_client=comfy, poll_interval_s=0.01)
    await reconciler.run_once()

    updated = await queue.get(job.id)
    assert updated.state == "retry_wait"
    assert updated.error_code == "worker_lease_expired"


@pytest.mark.asyncio
async def test_reconciler_fails_job_when_retries_exhausted():
    queue = InMemoryJobQueue()
    job = _job(state="dispatched", current_attempt=2, max_attempts=3)
    job.lease_owner = "scheduler-x"
    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await queue.enqueue(job)
    comfy = MockComfyUIClient()

    reconciler = Reconciler(job_queue=queue, comfy_client=comfy, poll_interval_s=0.01)
    await reconciler.run_once()

    updated = await queue.get(job.id)
    assert updated.state == "failed"


@pytest.mark.asyncio
async def test_reconciler_graceful_shutdown_flag():
    queue = InMemoryJobQueue()
    comfy = MockComfyUIClient()
    reconciler = Reconciler(job_queue=queue, comfy_client=comfy, poll_interval_s=0.01)
    reconciler.request_shutdown()
    await reconciler.run_forever()  # returns immediately since shutdown already requested
    assert reconciler._shutdown.is_set()
