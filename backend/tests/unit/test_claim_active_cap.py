"""Unit tests for JobQueue.claim_next(_with_lease)'s `max_active_per_user` param
(2026-08-20) against InMemoryJobQueue -- the fast, DB-free half of verification.
See app/adapters/queue/postgres.py's `_claim` for the real-Postgres-tested SQL side,
verified separately (including a concurrent-claim race test) since that query has its
own documented history of subtle concurrency bugs."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.queue import InMemoryJobQueue, QueuedJob


def _job(user_id, queued_at, priority=0):
    return QueuedJob(
        id=uuid.uuid4(),
        user_id=user_id,
        kind="image_basic",
        state="queued",
        priority=priority,
        effective_priority=float(priority),
        input_payload={},
        idempotency_key=str(uuid.uuid4()),
        queued_at=queued_at,
    )


@pytest.mark.asyncio
async def test_zero_disables_cap_matches_old_behavior():
    queue = InMemoryJobQueue()
    u1 = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await queue.enqueue(_job(u1, now))
    await queue.enqueue(_job(u1, now + timedelta(seconds=1)))
    claimed = await queue.claim_next(worker_capacity=5, max_active_per_user=0)
    assert len(claimed) == 2


@pytest.mark.asyncio
async def test_cap_limits_how_many_of_one_users_queued_jobs_claim_in_one_batch():
    queue = InMemoryJobQueue()
    heavy = uuid.uuid4()
    other = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await queue.enqueue(_job(heavy, now))
    await queue.enqueue(_job(heavy, now + timedelta(seconds=1)))
    await queue.enqueue(_job(heavy, now + timedelta(seconds=2)))
    await queue.enqueue(_job(other, now + timedelta(seconds=3)))

    claimed = await queue.claim_next(worker_capacity=5, max_active_per_user=1)
    by_user = {}
    for j in claimed:
        by_user[j.user_id] = by_user.get(j.user_id, 0) + 1
    assert by_user.get(heavy) == 1
    assert by_user.get(other) == 1
    assert len(claimed) == 2  # not 4 -- capacity was available but heavy's cap wasn't


@pytest.mark.asyncio
async def test_pre_existing_active_job_counts_against_the_cap():
    queue = InMemoryJobQueue()
    u1 = uuid.uuid4()
    now = datetime.now(timezone.utc)
    already_running = _job(u1, now)
    already_running.state = "running"
    await queue.enqueue(already_running)
    await queue.enqueue(_job(u1, now + timedelta(seconds=1)))

    claimed = await queue.claim_next(worker_capacity=5, max_active_per_user=1)
    assert claimed == []


@pytest.mark.asyncio
async def test_claim_next_with_lease_passes_cap_through():
    queue = InMemoryJobQueue()
    heavy = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await queue.enqueue(_job(heavy, now))
    await queue.enqueue(_job(heavy, now + timedelta(seconds=1)))

    claimed = await queue.claim_next_with_lease(
        worker_capacity=5, lease_owner="s1", lease_seconds=60, max_active_per_user=1
    )
    assert len(claimed) == 1
    assert claimed[0].lease_owner == "s1"
