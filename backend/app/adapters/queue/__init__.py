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


class JobQueue(Protocol):
    async def enqueue(self, job: QueuedJob) -> QueuedJob: ...

    async def get(self, job_id: uuid.UUID) -> QueuedJob | None: ...

    async def claim_next(
        self, worker_capacity: int = 1, kinds: frozenset[str] | None = None
    ) -> list[QueuedJob]:
        """`kinds`, when given, restricts candidates to jobs whose `kind` is in the set --
        used by the scheduler to claim ComfyUI-backend and Gemini-backend jobs against
        independent capacity numbers (see app/services/scheduler.py and
        app/domain/jobs/workflow_registry.py:kinds_for_backend) so a slow Gemini
        poster/infographic job can't starve an unrelated ComfyUI job's slot or vice
        versa. None (default) means no filtering -- every existing caller keeps its
        original behavior."""
        ...

    async def claim_next_with_lease(
        self,
        worker_capacity: int,
        lease_owner: str,
        lease_seconds: float,
        kinds: frozenset[str] | None = None,
    ) -> list[QueuedJob]:
        """Fairness-ordered claim (same selection as claim_next, including the same
        `kinds` filter) that additionally stamps a lease_owner/lease_expires_at on each
        claimed job, so a reconciler can later find dispatched/running jobs whose lease
        expired without a heartbeat/finalization (crashed scheduler, worker, or lost
        connection to ComfyUI)."""
        ...

    async def list_active(self) -> list[QueuedJob]:
        """Returns jobs currently in `dispatched` or `running` state, for reconciliation."""
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
        project's documented "every transition uses a conditional update" invariant."""
        ...

    async def mark_failed(
        self, job_id: uuid.UUID, error_code: str, error_detail: str | None = None
    ) -> None:
        """Same terminal-state guard as mark_succeeded above."""
        ...

    async def mark_retry_wait(
        self, job_id: uuid.UUID, error_code: str, error_detail: str | None = None
    ) -> None:
        """Same terminal-state guard as mark_succeeded above."""
        ...

    async def set_prompt_id(self, job_id: uuid.UUID, prompt_id: str) -> None: ...

    async def cancel(self, job_id: uuid.UUID) -> bool: ...


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
        self, worker_capacity: int = 1, kinds: frozenset[str] | None = None
    ) -> list[QueuedJob]:
        claimed: list[QueuedJob] = []
        candidates = sorted(
            (
                j
                for j in self._jobs.values()
                if j.state in ("queued", "retry_wait") and (kinds is None or j.kind in kinds)
            ),
            key=lambda j: (-j.effective_priority, j.queued_at),
        )
        for job in candidates[:worker_capacity]:
            job.state = "dispatched"
            claimed.append(job)
        return claimed

    async def claim_next_with_lease(
        self,
        worker_capacity: int,
        lease_owner: str,
        lease_seconds: float,
        kinds: frozenset[str] | None = None,
    ) -> list[QueuedJob]:
        claimed = await self.claim_next(worker_capacity, kinds=kinds)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        for job in claimed:
            job.lease_owner = lease_owner
            job.lease_expires_at = expires_at
        return claimed

    async def list_active(self) -> list[QueuedJob]:
        return [j for j in self._jobs.values() if j.state in ("dispatched", "running")]

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
        self, job_id: uuid.UUID, error_code: str, error_detail: str | None = None
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
