"""Job status, SSE event stream, and cancellation."""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.adapters.queue import JobQueue
from app.adapters.storage import ObjectStorage
from app.api.deps import get_current_user, get_job_queue, get_storage
from app.core.config import get_settings
from app.db.models import User
from app.domain.jobs.admission import estimate_wait_seconds
from app.domain.jobs.ownership import NotOwnerError, assert_owner
from app.domain.jobs.workflow_registry import backend_for_kind, kinds_for_backend

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobOut(BaseModel):
    id: str
    state: str
    kind: str
    current_attempt: int
    error_code: str | None = None
    # The underlying adapter's own sanitized error (e.g. "gemini_error:ClientError",
    # "gemini_not_configured", "gemini_no_image_in_response") -- error_code is only the
    # reconciler's coarse retry-classification bucket and is the same value ("comfy_transient")
    # whether ComfyUI or Gemini actually failed, so this is the field that tells you which
    # backend handled the job and why it failed without reading server logs.
    error_detail: str | None = None
    result: dict | None = None
    # 2026-08-20 (see app/domain/jobs/admission.py:estimate_wait_seconds): a LIVE,
    # aging-aware position within this job's own backend's queue, recomputed on every
    # GET -- unlike GenerationOut's queue_position (a snapshot taken once, at admission
    # time), this one tracks the job as it moves up the queue. Both None once the job
    # has left `queued`/`retry_wait` (dispatched/running/terminal) -- see
    # JobQueue.queue_rank's docstring for why a stale position is worse than none.
    queue_position: int | None = None
    estimated_wait_seconds: float | None = None
    # 2026-08-20, see app/domain/jobs/admission.py's AdmissionResult.is_preview
    # docstring -- read directly off input_payload (no extra query needed, unlike the
    # queue_position/ETA fields above) since QueuedJob already carries it.
    is_preview: bool = False


async def _live_queue_eta(queue: JobQueue, job) -> tuple[int | None, float | None]:
    """Shared by get_job/cancel_job below. `hasattr` guard matches this module's
    existing fail-open style (see app/domain/jobs/admission.py's own convention) --
    a JobQueue that doesn't implement queue_rank just doesn't get live ETA fields."""
    if job.state not in ("queued", "retry_wait") or not hasattr(queue, "queue_rank"):
        return None, None
    backend = backend_for_kind(job.kind)
    if backend is None:
        return None, None
    position = await queue.queue_rank(job.id, kinds_for_backend(backend))
    if position is None:
        return None, None
    return position, estimate_wait_seconds(position, backend, get_settings())


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
    position, eta = await _live_queue_eta(queue, job)
    return JobOut(
        id=str(job.id),
        state=job.state,
        kind=job.kind,
        current_attempt=job.current_attempt,
        error_code=job.error_code,
        error_detail=job.error_detail,
        result=job.result,
        queue_position=position,
        estimated_wait_seconds=eta,
        is_preview=bool((job.input_payload or {}).get("is_preview", False)),
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
    # A cancelled job is terminal, so this always resolves to (None, None) via the same
    # state-check _live_queue_eta itself does -- calling it (rather than hardcoding None
    # here) just means a future state_machine change (e.g. a genuine cancelling->
    # cancelled path, see app/adapters/queue/postgres.py's module docstring) can't
    # silently leave this endpoint out of sync with get_job.
    position, eta = await _live_queue_eta(queue, job)
    return JobOut(
        id=str(job.id),
        state=job.state,
        kind=job.kind,
        current_attempt=job.current_attempt,
        error_code=job.error_code,
        error_detail=job.error_detail,
        result=job.result,
        queue_position=position,
        estimated_wait_seconds=eta,
        is_preview=bool((job.input_payload or {}).get("is_preview", False)),
    )


@router.get("/{job_id}/asset")
async def get_job_asset(
    job_id: str,
    index: int = 0,
    queue: JobQueue = Depends(get_job_queue),
    storage: ObjectStorage = Depends(get_storage),
    user: User = Depends(get_current_user),
) -> Response:
    """Streams the raw bytes of one of the job's generated outputs, ownership-checked
    via the same `_get_owned_job` path as GET /{job_id}. A dedicated
    signed-URL-issuing endpoint (per the original architecture doc) is the production
    path for a real S3/MinIO deployment; this direct-stream endpoint is the pragmatic
    equivalent for the in-memory/dev storage adapter and works unchanged against
    either -- the client never needs to know which one is behind it."""
    job = await _get_owned_job(queue, job_id, user)
    outputs = (job.result or {}).get("outputs") or []
    if not (0 <= index < len(outputs)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")

    object_key = outputs[index].get("object_key")
    mime_type = outputs[index].get("mime_type", "application/octet-stream")
    if not object_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")

    try:
        data = await storage.get_object(object_key)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")

    return Response(content=data, media_type=mime_type)


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
                data = {
                    "id": str(current.id),
                    "state": current.state,
                    "error_code": current.error_code,
                    "error_detail": current.error_detail,
                }
                yield f"event: state\ndata: {json.dumps(data)}\n\n"
                last_state = current.state
            if current.state in terminal:
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
