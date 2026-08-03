"""JobQueue port + adapters.

The real implementation claims rows from `generation_jobs` in Postgres using
`SELECT ... FOR UPDATE SKIP LOCKED` (see backend/app/scheduler). This module defines the
port plus a deterministic in-memory fake used by tests and local dev without Postgres/Redis.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    result: dict | None = None


class JobQueue(Protocol):
    async def enqueue(self, job: QueuedJob) -> QueuedJob: ...

    async def get(self, job_id: uuid.UUID) -> QueuedJob | None: ...

    async def claim_next(self, worker_capacity: int = 1) -> list[QueuedJob]: ...

    async def mark_running(self, job_id: uuid.UUID) -> None: ...

    async def mark_succeeded(self, job_id: uuid.UUID, result: dict) -> None: ...

    async def mark_failed(self, job_id: uuid.UUID, error_code: str) -> None: ...

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

    async def mark_running(self, job_id: uuid.UUID) -> None:
        if job_id in self._jobs:
            self._jobs[job_id].state = "running"

    async def mark_succeeded(self, job_id: uuid.UUID, result: dict) -> None:
        if job_id in self._jobs:
            job = self._jobs[job_id]
            job.state = "succeeded"
            job.result = result

    async def mark_failed(self, job_id: uuid.UUID, error_code: str) -> None:
        if job_id in self._jobs:
            job = self._jobs[job_id]
            job.state = "failed"
            job.error_code = error_code

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
