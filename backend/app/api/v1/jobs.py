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
from app.api.deps import get_app_settings, get_current_user, get_job_queue, get_storage
from app.core.config import Settings
from app.db.models import User
from app.domain.jobs.ownership import NotOwnerError, assert_owner

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
    # Which comfyui-worker-N instance this job's CURRENT (or most recent) attempt was
    # dispatched to, e.g. "comfyui-worker-2:8188" -- None if the job hasn't been
    # dispatched yet, was routed to a non-ComfyUI backend (e.g. Gemini), or the encoding
    # is otherwise unrecognized. Purely additive/derived (no new column, no new query) --
    # see _worker_name_from_prompt_id's docstring for where the underlying data lives.
    worker_name: str | None = None


def _worker_name_from_prompt_id(prompt_id: str | None, worker_base_urls: list[str]) -> str | None:
    """Decodes the `"<worker_index>:<real_prompt_id>"` tag that
    `MultiWorkerComfyUIClient.submit()` stamps onto `QueuedJob.prompt_id` (see
    app/adapters/comfyui/multi_worker.py's module docstring) back into a human-readable
    worker name.

    Deliberately duplicates `Scheduler._worker_name`'s exact string transform
    (app/services/scheduler.py) rather than importing it -- that module pulls in the
    full scheduler/DB-session machinery, which this read-only status endpoint has no
    other reason to depend on. Keep the two in sync if the naming scheme ever changes;
    a mismatch here only degrades the displayed name, it can't corrupt job state.

    Returns None for: no prompt_id yet (job still queued/never dispatched), a prompt_id
    with no recognizable "<index>:" prefix (e.g. a non-ComfyUI backend's own id format),
    or an index outside the currently-configured worker list (stale id from a since-
    shrunk APP_COMFY_WORKER_BASE_URLS_CSV).
    """
    if not prompt_id:
        return None
    index_str, sep, _real_id = prompt_id.partition(":")
    if not sep:
        return None
    try:
        index = int(index_str)
        base_url = worker_base_urls[index]
    except (ValueError, IndexError):
        return None
    return base_url.replace("http://", "").replace("https://", "").replace("/", "-").rstrip("-")


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
    settings: Settings = Depends(get_app_settings),
) -> JobOut:
    job = await _get_owned_job(queue, job_id, user)
    return JobOut(
        id=str(job.id),
        state=job.state,
        kind=job.kind,
        current_attempt=job.current_attempt,
        error_code=job.error_code,
        error_detail=job.error_detail,
        result=job.result,
        worker_name=_worker_name_from_prompt_id(job.prompt_id, settings.comfy_worker_base_urls),
    )


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: str,
    queue: JobQueue = Depends(get_job_queue),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_app_settings),
) -> JobOut:
    job = await _get_owned_job(queue, job_id, user)
    await queue.cancel(job.id)
    job = await queue.get(job.id)
    return JobOut(
        id=str(job.id),
        state=job.state,
        kind=job.kind,
        current_attempt=job.current_attempt,
        error_code=job.error_code,
        error_detail=job.error_detail,
        result=job.result,
        worker_name=_worker_name_from_prompt_id(job.prompt_id, settings.comfy_worker_base_urls),
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
