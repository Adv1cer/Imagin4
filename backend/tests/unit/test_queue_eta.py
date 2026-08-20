"""Unit tests for the 2026-08-20 queue_position/estimated_wait_seconds feature:
- app/domain/jobs/admission.py: estimate_wait_seconds, and AdmissionResult's
  queue_position/estimated_wait_seconds fields computed at admission time.
- app/adapters/queue/__init__.py: InMemoryJobQueue.count_backlog_for_kinds / queue_rank.
- app/domain/jobs/workflow_registry.py: backend_for_kind.

The real-Postgres side (queue_rank's aging-aware SQL, count_backlog_for_kinds) is
covered separately against a real local Postgres 16 instance, same split as the
max_active_per_user work -- see test_real_postgres_claim.py's own module docstring for
why that split exists (this query family's documented history of concurrency bugs)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.queue import InMemoryJobQueue, QueuedJob
from app.core.config import Settings
from app.domain.jobs.admission import admit_generation_job, estimate_wait_seconds
from app.domain.jobs.workflow_registry import backend_for_kind, kinds_for_backend


def _job(kind, queued_at, state="queued"):
    return QueuedJob(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind=kind,
        state=state,
        priority=0,
        effective_priority=0.0,
        input_payload={},
        idempotency_key=str(uuid.uuid4()),
        queued_at=queued_at,
    )


# -- backend_for_kind ------------------------------------------------------------


def test_backend_for_kind_resolves_known_kinds():
    assert backend_for_kind("image_basic") == "comfyui"
    assert backend_for_kind("poster_infographic") == "gemini"


def test_backend_for_kind_returns_none_for_unknown_kind():
    assert backend_for_kind("not_a_real_workflow") is None


# -- estimate_wait_seconds --------------------------------------------------------


def test_estimate_wait_seconds_zero_position_is_zero_wait():
    settings = Settings(default_comfy_active_slots=1, estimated_job_duration_s=60.0)
    assert estimate_wait_seconds(0, "comfyui", settings) == 0.0


def test_estimate_wait_seconds_scales_with_position_and_capacity():
    settings = Settings(default_comfy_active_slots=2, estimated_job_duration_s=60.0)
    # 3 jobs ahead, 2 concurrent slots -> 1 full batch already drained (jobs 0-1),
    # this job is in the second batch -> one estimated_job_duration_s of wait.
    assert estimate_wait_seconds(3, "comfyui", settings) == 60.0
    # 4 jobs ahead, 2 slots -> exactly two full batches ahead -> 2x duration.
    assert estimate_wait_seconds(4, "comfyui", settings) == 120.0


def test_estimate_wait_seconds_uses_gemini_capacity_for_gemini_backend():
    settings = Settings(default_gemini_active_slots=3, estimated_job_duration_s=30.0)
    assert estimate_wait_seconds(3, "gemini", settings) == 30.0


def test_estimate_wait_seconds_none_when_capacity_is_zero():
    settings = Settings(default_comfy_active_slots=0)
    assert estimate_wait_seconds(5, "comfyui", settings) is None


# -- InMemoryJobQueue.count_backlog_for_kinds / queue_rank ------------------------


@pytest.mark.asyncio
async def test_count_backlog_for_kinds_filters_by_kind():
    queue = InMemoryJobQueue()
    now = datetime.now(timezone.utc)
    await queue.enqueue(_job("image_basic", now))
    await queue.enqueue(_job("image_basic", now + timedelta(seconds=1)))
    await queue.enqueue(_job("poster_infographic", now + timedelta(seconds=2)))

    assert await queue.count_backlog_for_kinds(kinds_for_backend("comfyui")) == 2
    assert await queue.count_backlog_for_kinds(kinds_for_backend("gemini")) == 1
    assert await queue.count_backlog_for_kinds(None) == 3


@pytest.mark.asyncio
async def test_count_backlog_for_kinds_excludes_dispatched_jobs():
    queue = InMemoryJobQueue()
    now = datetime.now(timezone.utc)
    await queue.enqueue(_job("image_basic", now, state="dispatched"))
    await queue.enqueue(_job("image_basic", now + timedelta(seconds=1)))
    assert await queue.count_backlog_for_kinds(kinds_for_backend("comfyui")) == 1


@pytest.mark.asyncio
async def test_queue_rank_reflects_fifo_position_within_backend():
    queue = InMemoryJobQueue()
    now = datetime.now(timezone.utc)
    j1 = _job("image_basic", now)
    j2 = _job("image_basic", now + timedelta(seconds=1))
    j3 = _job("image_basic", now + timedelta(seconds=2))
    for j in (j1, j2, j3):
        await queue.enqueue(j)

    kinds = kinds_for_backend("comfyui")
    assert await queue.queue_rank(j1.id, kinds) == 0
    assert await queue.queue_rank(j2.id, kinds) == 1
    assert await queue.queue_rank(j3.id, kinds) == 2


@pytest.mark.asyncio
async def test_queue_rank_ignores_other_backends():
    queue = InMemoryJobQueue()
    now = datetime.now(timezone.utc)
    poster = _job("poster_infographic", now)
    image = _job("image_basic", now + timedelta(seconds=1))
    await queue.enqueue(poster)
    await queue.enqueue(image)

    # image_basic is the ONLY comfyui-backend job -- its rank must be 0 even though a
    # gemini job was enqueued before it (different queue lane entirely).
    assert await queue.queue_rank(image.id, kinds_for_backend("comfyui")) == 0


@pytest.mark.asyncio
async def test_queue_rank_none_for_unknown_or_non_backlog_job():
    queue = InMemoryJobQueue()
    assert await queue.queue_rank(uuid.uuid4(), None) is None

    now = datetime.now(timezone.utc)
    dispatched = _job("image_basic", now, state="dispatched")
    await queue.enqueue(dispatched)
    assert await queue.queue_rank(dispatched.id, None) is None


# -- End-to-end through admit_generation_job --------------------------------------


@pytest.mark.asyncio
async def test_admission_sets_queue_position_zero_for_first_job():
    queue = InMemoryJobQueue()
    result = await admit_generation_job(
        queue=queue,
        user_id=uuid.uuid4(),
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "x"},
        idempotency_key="k1",
    )
    assert result.queue_position == 0
    assert result.estimated_wait_seconds == 0.0  # default_comfy_active_slots=1, pos 0


@pytest.mark.asyncio
async def test_admission_queue_position_increments_within_same_backend(monkeypatch):
    import app.domain.jobs.admission as admission_module

    queue = InMemoryJobQueue()
    # Keep both caps well above what this test enqueues so CapacityExceededError never
    # fires -- this test is only about queue_position bookkeeping.
    monkeypatch.setattr(
        admission_module,
        "get_settings",
        lambda: Settings(
            max_queued_jobs_per_user=100,
            global_queue_cap=100,
            default_comfy_active_slots=1,
            estimated_job_duration_s=45.0,
        ),
    )
    positions = []
    for i in range(3):
        result = await admit_generation_job(
            queue=queue,
            user_id=uuid.uuid4(),
            workflow_name="image_basic",
            workflow_version="v1",
            inputs={"prompt": f"p{i}"},
            idempotency_key=f"k{i}",
        )
        positions.append(result.queue_position)
    assert positions == [0, 1, 2]


@pytest.mark.asyncio
async def test_admission_queue_position_does_not_count_other_backend(monkeypatch):
    import app.domain.jobs.admission as admission_module

    queue = InMemoryJobQueue()
    monkeypatch.setattr(
        admission_module,
        "get_settings",
        lambda: Settings(max_queued_jobs_per_user=100, global_queue_cap=100),
    )
    # A poster/infographic job queued first must NOT inflate a subsequent image_basic
    # job's position -- separate backend lanes (see estimate_wait_seconds' own reasoning
    # for why ComfyUI/Gemini capacity are tracked independently).
    await admit_generation_job(
        queue=queue,
        user_id=uuid.uuid4(),
        workflow_name="poster_infographic",
        workflow_version="v1",
        inputs={"prompt": "poster"},
        idempotency_key="poster-1",
    )
    result = await admit_generation_job(
        queue=queue,
        user_id=uuid.uuid4(),
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "image"},
        idempotency_key="image-1",
    )
    assert result.queue_position == 0


@pytest.mark.asyncio
async def test_admission_replay_leaves_queue_position_none():
    queue = InMemoryJobQueue()
    await admit_generation_job(
        queue=queue,
        user_id=(user_id := uuid.uuid4()),
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "x"},
        idempotency_key="same-key",
    )
    replay = await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "x"},
        idempotency_key="same-key",
    )
    assert replay.replayed is True
    assert replay.queue_position is None
    assert replay.estimated_wait_seconds is None
