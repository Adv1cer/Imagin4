"""POST /v1/generations: admits a new generation job (or returns the existing one for a
replayed Idempotency-Key) and hands it to the JobQueue for the scheduler to pick up.

Admission logic itself lives in app/domain/jobs/admission.py, shared with the agentic
chat router's GENERAL_IMAGE path and its POSTER/INFOGRAPHIC confirm step (see
app/api/v1/chat_router.py) so there is exactly one validated way into the job queue.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel

from app.adapters.queue import JobQueue
from app.api.deps import get_current_user, get_job_queue
from app.db.models import User
from app.domain.jobs.admission import (
    IdempotencyConflictError,
    UnknownWorkflowError,
    admit_generation_job,
)

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
        result = await admit_generation_job(
            queue=queue,
            user_id=user.id,
            workflow_name=payload.workflow_name,
            workflow_version=payload.workflow_version,
            inputs=payload.inputs,
            idempotency_key=idempotency_key,
        )
    except UnknownWorkflowError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown workflow")
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key already used with a different payload",
        )

    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return GenerationOut(id=result.id, state=result.state, kind=result.kind)
