"""JobQueue port + adapters.

The real implementation claims rows from `generation_jobs` in Postgres using
`SELECT ... FOR UPDATE SKIP LOCKED` (see backend/app/scheduler). This module defines the
port plus a deterministic in-memory fake used by tests and local dev without Postgres/Redis.

CONCURRENCY BUG FIXED HERE (2026-08, root-caused from Chet's live production logs): a
poster_infographic job (kind requiring the Gemini backend) was observed dispatching FOUR
separate Gemini image-generation attempts for what should have been at most
`max_attempts` (3) -- including at least one dispatch that happened AFTER the job had
already reached a terminal `succeeded` state, and a `job_failed` reconciler event that
fired using an error from what turned out to be a STALE, superseded attempt.

Root cause: the Scheduler's dispatch (`mark_running` -> await `comfy_client.submit()` --
which for Gemini can take up to `gemini_image_request_timeout_s`, i.e. up to 90s -- ->
`set_prompt_id`) and the Reconciler's reconciliation pass (`list_active()` ->
`_reconcile_job`, polled independently on its own 5s interval) both read/write the SAME
mutable `QueuedJob` object with no locking or conditional-update guard between them --
exactly the race the project's own architecture doc warns about ("ทุก transition ต้องใช้
conditional update ... กัน worker สองตัวเปลี่ยนสถานะพร้อมกัน", see project instructions
section 4), which the in-memory adapter here had NOT actually been implementing despite
the real Postgres adapter being designed around it. Concretely: `job.prompt_id` from a
PREVIOUS (already-resolved, failed) attempt was never cleared when a NEW attempt began
dispatching, so if the reconciler's poll landed while a new attempt was still in flight
(job.state already "running" but the new prompt_id not yet set), it would resolve the
job using the OLD attempt's already-known-failed prompt_id -- potentially firing a
terminal failure (or, worse, overwriting an already-`succeeded` job) based on stale
information, independent of whatever the new, actually-in-flight attempt was doing.

Fixed two ways, both applied to InMemoryJobQueue below (and required of any future
Postgres-backed implementation too, per the docstrings on the JobQueue Protocol
methods above):
  1. `mark_running` now clears `prompt_id` back to None -- for the entire window between
     a new attempt starting and its own `set_prompt_id` call landing, the reconciler sees
     "no known prompt_id yet" and correctly does nothing (falls through to the
     lease-expiry check) instead of resolving a stale one.
  2. `mark_succeeded` / `mark_failed` / `mark_retry_wait` are now no-ops if the job is
     already in a terminal state (succeeded/failed/cancelled) -- a late-arriving stale
     reconciliation result can no longer downgrade/overwrite a job that has already
     reached its true final outcome.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol


@dataclass
class QueuedJob:
    id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    state: str
    priority: int
    effective_priority: float
    input_payload: dict
    idempotency_key: str
    queued_at: datetime
    current_attempt: int = 0
    max_attempts: int = 3
    assigned_worker_id: uuid.UUID | None = None
    error_code: str | None = None
    # The adapter's own (already-sanitized) error string, e.g. "gemini_error:ClientError"
    # or "gemini_no_image_in_response" -- error_code above is the reconciler's coarse
    # retry-classification bucket (e.g. "comfy_transient"), which is the same value
    # regardless of which backend (ComfyUI vs Gemini) actually failed. Without this field
    # a user/developer has no way to tell which backend handled a failed job short of
    # reading server logs. See app/services/reconciler.py:_fail_or_retry.
    error_detail: str | None = None
    result: dict | None = None
    # Lease bookkeeping (used by the scheduler/reconciler; see claim_next_with_lease).
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    prompt_id: str | None = None
    # Optional chat/agent conversation this job was admitted for. Used by agentflow's
    # shared-API-key path so max_queued is enforced per end-user conversation, not the
    # single service account (2026-08-20).
    conversation_id: uuid.UUID | None = None
    # Earliest time a queued/retry_wait job may be claimed -- maps to generation_jobs.
    # available_at. mark_retry_wait sets this to now+backoff so retries are not
    # immediately re-claimed (2026-08-20).
    available_at: datetime | None = None


class JobQueue(Protocol):
    async def enqueue(self, job: QueuedJob) -> QueuedJob: ...

    async def get(self, job_id: uuid.UUID) -> QueuedJob | None: ...

    async def claim_next(
        self,
        worker_capacity: int = 1,
        kinds: frozenset[str] | None = None,
        max_active_per_user: int = 0,
    ) -> list[QueuedJob]:
        """`kinds`, when given, restricts candidates to jobs whose `kind` is in the set --
        used by the scheduler to claim ComfyUI-backend and Gemini-backend jobs against
        independent capacity numbers (see app/services/scheduler.py and
        app/domain/jobs/workflow_registry.py:kinds_for_backend) so a slow Gemini
        poster/infographic job can't starve an unrelated ComfyUI job's slot or vice
        versa. None (default) means no filtering -- every existing caller keeps its
        original behavior.

        `max_active_per_user` (2026-08-20, see Settings.max_active_jobs_per_user's
        docstring): when > 0, a candidate whose user already has that many jobs in
        `dispatched`/`running` state -- counting jobs claimed earlier in THIS SAME batch
        too -- is skipped in favor of the next candidate, so one user can't take every
        slot in a single claim tick. 0 (default) disables this -- unlimited, matching
        every existing caller's original behavior."""
        ...

    async def claim_next_with_lease(
        self,
        worker_capacity: int,
        lease_owner: str,
        lease_seconds: float,
        kinds: frozenset[str] | None = None,
        max_active_per_user: int = 0,
    ) -> list[QueuedJob]:
        """Fairness-ordered claim (same selection as claim_next, including the same
        `kinds` filter and `max_active_per_user` cap) that additionally stamps a
        lease_owner/lease_expires_at on each claimed job, so a reconciler can later find
        dispatched/running jobs whose lease expired without a heartbeat/finalization
        (crashed scheduler, worker, or lost connection to ComfyUI)."""
        ...

    async def list_active(self) -> list[QueuedJob]:
        """Returns jobs currently in `dispatched` or `running` state, for reconciliation."""
        ...

    async def count_active(self, kinds: frozenset[str] | None = None) -> int:
        """How many jobs are currently `dispatched`/`running`, optionally filtered by
        `kinds`. Used by Scheduler._reserve_capacity_by_backend so claim budget subtracts
        in-flight work immediately instead of waiting on the 5s comfy_workers heartbeat
        (2026-08-20 DGX Spark turnaround fix)."""
        ...

    async def renew_lease(self, job_id: uuid.UUID, lease_seconds: float) -> None:
        """Extends lease_expires_at on the latest attempt while ComfyUI still reports
        the job as genuinely running -- so long gens (> DEFAULT_LEASE_SECONDS) are not
        treated as orphans, and unknown/lost prompts can still time out when the lease
        finally expires (2026-08-20)."""
        ...

    async def mark_running(self, job_id: uuid.UUID) -> None:
        """Transitions to `running` for a NEW attempt about to be dispatched. Must also
        clear any previously-set `prompt_id` -- see module docstring's "stale prompt_id"
        race for why a leftover prompt_id from a prior (already-resolved) attempt must
        never survive into a new attempt's in-flight window."""
        ...

    async def mark_succeeded(self, job_id: uuid.UUID, result: dict) -> None:
        """Must be a no-op (never downgrade) if the job is already in a terminal state
        (succeeded/failed/cancelled) -- see module docstring's "conditional update"
        note. A real (Postgres) implementation should express this as
        `WHERE id=:id AND state NOT IN ('succeeded','failed','cancelled')`, exactly the
        project's documented "every transition uses a conditional update" invariant.

        Must also clear `error_code`/`error_detail` back to None (2026-08-20, Chet:
        found via the /admin load-test tool showing a red `comfy_transient` error next
        to a job whose `state` was already `succeeded`) -- a job that failed once
        (mark_retry_wait stamps error_code/error_detail) and then succeeded on a LATER
        attempt would otherwise keep displaying that now-stale error forever, which
        reads as "this succeeded job is somehow also broken" to any caller/UI that
        naively shows `error_code` whenever it's non-null instead of gating on
        `state == 'failed'`."""
        ...

    async def mark_failed(
        self, job_id: uuid.UUID, error_code: str, error_detail: str | None = None
    ) -> None:
        """Same terminal-state guard as mark_succeeded above."""
        ...

    async def mark_retry_wait(
        self,
        job_id: uuid.UUID,
        error_code: str,
        error_detail: str | None = None,
        delay_seconds: float | None = None,
    ) -> None:
        """Same terminal-state guard as mark_succeeded above. When `delay_seconds` is
        set (or computed by the adapter from backoff defaults), stamps `available_at`
        so claim_next will not re-select this job until that time."""
        ...

    async def set_prompt_id(self, job_id: uuid.UUID, prompt_id: str) -> None: ...

    async def cancel(self, job_id: uuid.UUID) -> bool: ...

    async def count_user_backlog(self, user_id: uuid.UUID) -> int:
        """How many of this user's OWN jobs currently sit in `queued`/`retry_wait` --
        what app/domain/jobs/admission.py checks against
        Settings.max_queued_jobs_per_user before admitting a NEW job (2026-08-20, see
        CapacityExceededError's docstring there). Deliberately excludes
        `dispatched`/`running` (Settings.max_active_jobs_per_user is a separate,
        not-yet-enforced concern -- see that exception's docstring for why)."""
        ...

    async def count_conversation_backlog(self, conversation_id: uuid.UUID) -> int:
        """Same backlog states as count_user_backlog, scoped to one conversation --
        agentflow's shared API key admits many end users as one service account, so
        per-user backlog would otherwise throttle the whole campus as one identity
        (2026-08-20)."""
        ...

    async def count_global_backlog(self) -> int:
        """Same as count_user_backlog but system-wide, across every user -- what
        Settings.global_queue_cap bounds."""
        ...

    async def count_backlog_for_kinds(self, kinds: frozenset[str] | None = None) -> int:
        """Same `queued`/`retry_wait` backlog count as count_global_backlog, but scoped
        to `kinds` (None = every kind, matching count_global_backlog exactly) -- used by
        app/domain/jobs/admission.py's `estimate_wait_seconds` at ADMISSION time to give
        a brand-new job a `queue_position` before it has a row of its own to rank
        against (2026-08-20): since every job here is enqueued with priority=0 (pure
        FIFO within a backend lane), a new job always lands at the back, so "how many
        same-backend jobs are already queued/retry_wait" IS its position. See
        `queue_rank` below for the precise, aging-aware LIVE lookup used once the job
        actually has a row (GET /v1/jobs/{id})."""
        ...

    async def queue_rank(
        self, job_id: uuid.UUID, kinds: frozenset[str] | None = None
    ) -> int | None:
        """0-indexed count of same-`kinds` `queued`/`retry_wait` jobs that would be
        claimed BEFORE `job_id` under this queue's own real claim ordering (i.e. the
        same effective-priority-then-queued_at comparison `claim_next` itself uses --
        see each adapter's implementation for exactly which ordering that is, since
        InMemoryJobQueue and PostgresJobQueue don't compute it identically, per
        app/adapters/queue/postgres.py's module docstring "Fairness/aging" note).
        `kinds` should be the caller's own backend's kind set (see
        workflow_registry.kinds_for_backend) so a Gemini job's position isn't inflated
        by unrelated ComfyUI jobs sharing the same global backlog, or vice versa.

        Returns None if `job_id` doesn't exist, or exists but is no longer in
        `queued`/`retry_wait` state (already dispatched/running/terminal -- "position in
        the queue" is meaningless once a job has left it; callers should omit the ETA
        fields entirely in that case rather than showing a stale number)."""
        ...


class InMemoryJobQueue:
    """Deterministic in-memory JobQueue. Not thread-safe across processes (fine for
    single-process tests); mirrors the state machine transitions of the real queue."""

    def __init__(self) -> None:
        self._jobs: dict[uuid.UUID, QueuedJob] = {}
        self._order: list[uuid.UUID] = []

    async def enqueue(self, job: QueuedJob) -> QueuedJob:
        self._jobs[job.id] = job
        self._order.append(job.id)
        return job

    async def get(self, job_id: uuid.UUID) -> QueuedJob | None:
        return self._jobs.get(job_id)

    async def claim_next(
        self,
        worker_capacity: int = 1,
        kinds: frozenset[str] | None = None,
        max_active_per_user: int = 0,
    ) -> list[QueuedJob]:
        claimed: list[QueuedJob] = []
        now = datetime.now(timezone.utc)
        candidates = sorted(
            (
                j
                for j in self._jobs.values()
                if j.state in ("queued", "retry_wait")
                and (kinds is None or j.kind in kinds)
                and (j.available_at is None or j.available_at <= now)
            ),
            key=lambda j: (-j.effective_priority, j.queued_at),
        )
        # Pre-existing (dispatched/running) counts, PLUS whatever this same call claims
        # below -- see the Protocol docstring's "counting jobs claimed earlier in THIS
        # SAME batch too" note, otherwise a user with 3 queued jobs and capacity=3 would
        # still get all 3 in one tick even with max_active_per_user=1.
        active_counts: dict[uuid.UUID, int] = {}
        if max_active_per_user > 0:
            for j in self._jobs.values():
                if j.state in ("dispatched", "running"):
                    active_counts[j.user_id] = active_counts.get(j.user_id, 0) + 1
        for job in candidates:
            if len(claimed) >= worker_capacity:
                break
            if max_active_per_user > 0 and active_counts.get(job.user_id, 0) >= max_active_per_user:
                continue
            job.state = "dispatched"
            claimed.append(job)
            if max_active_per_user > 0:
                active_counts[job.user_id] = active_counts.get(job.user_id, 0) + 1
        return claimed

    async def claim_next_with_lease(
        self,
        worker_capacity: int,
        lease_owner: str,
        lease_seconds: float,
        kinds: frozenset[str] | None = None,
        max_active_per_user: int = 0,
    ) -> list[QueuedJob]:
        claimed = await self.claim_next(
            worker_capacity, kinds=kinds, max_active_per_user=max_active_per_user
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        for job in claimed:
            job.lease_owner = lease_owner
            job.lease_expires_at = expires_at
        return claimed

    async def list_active(self) -> list[QueuedJob]:
        return [j for j in self._jobs.values() if j.state in ("dispatched", "running")]

    async def count_active(self, kinds: frozenset[str] | None = None) -> int:
        return sum(
            1
            for j in self._jobs.values()
            if j.state in ("dispatched", "running") and (kinds is None or j.kind in kinds)
        )

    async def renew_lease(self, job_id: uuid.UUID, lease_seconds: float) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.state not in ("dispatched", "running"):
            return
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)

    # Terminal states: once a job reaches one of these, no further mark_* call may
    # change it -- see module docstring's "CONCURRENCY BUG FIXED HERE" note. Cheap
    # in-process equivalent of a Postgres `WHERE state NOT IN (...)` guard.
    _TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

    async def mark_running(self, job_id: uuid.UUID) -> None:
        if job_id in self._jobs:
            job = self._jobs[job_id]
            job.state = "running"
            # Clear any prompt_id left over from a previous (already-resolved) attempt
            # -- see module docstring. Without this, a reconciler pass landing while
            # THIS new attempt's own comfy_client.submit() is still in flight (which for
            # Gemini can take up to gemini_image_request_timeout_s) would resolve the
            # job using the stale prior attempt's prompt_id/outcome instead of waiting
            # for the new one.
            job.prompt_id = None

    async def mark_succeeded(self, job_id: uuid.UUID, result: dict) -> None:
        if job_id in self._jobs:
            job = self._jobs[job_id]
            if job.state in self._TERMINAL_STATES:
                return
            job.state = "succeeded"
            job.result = result
            job.lease_owner = None
            job.lease_expires_at = None
            # Clear any error_code/error_detail left over from an earlier FAILED
            # attempt of this same job (see the Protocol docstring's 2026-08-20 note)
            # -- otherwise a job that failed once, then succeeded on retry, keeps
            # showing a stale red error next to "succeeded" forever.
            job.error_code = None
            job.error_detail = None

    async def mark_failed(
        self, job_id: uuid.UUID, error_code: str, error_detail: str | None = None
    ) -> None:
        if job_id in self._jobs:
            job = self._jobs[job_id]
            if job.state in self._TERMINAL_STATES:
                return
            job.state = "failed"
            job.error_code = error_code
            job.error_detail = error_detail
            job.lease_owner = None
            job.lease_expires_at = None

    async def mark_retry_wait(
        self,
        job_id: uuid.UUID,
        error_code: str,
        error_detail: str | None = None,
        delay_seconds: float | None = None,
    ) -> None:
        if job_id in self._jobs:
            job = self._jobs[job_id]
            if job.state in self._TERMINAL_STATES:
                return
            job.state = "retry_wait"
            job.error_code = error_code
            job.error_detail = error_detail
            job.current_attempt += 1
            job.lease_owner = None
            job.lease_expires_at = None
            if delay_seconds is None:
                from app.domain.jobs.retry import compute_backoff_seconds

                delay_seconds = compute_backoff_seconds(job.current_attempt)
            job.available_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)

    async def set_prompt_id(self, job_id: uuid.UUID, prompt_id: str) -> None:
        if job_id in self._jobs:
            self._jobs[job_id].prompt_id = prompt_id

    async def cancel(self, job_id: uuid.UUID) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.state in ("succeeded", "failed", "cancelled"):
            return False
        job.state = "cancelled"
        return True

    async def find_by_idempotency_key(
        self, user_id: uuid.UUID, idempotency_key: str, kind: str
    ) -> QueuedJob | None:
        for job in self._jobs.values():
            if (
                job.user_id == user_id
                and job.idempotency_key == idempotency_key
                and job.kind == kind
            ):
                return job
        return None

    _BACKLOG_STATES = frozenset({"queued", "retry_wait"})

    async def count_user_backlog(self, user_id: uuid.UUID) -> int:
        return sum(
            1
            for j in self._jobs.values()
            if j.user_id == user_id and j.state in self._BACKLOG_STATES
        )

    async def count_conversation_backlog(self, conversation_id: uuid.UUID) -> int:
        return sum(
            1
            for j in self._jobs.values()
            if j.conversation_id == conversation_id and j.state in self._BACKLOG_STATES
        )

    async def count_global_backlog(self) -> int:
        return sum(1 for j in self._jobs.values() if j.state in self._BACKLOG_STATES)

    async def count_backlog_for_kinds(self, kinds: frozenset[str] | None = None) -> int:
        return sum(
            1
            for j in self._jobs.values()
            if j.state in self._BACKLOG_STATES and (kinds is None or j.kind in kinds)
        )

    async def queue_rank(
        self, job_id: uuid.UUID, kinds: frozenset[str] | None = None
    ) -> int | None:
        job = self._jobs.get(job_id)
        if job is None or job.state not in self._BACKLOG_STATES:
            return None
        # Same ordering claim_next itself uses for this adapter -- the STATIC
        # effective_priority column, not aging-recomputed (see module docstring's
        # "Fairness/aging" note on PostgresJobQueue for how that adapter differs).
        candidates = sorted(
            (
                j
                for j in self._jobs.values()
                if j.state in self._BACKLOG_STATES and (kinds is None or j.kind in kinds)
            ),
            key=lambda j: (-j.effective_priority, j.queued_at),
        )
        for idx, j in enumerate(candidates):
            if j.id == job_id:
                return idx
        return None
