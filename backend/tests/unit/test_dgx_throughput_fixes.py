"""Regression tests for 2026-08-20 DGX Spark / agentflow throughput fixes."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.comfyui import ComfyStatus, MockComfyUIClient
from app.adapters.queue import InMemoryJobQueue, QueuedJob
from app.core.config import Settings
from app.domain.jobs.admission import CapacityExceededError, admit_generation_job
from app.services.reconciler import Reconciler
from app.services.scheduler import Scheduler


def _job(**overrides) -> QueuedJob:
    defaults = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "kind": "image_basic",
        "state": "queued",
        "priority": 0,
        "effective_priority": 0.0,
        "input_payload": {"prompt": "a cat"},
        "idempotency_key": str(uuid.uuid4()),
        "queued_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return QueuedJob(**defaults)


@pytest.mark.asyncio
async def test_scheduler_capacity_subtracts_in_flight_comfy_jobs():
    queue = InMemoryJobQueue()
    running = _job(state="running")
    await queue.enqueue(running)
    # Mark running without going through claim so count_active sees it.
    running.state = "running"

    settings = Settings(default_comfy_active_slots=1, comfy_pending_buffer=0)
    scheduler = Scheduler(
        job_queue=queue, comfy_client=MockComfyUIClient(), settings=settings
    )
    capacity = await scheduler._reserve_capacity_by_backend()
    assert capacity["comfyui"] == 0


@pytest.mark.asyncio
async def test_scheduler_pending_buffer_allows_one_extra_claim_slot():
    queue = InMemoryJobQueue()
    running = _job(state="running")
    await queue.enqueue(running)
    running.state = "running"

    settings = Settings(default_comfy_active_slots=1, comfy_pending_buffer=1)
    scheduler = Scheduler(
        job_queue=queue, comfy_client=MockComfyUIClient(), settings=settings
    )
    capacity = await scheduler._reserve_capacity_by_backend()
    assert capacity["comfyui"] == 1


@pytest.mark.asyncio
async def test_claim_skips_retry_wait_before_available_at():
    queue = InMemoryJobQueue()
    job = _job()
    await queue.enqueue(job)
    await queue.mark_running(job.id)
    await queue.mark_retry_wait(job.id, "comfy_transient", "blip", delay_seconds=60)

    claimed = await queue.claim_next(worker_capacity=1)
    assert claimed == []
    stored = await queue.get(job.id)
    assert stored.state == "retry_wait"
    assert stored.available_at is not None
    assert stored.available_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_claim_takes_retry_wait_after_available_at():
    queue = InMemoryJobQueue()
    job = _job()
    await queue.enqueue(job)
    await queue.mark_running(job.id)
    await queue.mark_retry_wait(job.id, "comfy_transient", "blip", delay_seconds=0)
    stored = await queue.get(job.id)
    stored.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    claimed = await queue.claim_next(worker_capacity=1)
    assert [j.id for j in claimed] == [job.id]


class _UnknownThenFailClient(MockComfyUIClient):
    async def get_status(self, prompt_id: str) -> ComfyStatus:
        return ComfyStatus(prompt_id=prompt_id, state="unknown")


@pytest.mark.asyncio
async def test_reconciler_leaves_unknown_prompt_until_lease_expires():
    queue = InMemoryJobQueue()
    job = _job(state="running")
    job.prompt_id = "missing-prompt"
    job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    await queue.enqueue(job)

    reconciler = Reconciler(
        job_queue=queue, comfy_client=_UnknownThenFailClient(), poll_interval_s=0.01
    )
    await reconciler.run_once()

    updated = await queue.get(job.id)
    assert updated.state == "running"


@pytest.mark.asyncio
async def test_reconciler_retries_unknown_prompt_after_lease_expires():
    queue = InMemoryJobQueue()
    job = _job(state="running", current_attempt=0, max_attempts=3)
    job.prompt_id = "missing-prompt"
    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await queue.enqueue(job)

    reconciler = Reconciler(
        job_queue=queue, comfy_client=_UnknownThenFailClient(), poll_interval_s=0.01
    )
    await reconciler.run_once()

    updated = await queue.get(job.id)
    assert updated.state == "retry_wait"
    assert updated.error_code == "comfy_transient"


@pytest.mark.asyncio
async def test_reconciler_renews_lease_while_comfy_still_running():
    queue = InMemoryJobQueue()
    job = _job(state="running")
    await queue.enqueue(job)
    comfy = MockComfyUIClient(polls_to_complete=5)
    submit = await comfy.submit(job.input_payload)
    job.prompt_id = submit.prompt_id
    job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=5)

    reconciler = Reconciler(
        job_queue=queue, comfy_client=comfy, poll_interval_s=0.01, lease_seconds=120.0
    )
    await reconciler.run_once()

    updated = await queue.get(job.id)
    assert updated.state == "running"
    assert updated.lease_expires_at is not None
    assert updated.lease_expires_at > datetime.now(timezone.utc) + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_admit_uses_conversation_backlog_not_shared_user():
    queue = InMemoryJobQueue()
    user_id = uuid.uuid4()
    conv_a = uuid.uuid4()
    conv_b = uuid.uuid4()

    for i in range(3):
        await admit_generation_job(
            queue=queue,
            user_id=user_id,
            workflow_name="image_basic",
            workflow_version="v1",
            inputs={"prompt": f"a-{i}"},
            idempotency_key=f"a-{i}",
            conversation_id=conv_a,
        )

    with pytest.raises(CapacityExceededError):
        await admit_generation_job(
            queue=queue,
            user_id=user_id,
            workflow_name="image_basic",
            workflow_version="v1",
            inputs={"prompt": "a-overflow"},
            idempotency_key="a-overflow",
            conversation_id=conv_a,
        )

    # Different conversation under the same service user still admits.
    result = await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "b-0"},
        idempotency_key="b-0",
        conversation_id=conv_b,
    )
    assert result.replayed is False
