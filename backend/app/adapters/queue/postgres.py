"""PostgresJobQueue: durable JobQueue backed by `generation_jobs` / `job_attempts` /
`job_events` (see app/db/models.py), closing the gap flagged throughout this repo (see
app/main.py's `lifespan`, app/services/scheduler.py's/reconciler.py's `main()` --
"Only the in-memory JobQueue exists in this repo today").

Why this matters (see README "Known limitations" and app/main.py:211-218): the
InMemoryJobQueue is a plain Python dict local to ONE process. Every job disappears on
restart, and two API replicas (or the standalone `scheduler`/`reconciler`
docker-compose services, which each construct their own separate InMemoryJobQueue) do
not share state at all -- which is exactly why the standalone scheduler/reconciler
containers in docker-compose.yml have never actually dispatched anything: app/main.py's
lifespan runs its OWN in-process scheduler+reconciler against app.state.job_queue as a
workaround. This adapter is durable and shared, so once it's wired in (see
`app/adapters/queue/factory.py`), the standalone containers become real, independent,
horizontally-replicable processes again, matching the project's own architecture
contract ("FastAPI API processes are stateless and horizontally replicable").

Design notes:
  - Every transition is a conditional `UPDATE ... WHERE id = :id AND state = :expected`
    (or an explicit `state NOT IN (terminal)` guard), per this project's own invariant
    (see app/domain/jobs/state_machine.py's module docstring and the architecture doc,
    "ทุก transition ต้องใช้ conditional update"). Terminal-state no-op guards mirror
    InMemoryJobQueue exactly (see app/adapters/queue/__init__.py's "CONCURRENCY BUG
    FIXED HERE" note) -- a late/duplicate mark_* call can never downgrade a job that has
    already reached succeeded/failed/cancelled.
  - `claim_next(_with_lease)` uses a single `UPDATE ... FROM (SELECT ... FOR UPDATE
    SKIP LOCKED)` statement so the claim is atomic (no separate SELECT-then-UPDATE race
    window between two scheduler replicas).
  - Per-attempt state (lease_owner, lease_expires_at, comfy_prompt_id) lives in
    `job_attempts`, one row per attempt_no, as the project's own data model specifies
    (section 6 of the architecture doc) -- NOT flattened onto generation_jobs. This
    structurally eliminates the "stale prompt_id from a previous attempt" race that
    InMemoryJobQueue had to explicitly work around in `mark_running` (see its docstring):
    a new attempt gets its OWN row with comfy_prompt_id starting NULL, so there is no
    shared mutable field a stale reconciliation pass could read after a new attempt has
    already started. `job_attempts.lease_owner`/`lease_expires_at` are NOT NULL in the
    schema, so unleased `claim_next()` (no caller-supplied lease) delegates to
    `claim_next_with_lease` internally with a default synthetic lease -- see its
    docstring below.
  - `result` (the `{"outputs": [...]}` dict InMemoryJobQueue stores directly on the
    dataclass) has no dedicated column in this schema; the real destination for
    generated-image metadata is the `assets` table (owned by the storage layer, not this
    queue). Until that wiring exists, this adapter stashes it in the current attempt's
    `job_attempts.metrics` JSONB column (`metrics->>'result'`) as a pragmatic interim
    home -- documented here so it's an obvious thing to migrate, not a silent hack.
  - `error_code` / `error_detail` map directly to generation_jobs' own
    `error_code`/`error_detail_sanitized` columns (no job_attempts round-trip needed).
  - `cancel()` mirrors InMemoryJobQueue's current (simpler) behavior: any non-terminal
    job goes straight to `cancelled`. The richer `running -> cancelling -> cancelled`
    path modeled in app/domain/jobs/state_machine.py (for when a live ComfyUI cancel
    call is actually wired in) is intentionally NOT used yet, to keep this adapter's
    observable behavior identical to the one every other component was built/tested
    against -- swap this for `state_machine.request_cancel()` once real
    ComfyUI-cancellation exists (see workflow_registry/CompositeComfyUIClient's `cancel`
    -- not implemented anywhere in this repo yet).
  - Fairness/aging: `claim_next`/`claim_next_with_lease` order candidates by
    `priority + age_minutes * aging_increment_per_minute` computed AT QUERY TIME (not
    the static `effective_priority` value stamped at enqueue time), so jobs that have
    been waiting actually age the way app/domain/jobs/fairness.py's `effective_priority`
    intends -- InMemoryJobQueue does not do this today (it sorts on the static column),
    so this is a deliberate behavioral improvement, not just a port.

NOT yet implemented (see "next safe increment" in the final report): `comfy_workers`
worker-selection integration (app/services/scheduler.py's `_reserve_capacity_by_backend`
already reads that table directly when given a `session_factory`, independent of which
JobQueue backend is active, so no change needed there), and a real Postgres integration
test run (see tests/integration/test_postgres_job_queue.py -- written using
`testcontainers`, per this repo's own dev dependency, but NOT executed in the sandbox
this file was authored in: no Docker daemon was available there. Run it for real before
trusting this in production.)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.adapters.queue import QueuedJob
from app.domain.jobs.workflow_registry import list_workflows

# Mirrors app/domain/jobs/state_machine.py's TERMINAL_STATES -- duplicated as a plain SQL
# tuple (rather than importing and formatting it) so every conditional UPDATE below can
# inline it directly into `state NOT IN :terminal_states` without extra round-trips.
_TERMINAL_STATES = ("succeeded", "failed", "cancelled")

# Mirrors the states app/domain/jobs/admission.py's CapacityExceededError check counts
# as "backlog" -- jobs claimed (dispatched/running) are deliberately excluded, see that
# exception's docstring for why Settings.max_active_jobs_per_user isn't covered here.
_BACKLOG_STATES = ("queued", "retry_wait")

_DEFAULT_UNLEASED_LEASE_SECONDS = 300.0

# Mirrors Settings.aging_increment_per_minute's own default (see _claim's params below
# for why this adapter hardcodes it rather than importing app.core.config -- kept
# dependency-free of the app layer to stay easy to unit-test in isolation). Pulled out
# as a named constant (2026-08-20) so `_claim` and the new `queue_rank` below can't
# silently drift apart on which aging rate they each assume.
_AGING_INCREMENT_PER_MINUTE = 0.5

# Reused by get()/list_active()/claim*() -- LEFT JOIN LATERAL pulls the latest
# job_attempts row (by attempt_no) per job, so callers see the CURRENT attempt's
# lease/prompt_id/result without a second query. No matching job_attempts row (job never
# claimed yet) leaves those columns NULL, matching InMemoryJobQueue's field defaults.
_SELECT_JOB_WITH_LATEST_ATTEMPT = """
    SELECT
        g.id, g.user_id, g.kind, g.state, g.priority, g.effective_priority,
        g.input_payload, g.idempotency_key, g.queued_at, g.current_attempt,
        g.max_attempts, g.assigned_worker_id, g.error_code, g.error_detail_sanitized,
        a.lease_owner, a.lease_expires_at, a.comfy_prompt_id, a.metrics
    FROM generation_jobs g
    LEFT JOIN LATERAL (
        SELECT lease_owner, lease_expires_at, comfy_prompt_id, metrics
        FROM job_attempts
        WHERE job_attempts.job_id = g.id
        ORDER BY attempt_no DESC
        LIMIT 1
    ) a ON true
"""


def _resolve_workflow_defaults(kind: str) -> tuple[str, str]:
    """(model_family, workflow_version) for a given `kind` (== workflow name).

    generation_jobs.model_family/workflow_version are NOT NULL, but QueuedJob (the
    Protocol's DTO, shared with InMemoryJobQueue which has no such columns) doesn't
    carry them -- app/domain/jobs/admission.py already validates `kind` against the
    allowlist via `resolve_workflow` before ever calling `queue.enqueue`, so re-deriving
    here from the same registry is safe, not a second validation path. Matches the
    existing assumption elsewhere in this repo (see workflow_registry.kinds_for_backend's
    docstring) that a `kind` with multiple versions all agree on the fields that matter
    here -- picks the first registered definition for `kind` if more than one version
    exists.
    """
    for wf in list_workflows():
        if wf.name == kind:
            return wf.model_family, wf.version
    # Should be unreachable in practice (admission.py already rejected unknown kinds),
    # but never silently persist a NOT NULL column as an empty string.
    raise ValueError(f"cannot resolve model_family/workflow_version for unknown kind={kind!r}")


def _row_to_queued_job(row: Any) -> QueuedJob:
    metrics = row.metrics or {}
    return QueuedJob(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        state=row.state,
        priority=row.priority,
        effective_priority=row.effective_priority,
        input_payload=row.input_payload,
        idempotency_key=row.idempotency_key,
        queued_at=row.queued_at,
        current_attempt=row.current_attempt,
        max_attempts=row.max_attempts,
        assigned_worker_id=row.assigned_worker_id,
        error_code=row.error_code,
        error_detail=row.error_detail_sanitized,
        result=metrics.get("result"),
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        prompt_id=row.comfy_prompt_id,
    )


class PostgresJobQueue:
    """Durable, multi-process-safe JobQueue. See module docstring for the full design
    rationale. `session_factory` must be an `async_sessionmaker` bound to an engine
    using the asyncpg driver (matches `app.state.session_factory`, already built in
    app/main.py's `_build_state` from `settings.database_url`)."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory

    async def enqueue(self, job: QueuedJob) -> QueuedJob:
        model_family, workflow_version = _resolve_workflow_defaults(job.kind)
        async with self.session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                        INSERT INTO generation_jobs
                            (id, user_id, kind, model_family, workflow_version, state,
                             priority, effective_priority, idempotency_key, input_payload,
                             current_attempt, max_attempts, queued_at)
                        VALUES
                            (:id, :user_id, :kind, :model_family, :workflow_version, :state,
                             :priority, :effective_priority, :idempotency_key, :input_payload,
                             :current_attempt, :max_attempts, :queued_at)
                        """
                    # Raw text() params default to NullType -- without an explicit JSONB
                    # type here, SQLAlchemy skips the JSON-serialize bind processor and
                    # hands asyncpg a raw Python dict, which its binary jsonb encoder
                    # rejects with `AttributeError: 'dict' object has no attribute
                    # 'encode'` (confirmed via Chet's actual integration-test run against a
                    # real Postgres, 2026-08-17 -- this was NOT caught before that, since
                    # this sandbox has no Docker to run the integration suite against).
                ).bindparams(bindparam("input_payload", type_=JSONB)),
                {
                    "id": job.id,
                    "user_id": job.user_id,
                    "kind": job.kind,
                    "model_family": model_family,
                    "workflow_version": workflow_version,
                    "state": job.state,
                    "priority": job.priority,
                    "effective_priority": job.effective_priority,
                    "idempotency_key": job.idempotency_key,
                    "input_payload": job.input_payload,
                    "current_attempt": job.current_attempt,
                    "max_attempts": job.max_attempts,
                    "queued_at": job.queued_at,
                },
            )
            await self._append_event(session, job.id, "created", {})
        return job

    async def get(self, job_id: uuid.UUID) -> QueuedJob | None:
        async with self.session_factory() as session:
            result = await session.execute(
                text(_SELECT_JOB_WITH_LATEST_ATTEMPT + " WHERE g.id = :job_id"),
                {"job_id": job_id},
            )
            row = result.first()
            return _row_to_queued_job(row) if row is not None else None

    # Fixed, arbitrary constant -- namespaces the advisory lock below to this one
    # feature (pg_advisory_xact_lock's keyspace is global to the whole database, so any
    # future unrelated use of advisory locks elsewhere must pick a different constant).
    _CLAIM_ACTIVE_CAP_LOCK_KEY = 872234198

    async def _finalize_claimed_row(
        self, session, row, lease_owner: str, lease_expires_at
    ) -> QueuedJob:
        """Shared by both claim paths below: records the job_attempts row for this
        attempt and the `dispatched` event, then builds the QueuedJob DTO the caller
        returns. Factored out so the new per-user-capped path (which claims one row per
        SQL round-trip instead of one batch) doesn't duplicate this bookkeeping."""
        attempt_no = row.current_attempt + 1
        await session.execute(
            text("""
                    INSERT INTO job_attempts
                        (job_id, attempt_no, state, lease_owner, lease_expires_at,
                         submitted_at)
                    VALUES
                        (:job_id, :attempt_no, 'dispatched', :lease_owner,
                         :lease_expires_at, now())
                    ON CONFLICT (job_id, attempt_no) DO UPDATE SET
                        lease_owner = excluded.lease_owner,
                        lease_expires_at = excluded.lease_expires_at
                    """),
            {
                "job_id": row.id,
                "attempt_no": attempt_no,
                "lease_owner": lease_owner,
                "lease_expires_at": lease_expires_at,
            },
        )
        await self._append_event(session, row.id, "dispatched", {"attempt_no": attempt_no})
        return QueuedJob(
            id=row.id,
            user_id=row.user_id,
            kind=row.kind,
            state=row.state,
            priority=row.priority,
            effective_priority=row.effective_priority,
            input_payload=row.input_payload,
            idempotency_key=row.idempotency_key,
            queued_at=row.queued_at,
            current_attempt=row.current_attempt,
            max_attempts=row.max_attempts,
            assigned_worker_id=row.assigned_worker_id,
            error_code=row.error_code,
            error_detail=row.error_detail_sanitized,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
        )

    async def _claim(
        self,
        worker_capacity: int,
        kinds: frozenset[str] | None,
        lease_owner: str,
        lease_seconds: float,
        max_active_per_user: int = 0,
    ) -> list[QueuedJob]:
        if worker_capacity <= 0:
            return []

        # NOTE (2026-08-17, root-caused from Chet's actual concurrent-claim integration
        # test): the original version of this query used a `WITH candidates AS (SELECT
        # ... FOR UPDATE SKIP LOCKED) UPDATE ... FROM candidates` CTE form. That let TWO
        # concurrent transactions both claim the SAME row under real Postgres (confirmed
        # via test_claim_next_with_lease_is_atomic_under_concurrent_schedulers failing
        # with `assert 2 == 1`) -- a data-modifying CTE's row set is NOT guaranteed to
        # observe another session's FOR UPDATE SKIP LOCKED the same way a plain subquery
        # does. Rewritten to the well-established, widely-used-for-exactly-this-purpose
        # form instead: the SKIP LOCKED scan lives directly in the UPDATE's own WHERE
        # subquery (the pattern used by Que/pg-boss/River and virtually every "Postgres
        # as a queue" reference implementation), which does NOT have this gap.
        kinds_clause = "AND kind IN :kinds" if kinds is not None else ""

        lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)

        if max_active_per_user <= 0:
            # Unchanged from before 2026-08-20 -- single multi-row claim, zero added
            # risk for every caller that doesn't opt into the per-user cap below.
            stmt = text(f"""
                UPDATE generation_jobs g
                SET state = 'dispatched'
                WHERE g.id IN (
                    SELECT id
                    FROM generation_jobs
                    WHERE state IN ('queued', 'retry_wait')
                    {kinds_clause}
                    ORDER BY
                        (priority + (extract(epoch from (now() - queued_at)) / 60.0)
                            * :aging_increment) DESC,
                        queued_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT :capacity
                )
                RETURNING g.id, g.user_id, g.kind, g.state, g.priority, g.effective_priority,
                          g.input_payload, g.idempotency_key, g.queued_at, g.current_attempt,
                          g.max_attempts, g.assigned_worker_id, g.error_code,
                          g.error_detail_sanitized
                """)
            if kinds is not None:
                stmt = stmt.bindparams(bindparam("kinds", expanding=True))
            # aging_increment_per_minute is a Settings value in the app layer; this
            # adapter has no Settings reference of its own (kept dependency-free of
            # app.core.config to stay easy to unit-test) -- 0.5 matches
            # Settings.aging_increment_per_minute's own default.
            params: dict[str, Any] = {
                "capacity": worker_capacity,
                "aging_increment": _AGING_INCREMENT_PER_MINUTE,
            }
            if kinds is not None:
                params["kinds"] = list(kinds)

            async with self.session_factory() as session, session.begin():
                result = await session.execute(stmt, params)
                claimed_rows = result.fetchall()
                return [
                    await self._finalize_claimed_row(session, row, lease_owner, lease_expires_at)
                    for row in claimed_rows
                ]

        # max_active_per_user > 0: one row per SQL round-trip (up to worker_capacity),
        # each re-evaluating a correlated "how many of this candidate's OWN jobs are
        # already dispatched/running" subquery -- see Settings.max_active_jobs_per_user's
        # docstring for why this is enforced here (claim time) rather than only at
        # admission (CapacityExceededError only caps the QUEUED backlog, not how many of
        # one user's jobs can be simultaneously active when worker_capacity > 1).
        #
        # The advisory lock below is NOT optional: without it, two concurrent scheduler
        # transactions can each run the correlated subquery, both see "0 active for user
        # X" under READ COMMITTED (neither sees the other's still-uncommitted UPDATE),
        # and both claim a job for X -- verified against a real local Postgres 16 with
        # concurrent asyncio tasks before this code was written (the same class of bug
        # the 2026-08-17 note above describes, just one level up: THIS query already
        # avoids double-claiming the same ROW via FOR UPDATE SKIP LOCKED, but nothing
        # protected the separate "how many active does this USER have" count without
        # this lock). Xact-scoped (`_xact_lock`, not session-level) -- released
        # automatically on COMMIT/ROLLBACK, never needs an explicit unlock call. Only
        # taken on this path -- callers with max_active_per_user<=0 (unchanged default
        # for every existing caller) never pay for it.
        single_stmt = text(f"""
            UPDATE generation_jobs g
            SET state = 'dispatched'
            WHERE g.id = (
                SELECT id
                FROM generation_jobs
                WHERE state IN ('queued', 'retry_wait')
                {kinds_clause}
                AND (
                    SELECT count(*) FROM generation_jobs g2
                    WHERE g2.user_id = generation_jobs.user_id
                      AND g2.state IN ('dispatched', 'running')
                ) < :max_active_per_user
                ORDER BY
                    (priority + (extract(epoch from (now() - queued_at)) / 60.0)
                        * :aging_increment) DESC,
                    queued_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING g.id, g.user_id, g.kind, g.state, g.priority, g.effective_priority,
                      g.input_payload, g.idempotency_key, g.queued_at, g.current_attempt,
                      g.max_attempts, g.assigned_worker_id, g.error_code,
                      g.error_detail_sanitized
            """)
        if kinds is not None:
            single_stmt = single_stmt.bindparams(bindparam("kinds", expanding=True))
        single_params: dict[str, Any] = {
            "aging_increment": _AGING_INCREMENT_PER_MINUTE,
            "max_active_per_user": max_active_per_user,
        }
        if kinds is not None:
            single_params["kinds"] = list(kinds)

        claimed_jobs: list[QueuedJob] = []
        async with self.session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": self._CLAIM_ACTIVE_CAP_LOCK_KEY},
            )
            for _ in range(worker_capacity):
                result = await session.execute(single_stmt, single_params)
                row = result.first()
                if row is None:
                    break
                claimed_jobs.append(
                    await self._finalize_claimed_row(session, row, lease_owner, lease_expires_at)
                )
        return claimed_jobs

    async def claim_next(
        self,
        worker_capacity: int = 1,
        kinds: frozenset[str] | None = None,
        max_active_per_user: int = 0,
    ) -> list[QueuedJob]:
        """job_attempts.lease_owner/lease_expires_at are NOT NULL in this schema (unlike
        InMemoryJobQueue's nullable dataclass fields), so an "unleased" claim still needs
        *some* lease row -- delegates to claim_next_with_lease with a synthetic owner/
        default duration rather than duplicating the claim SQL. Real callers should
        always use claim_next_with_lease (see app/services/scheduler.py, which prefers
        it via `hasattr` whenever available -- true for this adapter)."""
        return await self._claim(
            worker_capacity,
            kinds,
            lease_owner="unleased",
            lease_seconds=_DEFAULT_UNLEASED_LEASE_SECONDS,
            max_active_per_user=max_active_per_user,
        )

    async def claim_next_with_lease(
        self,
        worker_capacity: int,
        lease_owner: str,
        lease_seconds: float,
        kinds: frozenset[str] | None = None,
        max_active_per_user: int = 0,
    ) -> list[QueuedJob]:
        return await self._claim(
            worker_capacity,
            kinds,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            max_active_per_user=max_active_per_user,
        )

    async def list_active(self) -> list[QueuedJob]:
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    _SELECT_JOB_WITH_LATEST_ATTEMPT + " WHERE g.state IN ('dispatched', 'running')"
                )
            )
            return [_row_to_queued_job(row) for row in result.fetchall()]

    async def mark_running(self, job_id: uuid.UUID) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                text("""
                        UPDATE generation_jobs
                        SET state = 'running', started_at = now()
                        WHERE id = :id AND state NOT IN :terminal
                        """).bindparams(bindparam("terminal", expanding=True)),
                {"id": job_id, "terminal": list(_TERMINAL_STATES)},
            )
            # No stale-prompt_id clear needed here (unlike InMemoryJobQueue) -- the
            # current attempt's job_attempts row already has comfy_prompt_id = NULL
            # until set_prompt_id() is called for THIS attempt_no; a prior attempt's
            # prompt_id lives on a DIFFERENT row entirely. See module docstring.

    async def mark_succeeded(self, job_id: uuid.UUID, result: dict) -> None:
        async with self.session_factory() as session, session.begin():
            updated = await session.execute(
                text("""
                        UPDATE generation_jobs
                        SET state = 'succeeded', finished_at = now()
                        WHERE id = :id AND state NOT IN :terminal
                        RETURNING id
                        """).bindparams(bindparam("terminal", expanding=True)),
                {"id": job_id, "terminal": list(_TERMINAL_STATES)},
            )
            if updated.first() is None:
                return  # already terminal -- no-op, matches InMemoryJobQueue
            await self._update_latest_attempt(
                session, job_id, state="succeeded", finished=True, extra_metrics={"result": result}
            )
            await self._append_event(session, job_id, "job_succeeded", {})

    async def mark_failed(
        self, job_id: uuid.UUID, error_code: str, error_detail: str | None = None
    ) -> None:
        async with self.session_factory() as session, session.begin():
            updated = await session.execute(
                text("""
                        UPDATE generation_jobs
                        SET state = 'failed', finished_at = now(),
                            error_code = :error_code, error_detail_sanitized = :error_detail
                        WHERE id = :id AND state NOT IN :terminal
                        RETURNING id
                        """).bindparams(bindparam("terminal", expanding=True)),
                {
                    "id": job_id,
                    "terminal": list(_TERMINAL_STATES),
                    "error_code": error_code,
                    "error_detail": error_detail,
                },
            )
            if updated.first() is None:
                return
            await self._update_latest_attempt(
                session, job_id, state="failed", finished=True, error_code=error_code
            )
            await self._append_event(
                session, job_id, "job_failed", {"error_code": error_code, "detail": error_detail}
            )

    async def mark_retry_wait(
        self, job_id: uuid.UUID, error_code: str, error_detail: str | None = None
    ) -> None:
        async with self.session_factory() as session, session.begin():
            updated = await session.execute(
                text("""
                    UPDATE generation_jobs
                    SET state = 'retry_wait', current_attempt = current_attempt + 1,
                        error_code = :error_code, error_detail_sanitized = :error_detail
                    WHERE id = :id AND state NOT IN :terminal
                    RETURNING id
                    """).bindparams(bindparam("terminal", expanding=True)),
                {
                    "id": job_id,
                    "terminal": list(_TERMINAL_STATES),
                    "error_code": error_code,
                    "error_detail": error_detail,
                },
            )
            if updated.first() is None:
                return  # no-op: also correctly leaves current_attempt un-bumped
            await self._update_latest_attempt(
                session, job_id, state="retry_wait", finished=True, error_code=error_code
            )
            await self._append_event(
                session,
                job_id,
                "job_retry_scheduled",
                {"error_code": error_code, "detail": error_detail},
            )

    async def set_prompt_id(self, job_id: uuid.UUID, prompt_id: str) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                text("""
                        UPDATE job_attempts
                        SET comfy_prompt_id = :prompt_id
                        WHERE job_id = :job_id
                          AND attempt_no = (
                              SELECT max(attempt_no) FROM job_attempts WHERE job_id = :job_id
                          )
                        """),
                {"job_id": job_id, "prompt_id": prompt_id},
            )

    async def cancel(self, job_id: uuid.UUID) -> bool:
        # Straight-to-cancelled for any non-terminal state -- see module docstring for
        # why this doesn't (yet) use state_machine.request_cancel()'s richer
        # running->cancelling->cancelled path.
        async with self.session_factory() as session, session.begin():
            updated = await session.execute(
                text("""
                        UPDATE generation_jobs
                        SET state = 'cancelled', cancel_requested_at = now(), finished_at = now()
                        WHERE id = :id AND state NOT IN :terminal
                        RETURNING id
                        """).bindparams(bindparam("terminal", expanding=True)),
                {"id": job_id, "terminal": list(_TERMINAL_STATES)},
            )
            if updated.first() is None:
                return False
            await self._append_event(session, job_id, "job_cancelled", {})
            return True

    async def find_by_idempotency_key(
        self, user_id: uuid.UUID, idempotency_key: str, kind: str
    ) -> QueuedJob | None:
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    _SELECT_JOB_WITH_LATEST_ATTEMPT
                    + " WHERE g.user_id = :user_id AND g.idempotency_key = :key AND g.kind = :kind"
                ),
                {"user_id": user_id, "key": idempotency_key, "kind": kind},
            )
            row = result.first()
            return _row_to_queued_job(row) if row is not None else None

    async def count_user_backlog(self, user_id: uuid.UUID) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                text("""
                    SELECT count(*) FROM generation_jobs
                    WHERE user_id = :user_id AND state IN :backlog_states
                    """).bindparams(bindparam("backlog_states", expanding=True)),
                {"user_id": user_id, "backlog_states": list(_BACKLOG_STATES)},
            )
            return int(result.scalar_one())

    async def count_global_backlog(self) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT count(*) FROM generation_jobs WHERE state IN :backlog_states"
                ).bindparams(bindparam("backlog_states", expanding=True)),
                {"backlog_states": list(_BACKLOG_STATES)},
            )
            return int(result.scalar_one())

    async def count_backlog_for_kinds(self, kinds: frozenset[str] | None = None) -> int:
        kinds_clause = "AND kind IN :kinds" if kinds is not None else ""
        stmt = text(f"""
            SELECT count(*) FROM generation_jobs
            WHERE state IN :backlog_states
            {kinds_clause}
            """).bindparams(bindparam("backlog_states", expanding=True))
        params: dict[str, Any] = {"backlog_states": list(_BACKLOG_STATES)}
        if kinds is not None:
            stmt = stmt.bindparams(bindparam("kinds", expanding=True))
            params["kinds"] = list(kinds)
        async with self.session_factory() as session:
            result = await session.execute(stmt, params)
            return int(result.scalar_one())

    async def queue_rank(
        self, job_id: uuid.UUID, kinds: frozenset[str] | None = None
    ) -> int | None:
        """Precise, aging-aware position: counts same-`kinds` backlog rows that rank
        AHEAD of `job_id` under the exact same `priority + age_minutes * aging_increment`
        formula `_claim` itself orders by (see that method's "Fairness/aging" note in the
        module docstring) -- deliberately NOT just `effective_priority` (the static
        column stamped at enqueue time), since that would drift from the real claim
        order as jobs age differently. Two round-trips (fetch the target row, then count
        against it) rather than one self-join query, for readability -- this is a
        read-only status lookup on the GET /v1/jobs/{id} hot path, not claim-time
        contention-sensitive code, so the extra round-trip is a fine trade."""
        async with self.session_factory() as session:
            target = (
                await session.execute(
                    text("SELECT state, priority, queued_at FROM generation_jobs WHERE id = :id"),
                    {"id": job_id},
                )
            ).first()
            if target is None or target.state not in _BACKLOG_STATES:
                return None

            kinds_clause = "AND kind IN :kinds" if kinds is not None else ""
            stmt = text(f"""
                SELECT count(*) FROM generation_jobs
                WHERE state IN :backlog_states
                {kinds_clause}
                AND (
                    (priority + (extract(epoch from (now() - queued_at)) / 60.0)
                        * :aging_increment)
                    > (:target_priority + (extract(epoch from (now() - :target_queued_at)) / 60.0)
                        * :aging_increment)
                    OR (
                        (priority + (extract(epoch from (now() - queued_at)) / 60.0)
                            * :aging_increment)
                        = (:target_priority + (extract(epoch from (now() - :target_queued_at)) / 60.0)
                            * :aging_increment)
                        AND queued_at < :target_queued_at
                    )
                )
                """).bindparams(bindparam("backlog_states", expanding=True))
            params: dict[str, Any] = {
                "backlog_states": list(_BACKLOG_STATES),
                "aging_increment": _AGING_INCREMENT_PER_MINUTE,
                "target_priority": target.priority,
                "target_queued_at": target.queued_at,
            }
            if kinds is not None:
                stmt = stmt.bindparams(bindparam("kinds", expanding=True))
                params["kinds"] = list(kinds)
            result = await session.execute(stmt, params)
            return int(result.scalar_one())

    # -- internal helpers -----------------------------------------------------------

    async def _append_event(
        self, session, job_id: uuid.UUID, event_type: str, payload: dict
    ) -> None:
        # See enqueue()'s comment on bindparam(type_=JSONB) -- same fix needed here,
        # and this one helper is shared by every mark_*/cancel/claim call site, so this
        # single bindparams() call was the fix for 7 of the 8 failing integration tests.
        await session.execute(
            text("""
                INSERT INTO job_events (job_id, sequence_no, event_type, payload)
                VALUES (
                    :job_id,
                    coalesce((SELECT max(sequence_no) FROM job_events WHERE job_id = :job_id), 0) + 1,
                    :event_type, :payload
                )
                """).bindparams(bindparam("payload", type_=JSONB)),
            {"job_id": job_id, "event_type": event_type, "payload": payload},
        )

    async def _update_latest_attempt(
        self,
        session,
        job_id: uuid.UUID,
        state: str,
        finished: bool = False,
        error_code: str | None = None,
        extra_metrics: dict | None = None,
    ) -> None:
        """No-op (rather than erroring) if the job was marked terminal before it was ever
        actually claimed/dispatched (no job_attempts row exists) -- callers of
        mark_succeeded/mark_failed on a never-dispatched job are a misuse of the
        Protocol, not something this adapter should crash on."""
        set_clauses = ["state = :state"]
        params: dict[str, Any] = {"job_id": job_id, "state": state}
        if finished:
            set_clauses.append("finished_at = now()")
        if error_code is not None:
            set_clauses.append("error_code = :error_code")
            params["error_code"] = error_code
        if extra_metrics is not None:
            set_clauses.append("metrics = metrics || :extra_metrics")
            params["extra_metrics"] = extra_metrics
        stmt = text(f"""
            UPDATE job_attempts
            SET {", ".join(set_clauses)}
            WHERE job_id = :job_id
              AND attempt_no = (SELECT max(attempt_no) FROM job_attempts WHERE job_id = :job_id)
            """)
        if extra_metrics is not None:
            # See enqueue()'s comment on bindparam(type_=JSONB) -- same fix, needed here
            # too since `metrics || :extra_metrics` is a jsonb || jsonb concat and
            # asyncpg needs the JSON-serialize bind processor to run on this param.
            stmt = stmt.bindparams(bindparam("extra_metrics", type_=JSONB))
        await session.execute(stmt, params)
