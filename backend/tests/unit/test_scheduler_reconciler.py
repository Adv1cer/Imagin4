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
    defaults = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "kind": "txt2img_basic",
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
async def test_claim_next_kinds_filter_restricts_candidates():
    queue = InMemoryJobQueue()
    comfy_job = _job(kind="image_basic")
    gemini_job = _job(kind="poster_infographic")
    await queue.enqueue(comfy_job)
    await queue.enqueue(gemini_job)

    claimed = await queue.claim_next(worker_capacity=10, kinds=frozenset({"poster_infographic"}))

    assert [j.id for j in claimed] == [gemini_job.id]
    # Untouched: still queued, not claimed by the filtered call above.
    assert (await queue.get(comfy_job.id)).state == "queued"


@pytest.mark.asyncio
async def test_claim_next_kinds_none_means_no_filtering():
    queue = InMemoryJobQueue()
    job = _job()
    await queue.enqueue(job)
    claimed = await queue.claim_next(worker_capacity=10, kinds=None)
    assert [j.id for j in claimed] == [job.id]


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
    capacity = await scheduler._reserve_capacity_by_backend()
    assert capacity == {
        "comfyui": scheduler.settings.default_comfy_active_slots,
        "gemini": scheduler.settings.default_gemini_active_slots,
    }


@pytest.mark.asyncio
async def test_scheduler_dispatches_gemini_and_comfyui_jobs_as_independent_lanes():
    """The whole point of splitting capacity by backend: a full ComfyUI lane must not
    block a queued Gemini (poster_infographic) job, and vice versa -- see
    Scheduler._reserve_capacity_by_backend / _claim_for_backend."""
    queue = InMemoryJobQueue()
    comfy_job = _job(kind="image_basic")
    gemini_job = _job(kind="poster_infographic")
    await queue.enqueue(comfy_job)
    await queue.enqueue(gemini_job)
    comfy = MockComfyUIClient(polls_to_complete=0)
    scheduler = Scheduler(job_queue=queue, comfy_client=comfy, poll_interval_s=0.01)

    await scheduler._tick()
    if scheduler._inflight:
        import asyncio

        await asyncio.gather(*list(scheduler._inflight))

    updated_comfy = await queue.get(comfy_job.id)
    updated_gemini = await queue.get(gemini_job.id)
    assert updated_comfy.state == "running"
    assert updated_gemini.state == "running"


@pytest.mark.asyncio
async def test_full_gemini_lane_does_not_block_comfyui_claim():
    queue = InMemoryJobQueue()
    # Two gemini jobs, but capacity is overridden to 1 for that lane below -- only one
    # should be claimed, while the comfyui job (separate lane/capacity) still is.
    gemini_job_1 = _job(kind="poster_infographic")
    gemini_job_2 = _job(kind="poster_infographic")
    comfy_job = _job(kind="image_basic")
    await queue.enqueue(gemini_job_1)
    await queue.enqueue(gemini_job_2)
    await queue.enqueue(comfy_job)
    comfy = MockComfyUIClient(polls_to_complete=0)
    from app.core.config import Settings

    settings = Settings(default_comfy_active_slots=1, default_gemini_active_slots=1)
    scheduler = Scheduler(
        job_queue=queue, comfy_client=comfy, settings=settings, poll_interval_s=0.01
    )

    await scheduler._tick()
    if scheduler._inflight:
        import asyncio

        await asyncio.gather(*list(scheduler._inflight))

    states = {
        gemini_job_1.id: (await queue.get(gemini_job_1.id)).state,
        gemini_job_2.id: (await queue.get(gemini_job_2.id)).state,
        comfy_job.id: (await queue.get(comfy_job.id)).state,
    }
    # Exactly one of the two gemini jobs claimed (lane capacity 1)...
    gemini_states = [states[gemini_job_1.id], states[gemini_job_2.id]]
    assert gemini_states.count("running") == 1
    assert gemini_states.count("queued") == 1
    # ...and the comfyui job claimed too, unaffected by the full gemini lane.
    assert states[comfy_job.id] == "running"


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
