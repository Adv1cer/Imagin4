"""POST /v1/generations: admits a new generation job (or returns the existing one for a
replayed Idempotency-Key) and hands it to the JobQueue for the scheduler to pick up."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel

from app.adapters.queue import JobQueue, QueuedJob
from app.api.deps import get_current_user, get_job_queue
from app.db.models import User
from app.domain.jobs.idempotency import canonical_payload_hash, check_idempotency, IdempotencyOutcome
from app.domain.jobs.workflow_registry import UnknownWorkflowError, resolve_workflow

router = APIRouter(prefix="/generations", tags=["generations"])


class GenerationCreate(BaseModel):
    workflow_name: str
    workflow_version: str
    conversation_id: str | None = None
    inputs: dict


class GenerationOut(BaseModel):
    id: str
    state: str
    kind: str


@router.post("", response_model=GenerationOut, status_code=status.HTTP_202_ACCEPTED)
async def create_generation(
    payload: GenerationCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    queue: JobQueue = Depends(get_job_queue),
    user: User = Depends(get_current_user),
) -> GenerationOut:
    try:
        workflow = resolve_workflow(payload.workflow_name, payload.workflow_version)
    except UnknownWorkflowError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown workflow")

    kind = payload.workflow_name
    existing = None
    if hasattr(queue, "find_by_idempotency_key"):
        existing = await queue.find_by_idempotency_key(user.id, idempotency_key, kind)

    new_hash = canonical_payload_hash(payload.inputs)
    existing_hash = (
        canonical_payload_hash(existing.input_payload) if existing is not None else None
    )
    check = check_idempotency(
        existing_job_id=str(existing.id) if existing else None,
        existing_payload_hash=existing_hash,
        new_payload=payload.inputs,
    )

    if check.outcome == IdempotencyOutcome.CONFLICT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key already used with a different payload",
        )
    if check.outcome == IdempotencyOutcome.REPLAY and existing is not None:
        response.headers["Idempotency-Replayed"] = "true"
        return GenerationOut(id=str(existing.id), state=existing.state, kind=existing.kind)

    job = QueuedJob(
        id=uuid.uuid4(),
        user_id=user.id,
        kind=kind,
        state="queued",
        priority=0,
        effective_priority=0.0,
        input_payload=payload.inputs,
        idempotency_key=idempotency_key,
        queued_at=datetime.now(timezone.utc),
    )
    await queue.enqueue(job)
    return GenerationOut(id=str(job.id), state=job.state, kind=job.kind)
