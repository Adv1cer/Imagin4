"""Shared job-admission logic: workflow validation, idempotency-key handling, and
QueuedJob construction. Extracted from app/api/v1/generations.py so the agentic chat
router (app/api/v1/chat_router.py) admits jobs through the exact same path -- one
validated way into the queue, not two slightly-different copies that could drift.

Deliberately framework-free (no FastAPI/HTTPException here) so it stays usable from
non-HTTP callers too; raises domain-level exceptions that each caller translates into
whatever error shape is appropriate for it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.adapters.queue import JobQueue, QueuedJob
from app.domain.jobs.idempotency import (
    IdempotencyOutcome,
    canonical_payload_hash,
    check_idempotency,
)
from app.domain.jobs.workflow_registry import UnknownWorkflowError, resolve_workflow

__all__ = ["AdmissionResult", "IdempotencyConflictError", "admit_generation_job", "UnknownWorkflowError"]


class IdempotencyConflictError(ValueError):
    """The same (user, idempotency_key, kind) was already used with a different
    payload -- a client bug/conflict, not a transient failure."""


@dataclass(frozen=True)
class AdmissionResult:
    id: str
    state: str
    kind: str
    replayed: bool


async def admit_generation_job(
    queue: JobQueue,
    user_id: uuid.UUID,
    workflow_name: str,
    workflow_version: str,
    inputs: dict,
    idempotency_key: str,
) -> AdmissionResult:
    """Validates `(workflow_name, workflow_version)` against the server-side allowlist
    (app/domain/jobs/workflow_registry.py -- callers never get to pass an arbitrary
    workflow through), applies idempotency-key replay/conflict semantics identical to
    POST /v1/generations, and enqueues a new QueuedJob if this is genuinely new.

    Raises `UnknownWorkflowError` for an unrecognized workflow and
    `IdempotencyConflictError` for a key reused with a different payload; callers map
    those to their own appropriate HTTP status (400 / 409 in the existing endpoints).
    """
    resolve_workflow(workflow_name, workflow_version)  # raises UnknownWorkflowError

    kind = workflow_name
    existing = None
    if hasattr(queue, "find_by_idempotency_key"):
        existing = await queue.find_by_idempotency_key(user_id, idempotency_key, kind)

    existing_hash = (
        canonical_payload_hash(existing.input_payload) if existing is not None else None
    )
    check = check_idempotency(
        existing_job_id=str(existing.id) if existing else None,
        existing_payload_hash=existing_hash,
        new_payload=inputs,
    )

    if check.outcome == IdempotencyOutcome.CONFLICT:
        raise IdempotencyConflictError(
            "Idempotency-Key already used with a different payload"
        )
    if check.outcome == IdempotencyOutcome.REPLAY and existing is not None:
        return AdmissionResult(
            id=str(existing.id), state=existing.state, kind=existing.kind, replayed=True
        )

    job = QueuedJob(
        id=uuid.uuid4(),
        user_id=user_id,
        kind=kind,
        state="queued",
        priority=0,
        effective_priority=0.0,
        input_payload=inputs,
        idempotency_key=idempotency_key,
        queued_at=datetime.now(timezone.utc),
    )
    await queue.enqueue(job)
    return AdmissionResult(id=str(job.id), state=job.state, kind=job.kind, replayed=False)
