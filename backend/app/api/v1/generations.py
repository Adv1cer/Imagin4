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
from app.api.deps import check_admission_capacity, get_current_user, get_job_queue, rate_limited
from app.db.models import User
from app.domain.jobs.admission import (
    IdempotencyConflictError,
    InvalidComfyOverrideError,
    UnknownModelProfileError,
    UnknownWorkflowError,
    admit_generation_job,
)

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
    # never pick model_family directly (see that module's docstring for why).
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
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key already used with a different payload",
        )

    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return GenerationOut(id=result.id, state=result.state, kind=result.kind)
