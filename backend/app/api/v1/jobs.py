"""Job status, SSE event stream, and cancellation."""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.adapters.queue import JobQueue
from app.api.deps import get_current_user, get_job_queue
from app.db.models import User
from app.domain.jobs.ownership import NotOwnerError, assert_owner

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobOut(BaseModel):
    id: str
    state: str
    kind: str
    current_attempt: int
    error_code: str | None = None
    result: dict | None = None


async def _get_owned_job(queue: JobQueue, job_id: str, user: User):
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    job = await queue.get(job_uuid)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    try:
        assert_owner(str(job.user_id), str(user.id))
    except NotOwnerError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return job


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    queue: JobQueue = Depends(get_job_queue),
    user: User = Depends(get_current_user),
) -> JobOut:
    job = await _get_owned_job(queue, job_id, user)
    return JobOut(
        id=str(job.id), state=job.state, kind=job.kind,
        current_attempt=job.current_attempt, error_code=job.error_code, result=job.result,
    )


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: str,
    queue: JobQueue = Depends(get_job_queue),
    user: User = Depends(get_current_user),
) -> JobOut:
    job = await _get_owned_job(queue, job_id, user)
    await queue.cancel(job.id)
    job = await queue.get(job.id)
    return JobOut(
        id=str(job.id), state=job.state, kind=job.kind,
        current_attempt=job.current_attempt, error_code=job.error_code, result=job.result,
    )


@router.get("/{job_id}/events")
async def job_events(
    job_id: str,
    queue: JobQueue = Depends(get_job_queue),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events stream of job-state transitions until a terminal state.

    Polls the JobQueue port rather than assuming a specific pub/sub backend, so it works
    identically against the in-memory fake and the real Redis-backed implementation.
    """
    job = await _get_owned_job(queue, job_id, user)

    async def event_stream():
        last_state = None
        terminal = {"succeeded", "failed", "cancelled"}
        for _ in range(600):  # hard cap so a leaked connection can't stream forever
            current = await queue.get(job.id)
            if current is None:
                break
            if current.state != last_state:
                data = {"id": str(current.id), "state": current.state, "error_code": current.error_code}
                yield f"event: state\ndata: {json.dumps(data)}\n\n"
                last_state = current.state
            if current.state in terminal:
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
