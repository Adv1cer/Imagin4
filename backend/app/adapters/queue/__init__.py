"""JobQueue port + adapters.

The real implementation claims rows from `generation_jobs` in Postgres using
`SELECT ... FOR UPDATE SKIP LOCKED` (see backend/app/scheduler). This module defines the
port plus a deterministic in-memory fake used by tests and local dev without Postgres/Redis.
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

    async def claim_next(self, worker_capacity: int = 1) -> list[QueuedJob]: ...

    async def claim_next_with_lease(
        self, worker_capacity: int, lease_owner: str, lease_seconds: float
    ) -> list[QueuedJob]:
        """Fairness-ordered claim (same selection as claim_next) that additionally stamps
        a lease_owner/lease_expires_at on each claimed job, so a reconciler can later find
        dispatched/running jobs whose lease expired without a heartbeat/finalization
        (crashed scheduler, worker, or lost connection to ComfyUI)."""
        ...

    async def list_active(self) -> list[QueuedJob]:
        """Returns jobs currently in `dispatched` or `running` state, for reconciliation."""
        ...

    async def mark_running(self, job_id: uuid.UUID) -> None: ...

    async def mark_succeeded(self, job_id: uuid.UUID, result: dict) -> None: ...

    async def mark_failed(
        self, job_id: uuid.UUID, error_code: str, error_detail: str | None = None
    ) -> None: ...

    async def mark_retry_wait(
        self, job_id: uuid.UUID, error_code: str, error_detail: str | None = None
    ) -> None: ...

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

    async def claim_next(self, worker_capacity: int = 1) -> list[QueuedJob]:
        claimed: list[QueuedJob] = []
        candidates = sorted(
            (j for j in self._jobs.values() if j.state in ("queued", "retry_wait")),
            key=lambda j: (-j.effective_priority, j.queued_at),
        )
        for job in candidates[:worker_capacity]:
            job.state = "dispatched"
            claimed.append(job)
        return claimed

    async def claim_next_with_lease(
        self, worker_capacity: int, lease_owner: str, lease_seconds: float
    ) -> list[QueuedJob]:
        claimed = await self.claim_next(worker_capacity)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        for job in claimed:
            job.lease_owner = lease_owner
            job.lease_expires_at = expires_at
        return claimed

    async def list_active(self) -> list[QueuedJob]:
        return [j for j in self._jobs.values() if j.state in ("dispatched", "running")]

    async def mark_running(self, job_id: uuid.UUID) -> None:
        if job_id in self._jobs:
            self._jobs[job_id].state = "running"

    async def mark_succeeded(self, job_id: uuid.UUID, result: dict) -> None:
        if job_id in self._jobs:
            job = self._jobs[job_id]
            job.state = "succeeded"
            job.result = result
            job.lease_owner = None
            job.lease_expires_at = None

    async def mark_failed(
        self, job_id: uuid.UUID, error_code: str, error_detail: str | None = None
    ) -> None:
        if job_id in self._jobs:
            job = self._jobs[job_id]
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
