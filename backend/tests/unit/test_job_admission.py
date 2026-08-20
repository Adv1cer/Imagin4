"""Unit tests for app/domain/jobs/admission.py -- the job-admission path shared by
POST /v1/generations (app/api/v1/generations.py) and the agentic chat router's
GENERAL_IMAGE / POSTER-INFOGRAPHIC-confirm paths (app/api/v1/chat_router.py).

Covers: unknown/unapproved workflow names are rejected (nothing resembling an arbitrary
ComfyUI graph or unregistered workflow id can reach the queue), idempotency replay
returns the same job, a reused key with a different payload conflicts, and (2026-08-20)
per-user/global backlog caps reject admission once a user (or the whole system) already
has Settings.max_queued_jobs_per_user/global_queue_cap jobs waiting -- see
CapacityExceededError's docstring for the fairness incident this closes."""

from __future__ import annotations

import uuid

import pytest

from app.adapters.queue import InMemoryJobQueue
from app.core.config import get_settings
from app.domain.jobs.admission import (
    CapacityExceededError,
    IdempotencyConflictError,
    UnknownWorkflowError,
    admit_generation_job,
)


async def _admit(queue, user_id, *, key: str, prompt: str = "x"):
    return await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": prompt},
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_unknown_workflow_name_is_rejected():
    queue = InMemoryJobQueue()
    with pytest.raises(UnknownWorkflowError):
        await admit_generation_job(
            queue=queue,
            user_id=uuid.uuid4(),
            workflow_name="arbitrary_client_supplied_graph",
            workflow_version="v1",
            inputs={"prompt": "x"},
            idempotency_key="key-1",
        )


@pytest.mark.asyncio
async def test_unapproved_workflow_version_is_rejected():
    queue = InMemoryJobQueue()
    with pytest.raises(UnknownWorkflowError):
        await admit_generation_job(
            queue=queue,
            user_id=uuid.uuid4(),
            workflow_name="image_basic",
            workflow_version="v99-unapproved",
            inputs={"prompt": "x"},
            idempotency_key="key-1",
        )


@pytest.mark.asyncio
async def test_general_image_admits_via_the_comfyui_backed_workflow():
    """GENERAL_IMAGE must only ever go through "image_basic", whose registry entry is
    backend="comfyui" (see test_composite_comfyui_routing.py) -- never the paid
    "poster_infographic" workflow."""
    queue = InMemoryJobQueue()
    result = await admit_generation_job(
        queue=queue,
        user_id=uuid.uuid4(),
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "a cat"},
        idempotency_key="key-1",
    )
    assert result.kind == "image_basic"
    assert result.replayed is False


@pytest.mark.asyncio
async def test_poster_infographic_admits_via_the_gemini_backed_workflow():
    queue = InMemoryJobQueue()
    result = await admit_generation_job(
        queue=queue,
        user_id=uuid.uuid4(),
        workflow_name="poster_infographic",
        workflow_version="v1",
        inputs={"prompt": "a poster"},
        idempotency_key="key-1",
    )
    assert result.kind == "poster_infographic"


@pytest.mark.asyncio
async def test_same_idempotency_key_and_payload_replays_not_duplicates():
    queue = InMemoryJobQueue()
    user_id = uuid.uuid4()
    first = await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "same"},
        idempotency_key="dup-key",
    )
    second = await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "same"},
        idempotency_key="dup-key",
    )
    assert second.replayed is True
    assert second.id == first.id


@pytest.mark.asyncio
async def test_same_idempotency_key_different_payload_conflicts():
    """This is what makes POST /v1/pending-actions/{id}/confirm safe against a crash
    between "marked confirmed" and "job enqueued": a retry uses the SAME derived key
    (f"pending-action-{id}") and the SAME stored parameters, so it always replays --
    this test proves the inverse (different payload) correctly raises instead of
    silently admitting a second, different job under the same key."""
    queue = InMemoryJobQueue()
    user_id = uuid.uuid4()
    await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "original"},
        idempotency_key="dup-key",
    )
    with pytest.raises(IdempotencyConflictError):
        await admit_generation_job(
            queue=queue,
            user_id=user_id,
            workflow_name="image_basic",
            workflow_version="v1",
            inputs={"prompt": "different"},
            idempotency_key="dup-key",
        )


@pytest.mark.asyncio
async def test_admission_rejects_new_job_once_users_own_backlog_cap_reached():
    queue = InMemoryJobQueue()
    user_id = uuid.uuid4()
    cap = get_settings().max_queued_jobs_per_user
    for i in range(cap):
        result = await _admit(queue, user_id, key=f"key-{i}")
        assert result.replayed is False

    with pytest.raises(CapacityExceededError) as exc_info:
        await _admit(queue, user_id, key=f"key-{cap}")
    assert exc_info.value.retry_after_s > 0
    assert str(cap) in exc_info.value.reason


@pytest.mark.asyncio
async def test_admission_backlog_cap_is_per_user_not_shared():
    """The whole point of this cap (see CapacityExceededError's docstring): one user
    maxing out their own backlog must NOT block a different user's job."""
    queue = InMemoryJobQueue()
    heavy_user = uuid.uuid4()
    other_user = uuid.uuid4()
    cap = get_settings().max_queued_jobs_per_user
    for i in range(cap):
        await _admit(queue, heavy_user, key=f"heavy-{i}")

    # heavy_user is now at cap and gets rejected...
    with pytest.raises(CapacityExceededError):
        await _admit(queue, heavy_user, key=f"heavy-{cap}")

    # ...but other_user, starting from zero, is unaffected.
    result = await _admit(queue, other_user, key="other-1")
    assert result.replayed is False


@pytest.mark.asyncio
async def test_replay_does_not_count_against_backlog_cap():
    """A replay returns the SAME already-enqueued job rather than creating a new one, so
    it must never be blocked by (or count towards) the backlog cap -- otherwise a client
    retrying a request (the whole point of Idempotency-Key) could get incorrectly
    rejected once its own earlier jobs filled the user's queue."""
    queue = InMemoryJobQueue()
    user_id = uuid.uuid4()
    cap = get_settings().max_queued_jobs_per_user
    for i in range(cap):
        await _admit(queue, user_id, key=f"key-{i}", prompt=f"prompt-{i}")

    # Replaying the very first request (same key + same payload) must still succeed even
    # though the user's backlog is already at cap.
    replay = await _admit(queue, user_id, key="key-0", prompt="prompt-0")
    assert replay.replayed is True


@pytest.mark.asyncio
async def test_admission_rejects_new_job_once_global_queue_cap_reached(monkeypatch):
    import app.domain.jobs.admission as admission_module
    from app.core.config import Settings

    queue = InMemoryJobQueue()
    # Small, deterministic override instead of the real (5000) default -- avoids
    # enqueueing thousands of jobs just to exercise this path. max_queued_jobs_per_user
    # set high so ONLY the global cap can be the thing that trips in this test.
    monkeypatch.setattr(
        admission_module,
        "get_settings",
        lambda: Settings(global_queue_cap=2, max_queued_jobs_per_user=100),
    )

    await _admit(queue, uuid.uuid4(), key="u1-key1")
    await _admit(queue, uuid.uuid4(), key="u2-key1")  # different users -- proves this
    # is the GLOBAL cap, not a per-user one (each user is well under 100 here).

    with pytest.raises(CapacityExceededError) as exc_info:
        await _admit(queue, uuid.uuid4(), key="u3-key1")
    assert exc_info.value.retry_after_s > 0


@pytest.mark.asyncio
async def test_capacity_caps_disabled_when_set_to_zero_or_below(monkeypatch):
    import app.domain.jobs.admission as admission_module
    from app.core.config import Settings

    queue = InMemoryJobQueue()
    user_id = uuid.uuid4()
    monkeypatch.setattr(
        admission_module,
        "get_settings",
        lambda: Settings(global_queue_cap=0, max_queued_jobs_per_user=0),
    )
    # Well past what would be the default caps -- must all still succeed.
    for i in range(10):
        result = await _admit(queue, user_id, key=f"key-{i}")
        assert result.replayed is False
