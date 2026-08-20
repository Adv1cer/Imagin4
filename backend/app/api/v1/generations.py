"""POST /v1/generations: admits a new generation job (or returns the existing one for a
replayed Idempotency-Key) and hands it to the JobQueue for the scheduler to pick up.

Admission logic itself lives in app/domain/jobs/admission.py, shared with the agentic
chat router's GENERAL_IMAGE path and its POSTER/INFOGRAPHIC confirm step (see
app/api/v1/chat_router.py) so there is exactly one validated way into the job queue.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel

from app.adapters.queue import JobQueue
from app.api.deps import check_admission_capacity, get_current_user, get_job_queue, rate_limited
from app.db.models import User
from app.domain.jobs.admission import (
    CapacityExceededError,
    IdempotencyConflictError,
    InvalidComfyOverrideError,
    PreviewNotSupportedError,
    UnknownModelProfileError,
    UnknownWorkflowError,
    admit_generation_job,
)
from app.domain.jobs.ownership import NotOwnerError, assert_owner

router = APIRouter(prefix="/generations", tags=["generations"])


class GenerationCreate(BaseModel):
    workflow_name: str
    workflow_version: str
    conversation_id: str | None = None
    # Free-form, but admit_generation_job validates known keys server-side rather than
    # forwarding them unchecked. For a ComfyUI-backed workflow, `inputs` may include
    # `model_profile` (e.g. "student"/"personnel" -- see
    # app/domain/jobs/comfy_profiles.py) to select which server-configured model/
    # quality tier generates this job; omitted or None resolves to "student". May also
    # include `model_overrides` (see app/domain/jobs/comfy_overrides.py) -- an object of
    # individual field overrides (checkpoint_name/diffusion_model_name/clip_name/
    # vae_name/sampler_name/scheduler/steps/cfg_scale/negative_prompt) layered on top of
    # the resolved profile, each checked against a server-side allowlist/range before
    # use. Callers still never supply arbitrary values that bypass that allowlist, and
    # never pick model_family directly (see that module's docstring for why). May also
    # include `preview: true` (2026-08-20, ComfyUI-backed workflows only -- see
    # app/domain/jobs/admission.py's PreviewNotSupportedError and
    # Settings.preview_steps for the full scope note) to force a fast, low-step
    # preview render instead of full quality; pair with POST
    # /v1/generations/{id}/finalize below once the preview looks right.
    inputs: dict


class GenerationOut(BaseModel):
    id: str
    state: str
    kind: str
    # Populated only by POST /v1/agent/message when called with wait=true (see
    # app/api/v1/agent_router.py) and the job reached a terminal state -- every other
    # caller of GenerationOut leaves these None, so this is additive/backward-compatible.
    error_code: str | None = None
    error_detail: str | None = None
    # 2026-08-20 (see app/domain/jobs/admission.py:AdmissionResult/estimate_wait_seconds
    # for exactly what these mean and don't promise): 0-indexed position among this
    # job's own backend's queued/retry_wait backlog at the moment it was admitted, and a
    # rough derived ETA. Both None for an idempotent-replay response or a pending-action
    # confirm's replay path (see chat_router.py) -- those don't recompute a fresh
    # position for an already-existing job.
    queue_position: int | None = None
    estimated_wait_seconds: float | None = None
    # 2026-08-20, see AdmissionResult.is_preview's docstring -- true if this job was
    # admitted with `inputs.preview` truthy (fast/low-step render, not final quality).
    is_preview: bool = False


@router.post(
    "",
    response_model=GenerationOut,
    status_code=status.HTTP_202_ACCEPTED,
    # check_admission_capacity runs first (no DB touched yet) and sheds excess load with
    # a fast 503; rate_limited runs after auth and caps a single user's own throughput.
    # See app/core/rate_limit.py for the 2026-08-18 burst-test incident these exist for.
    dependencies=[
        Depends(check_admission_capacity),
        Depends(rate_limited("generation", "rl_generation_per_min")),
    ],
)
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
    except UnknownModelProfileError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown model_profile")
    except InvalidComfyOverrideError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except PreviewNotSupportedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key already used with a different payload",
        )
    except CapacityExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.reason,
            headers={"Retry-After": str(exc.retry_after_s)},
        )

    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return GenerationOut(
        id=result.id,
        state=result.state,
        kind=result.kind,
        queue_position=result.queue_position,
        estimated_wait_seconds=result.estimated_wait_seconds,
        is_preview=result.is_preview,
    )


@router.post(
    "/{job_id}/finalize",
    response_model=GenerationOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        Depends(check_admission_capacity),
        Depends(rate_limited("generation", "rl_generation_per_min")),
    ],
)
async def finalize_generation(
    job_id: str,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    queue: JobQueue = Depends(get_job_queue),
    user: User = Depends(get_current_user),
) -> GenerationOut:
    """Second half of the preview/final two-stage flow (2026-08-20, see
    GenerationCreate.inputs' `preview` note and Settings.preview_steps for the scope
    this covers): submits a NEW, full-quality job for the same prompt/settings a prior
    `preview` job was admitted with, restoring whatever `model_overrides` the caller
    originally sent (before preview forced `steps` down) -- see
    admit_generation_job's `preview_original_overrides` bookkeeping.

    Requires the referenced job to (a) exist and be owned by the caller -- 404
    otherwise, same as GET /v1/jobs/{id}, (b) have actually been admitted with
    `preview` truthy -- 400 "job is not a preview job" otherwise, and (c) have already
    reached `succeeded` -- 409 otherwise, since finalizing a preview the caller hasn't
    actually seen yet isn't a request this endpoint can make sense of. Goes through
    the exact same `admit_generation_job` path as POST /v1/generations above (new
    Idempotency-Key required), so it gets the same capacity/backlog/queue_position
    treatment as any other new job -- this is NOT a variant of the original job, it is
    a genuinely new one, linked back via `inputs.preview_of` for traceability.
    """
    try:
        original_id = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    original = await queue.get(original_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    try:
        assert_owner(str(original.user_id), str(user.id))
    except NotOwnerError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    if not (original.input_payload or {}).get("is_preview"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="job is not a preview job"
        )
    if original.state != "succeeded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"preview job has not finished yet (state={original.state})",
        )

    final_inputs = dict(original.input_payload)
    final_inputs.pop("preview", None)
    final_inputs.pop("is_preview", None)
    final_inputs["model_overrides"] = final_inputs.pop("preview_original_overrides", {})
    final_inputs["preview_of"] = str(original.id)

    try:
        result = await admit_generation_job(
            queue=queue,
            user_id=user.id,
            workflow_name=original.kind,
            workflow_version="v1",  # see workflow_registry.kinds_for_backend's docstring
            inputs=final_inputs,
            idempotency_key=idempotency_key,
        )
    except UnknownWorkflowError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown workflow")
    except UnknownModelProfileError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown model_profile")
    except InvalidComfyOverrideError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except PreviewNotSupportedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key already used with a different payload",
        )
    except CapacityExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.reason,
            headers={"Retry-After": str(exc.retry_after_s)},
        )

    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return GenerationOut(
        id=result.id,
        state=result.state,
        kind=result.kind,
        queue_position=result.queue_position,
        estimated_wait_seconds=result.estimated_wait_seconds,
        is_preview=result.is_preview,
    )
