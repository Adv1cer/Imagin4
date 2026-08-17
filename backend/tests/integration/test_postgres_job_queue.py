"""Integration tests for PostgresJobQueue (app/adapters/queue/postgres.py) against a
REAL Postgres, using `testcontainers` (already a dev dependency -- see pyproject.toml).

NOT executed in the sandbox this file was authored in: no Docker daemon was available
there (confirmed via `docker ps` failing with "command not found"), so these tests are
written to the same contract test_scheduler_reconciler.py / test_queue_terminal_state_
guards.py already exercise against InMemoryJobQueue, but have NOT actually been run
against a live database yet. Run `pytest tests/integration -m integration -v` (needs
Docker) before trusting this adapter in production -- this is exactly the kind of claim
the project's own instructions warn against making without a reproducible run.

Run with: `pytest tests/integration/test_postgres_job_queue.py -v` (requires Docker).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("testcontainers")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from app.adapters.comfyui import MockComfyUIClient
from app.adapters.queue import QueuedJob
from app.adapters.queue.postgres import PostgresJobQueue
from app.services.reconciler import Reconciler
from app.services.scheduler import Scheduler

BACKEND_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def postgres_url() -> str:
    """Starts a throwaway Postgres container, runs `alembic upgrade head` against it
    (via subprocess, same as a real deploy would), and yields an asyncpg SQLAlchemy URL."""
    with PostgresContainer("postgres:16-alpine") as pg:
        asyncpg_url = pg.get_connection_url(driver="asyncpg")
        env = {**os.environ, "APP_DATABASE_URL": asyncpg_url}
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(BACKEND_ROOT),
            env=env,
            check=True,
        )
        yield asyncpg_url


@pytest.fixture()
async def session_factory(postgres_url: str):
    engine = create_async_engine(postgres_url, connect_args={"statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
async def test_user_id(session_factory) -> uuid.UUID:
    """generation_jobs.user_id is a NOT NULL FK to users(id) -- every test needs a real
    row to satisfy it.

    Also truncates every table this suite touches FIRST (2026-08-17, root-caused from
    Chet's actual test run): `postgres_url` is module-scoped (one container/schema
    reused for the whole file, for speed -- see its docstring), but this fixture and
    `session_factory` are function-scoped, so nothing was ever clearing data BETWEEN
    tests. `test_enqueue_and_get_round_trip` (runs first) enqueues a job and never
    claims it; that row was still sitting there with state='queued' when
    `test_claim_next_with_lease_is_atomic_under_concurrent_schedulers` ran next --
    `claim_next_with_lease` correctly has no per-test scoping (it claims ANY eligible
    job, exactly like the real scheduler would), so with TWO eligible 'queued' rows in
    the table, queue_a and queue_b each legitimately claimed a DIFFERENT one --
    `total_claimed == 2` was the database being in a dirty cross-test state, not a
    Postgres locking gap. (The earlier CTE -> subquery rewrite of `_claim`'s SQL was a
    genuine improvement -- that CTE form does have a real, separately-documented
    correctness edge case -- but it was never what this specific failure was about, and
    saying otherwise before actually isolating the cause was a mistake.)
    """
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("TRUNCATE job_events, job_attempts, generation_jobs, users RESTART IDENTITY CASCADE")
            )
            user_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO users (id, email, display_name) "
                    "VALUES (:id, :email, 'Test User')"
                ),
                {"id": user_id, "email": f"{user_id}@example.test"},
            )
    return user_id


def _job(user_id: uuid.UUID, **overrides) -> QueuedJob:
    defaults = {
        "id": uuid.uuid4(),
        "user_id": user_id,
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
async def test_enqueue_and_get_round_trip(session_factory, test_user_id):
    queue = PostgresJobQueue(session_factory)
    job = _job(test_user_id)
    await queue.enqueue(job)

    fetched = await queue.get(job.id)
    assert fetched is not None
    assert fetched.state == "queued"
    assert fetched.input_payload == {"prompt": "a cat"}
    assert fetched.lease_owner is None  # never claimed yet
    assert fetched.prompt_id is None


@pytest.mark.asyncio
async def test_claim_next_with_lease_is_atomic_under_concurrent_schedulers(session_factory, test_user_id):
    """The core reason this adapter exists: two independent scheduler processes racing
    for the SAME job must never both win -- SKIP LOCKED must make exactly one claim
    succeed."""
    queue_a = PostgresJobQueue(session_factory)
    queue_b = PostgresJobQueue(session_factory)  # simulates a second replica/process
    job = _job(test_user_id)
    await queue_a.enqueue(job)

    import asyncio

    results = await asyncio.gather(
        queue_a.claim_next_with_lease(worker_capacity=1, lease_owner="scheduler-a", lease_seconds=60),
        queue_b.claim_next_with_lease(worker_capacity=1, lease_owner="scheduler-b", lease_seconds=60),
    )
    total_claimed = sum(len(r) for r in results)
    assert total_claimed == 1  # exactly one scheduler won, never both, never zero

    winner = await queue_a.get(job.id)
    assert winner.state == "dispatched"
    assert winner.lease_owner in ("scheduler-a", "scheduler-b")


@pytest.mark.asyncio
async def test_mark_succeeded_is_a_noop_once_job_already_failed(session_factory, test_user_id):
    """Same regression this repo's InMemoryJobQueue was fixed for (see
    tests/unit/test_queue_terminal_state_guards.py) -- must hold for the real adapter too."""
    queue = PostgresJobQueue(session_factory)
    job = _job(test_user_id)
    await queue.enqueue(job)
    await queue.claim_next_with_lease(worker_capacity=1, lease_owner="s1", lease_seconds=60)
    await queue.mark_failed(job.id, "comfy_transient", "boom")

    await queue.mark_succeeded(job.id, {"outputs": [{"object_key": "generated/x.png"}]})

    updated = await queue.get(job.id)
    assert updated.state == "failed"
    assert updated.result is None


@pytest.mark.asyncio
async def test_new_attempt_never_sees_a_stale_prompt_id_from_a_prior_attempt(session_factory, test_user_id):
    """The structural fix this schema gives for free (see module docstring): attempt N's
    comfy_prompt_id lives on its OWN job_attempts row, so a reconciler reading the
    CURRENT attempt can never observe a previous attempt's prompt_id."""
    queue = PostgresJobQueue(session_factory)
    job = _job(test_user_id, max_attempts=3)
    await queue.enqueue(job)

    [claimed] = await queue.claim_next_with_lease(worker_capacity=1, lease_owner="s1", lease_seconds=60)
    await queue.mark_running(claimed.id)
    await queue.set_prompt_id(claimed.id, "attempt-1-prompt")
    await queue.mark_retry_wait(claimed.id, "comfy_transient", "timeout")  # bumps current_attempt

    [reclaimed] = await queue.claim_next_with_lease(worker_capacity=1, lease_owner="s2", lease_seconds=60)
    await queue.mark_running(reclaimed.id)

    mid_flight = await queue.get(reclaimed.id)
    assert mid_flight.state == "running"
    assert mid_flight.prompt_id is None  # new attempt's own row, not "attempt-1-prompt"


@pytest.mark.asyncio
async def test_job_state_survives_a_fresh_queue_instance_pointed_at_the_same_db(session_factory, test_user_id):
    """The whole point of this adapter over InMemoryJobQueue: state is NOT held in this
    process's memory -- a brand new PostgresJobQueue instance (simulating a restarted
    process / a different replica) must see exactly the same state."""
    queue_1 = PostgresJobQueue(session_factory)
    job = _job(test_user_id)
    await queue_1.enqueue(job)
    await queue_1.claim_next_with_lease(worker_capacity=1, lease_owner="s1", lease_seconds=60)

    queue_2 = PostgresJobQueue(session_factory)  # fresh instance, no shared Python state
    fetched = await queue_2.get(job.id)
    assert fetched is not None
    assert fetched.state == "dispatched"
    assert fetched.lease_owner == "s1"


@pytest.mark.asyncio
async def test_end_to_end_generation_completes_via_scheduler_and_reconciler(session_factory, test_user_id):
    """Same shape as tests/e2e/test_generation_completes_via_scheduler_reconciler.py,
    but against the real Postgres-backed queue instead of the in-memory fake."""
    queue = PostgresJobQueue(session_factory)
    job = _job(test_user_id)
    await queue.enqueue(job)

    comfy_client = MockComfyUIClient(polls_to_complete=0)
    scheduler = Scheduler(job_queue=queue, comfy_client=comfy_client, poll_interval_s=0.01)
    reconciler = Reconciler(job_queue=queue, comfy_client=comfy_client, poll_interval_s=0.01)

    import asyncio

    await scheduler._tick()
    if scheduler._inflight:
        await asyncio.gather(*list(scheduler._inflight))
    dispatched = await queue.get(job.id)
    assert dispatched.state == "running"
    assert dispatched.prompt_id is not None

    await reconciler.run_once()
    finished = await queue.get(job.id)
    assert finished.state == "succeeded"
    assert finished.result is not None
    assert finished.result["outputs"][0]["object_key"].startswith("generated/")


@pytest.mark.asyncio
async def test_find_by_idempotency_key_returns_existing_job(session_factory, test_user_id):
    queue = PostgresJobQueue(session_factory)
    job = _job(test_user_id, idempotency_key="replay-me")
    await queue.enqueue(job)

    found = await queue.find_by_idempotency_key(test_user_id, "replay-me", "txt2img_basic")
    assert found is not None
    assert found.id == job.id

    missing = await queue.find_by_idempotency_key(test_user_id, "never-used", "txt2img_basic")
    assert missing is None


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_conditional(session_factory, test_user_id):
    queue = PostgresJobQueue(session_factory)
    job = _job(test_user_id)
    await queue.enqueue(job)

    first = await queue.cancel(job.id)
    second = await queue.cancel(job.id)
    assert first is True
    assert second is False  # already terminal -- no-op, matches InMemoryJobQueue.cancel

    cancelled = await queue.get(job.id)
    assert cancelled.state == "cancelled"
