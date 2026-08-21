"""POST /v1/agent/message -- machine-to-machine entry point into the same agentic chat
pipeline as POST /v1/conversations/{id}/smart-message (see app/api/v1/chat_router.py),
added 2026-08 for external systems that just want to forward raw text and get back a
chat reply or an enqueued image job, without first calling POST /v1/conversations
themselves.

Concretely: a university's own chatbot/workflow-automation platform proxies many real
end users' messages through this backend using ONE shared API key (see
app/domain/auth/api_keys.py). If every forwarded message landed in the same
conversation, every one of those unrelated end users' chat history/context would bleed
into each other. `external_conversation_id` solves that: the caller passes through
whatever it already uses to mean "this is the same end user/thread as last time" (e.g.
its own per-user id), and this endpoint transparently finds-or-creates a real
Conversation scoped to (authenticated user_id, that external id) -- so isolation is
correct even though every request authenticates as the same service account. The caller
never needs to know this system's own conversation_id, POST /v1/conversations, or
manage any state beyond that one external id.

Auth: same `get_current_user` dependency as every other endpoint, so it works with
either a session token/cookie or an `Authorization: Bearer imgn_...` API key -- nothing
here hard-requires an API key specifically, but this endpoint exists primarily to make
API-key callers ergonomic (no session-token human login flow to script around).

PRODUCT DECISION (2026-08): `wait=true` deliberately breaks this repo's normal "API must
never keep an HTTP request open until an image finishes" invariant (see project
instructions / architecture doc). It exists only because the specific external caller
this endpoint was built for (a no-code chatbot workflow builder) has no retry/wait/loop
node at all, so it is structurally unable to poll GET /v1/jobs/{id} itself -- without
this, that caller cannot get a finished image back through any number of nodes. Scoped
narrowly on purpose: opt-in (default False), bounded by WAIT_MAX_TIMEOUT_S regardless of
what the caller asks for, and only meaningful for this one endpoint -- smart-message and
POST /v1/generations are both unchanged and still respond immediately. If this pattern
needs to support many concurrent waiting callers later, move the wait loop off the
request-scoped DB session/connection-pool slot (see `agent_message`'s comment) rather
than reusing this implementation as-is.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.queue import JobQueue, QueuedJob
from app.api.deps import (
    check_admission_capacity,
    get_current_user,
    get_db_session,
    get_gemini_text_client,
    get_job_queue,
    get_redis,
)
from app.api.v1.chat_router import SmartMessageOut, process_routed_message
from app.api.v1.conversations import _append_message
from app.core.config import get_settings
from app.core.rate_limit import check_rate_limit, seconds_until_window_reset
from app.db.models import Conversation, User

router = APIRouter(prefix="/agent", tags=["agent"])

WAIT_DEFAULT_TIMEOUT_S = 100.0
WAIT_MAX_TIMEOUT_S = 170.0
WAIT_MIN_TIMEOUT_S = 5.0
WAIT_POLL_INTERVAL_S = 2.0

_TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "cancelled"})


class AgentMessageIn(BaseModel):
    # Whatever the calling system already uses to identify "this end user/thread" --
    # opaque to us, just used as a stable lookup key. Required and non-blank (validated
    # below) because skipping it would mean every caller using this key shares one
    # conversation, which defeats the whole point of this endpoint.
    external_conversation_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)
    client_message_id: str | None = None
    # When True and the routed result is an image job, this request blocks (polling the
    # queue server-side) until the job reaches a terminal state or wait_timeout_s
    # elapses, instead of responding immediately with state="queued". See this module's
    # docstring for why this exists and why it's opt-in.
    wait: bool = False
    wait_timeout_s: float | None = Field(default=None, gt=0)
    # 2026-08-19 (Chet + Opal): optional ComfyUI model tier ("student"/"personnel", see
    # app/domain/jobs/comfy_profiles.py) for whichever GENERAL_IMAGE job this message
    # routes to, if any -- agentflow already knows the requesting end-user's role and is
    # the intended owner of this decision (see that module's docstring). None/omitted ->
    # "student", same as before this field existed. An unrecognized value fails the job
    # with a clear error (see admit_generation_job's UnknownModelProfileError handling
    # below), not a silent fallback to "student".
    model_profile: str | None = Field(default=None, max_length=64)
    # 2026-08-19 (Chet + Opal): optional per-request ComfyUI param overrides (steps/
    # cfg_scale/etc -- same shape as POST /v1/generations' inputs.model_overrides, see
    # app/domain/jobs/comfy_overrides.py). Still validated against the server-side
    # allowlist/range by admit_generation_job -- a bounded escape hatch, not arbitrary
    # passthrough. Omitted/None -> the resolved model_profile's own tuned .env defaults
    # apply, unchanged from before this field existed. Still no Idempotency-Key needed
    # here -- this endpoint always derives its own key per message (see
    # process_routed_message's docstring).
    model_overrides: dict | None = None
    # 2026-08-19 (Chet + Opal): see process_routed_message's docstring
    # (app/api/v1/chat_router.py) for the full reasoning. On its own this only skips the
    # ComfyUI-side design step (comfy_prompt_designer) -- it does NOT stop route_intent
    # from rewriting `text` into `normalized_prompt` first. Combine with
    # `assume_image=True` below for a caller (e.g. one running its own web-search-backed
    # prompt engineering upstream) that wants `text` to reach ComfyUI byte-for-byte
    # untouched.
    skip_prompt_design: bool = False
    # 2026-08-19 (Chet + Opal): when True, skips route_intent/decide_next_step entirely
    # and always enqueues `text` as a GENERAL_IMAGE job verbatim -- for a caller that has
    # already classified this message as "ready to generate" upstream (e.g. agentflow's
    # own Intent Router node) and finds route_intent's re-classification + prompt rewrite
    # pure redundant cost/latency on top of its own. Default False preserves every
    # existing caller's behavior (route_intent still decides chat vs image vs
    # needs-clarification, as before this field existed). Pair with
    # skip_prompt_design=True to get a fully untouched prompt end-to-end; using this
    # alone still lets qwen-brain's comfy_prompt_designer rewrite `text` one step later.
    assume_image: bool = False
    # 2026-08-19 (Chet + Opal): only consulted when assume_image=True -- with
    # route_intent skipped there's no RouteDecision.exact_text to draw literal on-image
    # text (signs/captions) from, so a caller wanting that must pass it directly here.
    # Ignored when assume_image=False (route_intent's own decision.exact_text is used
    # instead, unchanged from before this field existed).
    exact_text: list[str] | None = None
    # 2026-08-19 (Chet + Opal): output size, for the EnqueueGeneralImage path only (same
    # scope as model_profile/model_overrides above). Unlike model_overrides' steps/
    # cfg_scale, these two are NOT validated against a configured allowlist -- they don't
    # need one: app/adapters/comfyui/live.py's _resolve_dimensions() already falls back
    # to a safe default ("1:1"/"1K") for any unrecognized value with a plain dict
    # .get(..., default), so there's no invalid/out-of-range input to reject in the first
    # place. This was already reachable via POST /v1/generations (inputs is a free-form
    # dict there) -- these two fields just expose the same existing capability here,
    # since agentflow's flow uses this endpoint, not /v1/generations.
    aspect_ratio: str | None = None
    resolution: str | None = None


async def _wait_for_terminal_state(
    queue: JobQueue, job_id: uuid.UUID, timeout_s: float
) -> QueuedJob | None:
    """Polls until the job leaves queued/running/etc, or timeout_s elapses -- whichever
    comes first. Returns the last-seen QueuedJob (which may still be non-terminal if we
    timed out) or None if the job somehow vanished from the queue mid-poll."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    job = await queue.get(job_id)
    while job is not None and job.state not in _TERMINAL_JOB_STATES:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(WAIT_POLL_INTERVAL_S, remaining))
        job = await queue.get(job_id)
    return job


async def _get_or_create_conversation_by_external_ref(
    session: AsyncSession, user: User, external_ref: str
) -> Conversation:
    existing = (
        await session.execute(
            select(Conversation).where(
                Conversation.user_id == user.id, Conversation.external_ref == external_ref
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.deleted_at is not None:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="conversation was deleted")
        return existing

    conv = Conversation(
        user_id=user.id,
        external_ref=external_ref,
        title=f"agent:{external_ref}"[:200],
    )
    session.add(conv)
    try:
        # flush (not commit) is enough here: _append_message runs its own commit right
        # after in the same transaction, and needs to see this row via a subsequent
        # SELECT ... FOR UPDATE in that same session, which a flush (not yet committed)
        # already satisfies.
        await session.flush()
    except IntegrityError:
        # Lost a race against a concurrent request for the same (user_id, external_ref)
        # -- fall back to the row that won instead of surfacing a 500.
        await session.rollback()
        existing = (
            await session.execute(
                select(Conversation).where(
                    Conversation.user_id == user.id, Conversation.external_ref == external_ref
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        raise
    return conv


@router.post(
    "/message",
    response_model=SmartMessageOut,
    status_code=status.HTTP_202_ACCEPTED,
    # Same admit_generation_job path as smart_message -- gate it identically. See
    # app/core/rate_limit.py. Per-user rate_limited("message") is intentionally NOT
    # applied here: agentflow uses one shared API key for many end users, so the
    # limit is enforced inside the handler keyed by external_conversation_id instead
    # (2026-08-20).
    dependencies=[
        Depends(check_admission_capacity),
    ],
)
async def agent_message(
    payload: AgentMessageIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    queue: JobQueue = Depends(get_job_queue),
    gemini=Depends(get_gemini_text_client),
) -> SmartMessageOut:
    if gemini is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="chat routing is not configured (APP_GEMINI_API_KEY unset)",
        )

    external_ref = payload.external_conversation_id.strip()
    if not external_ref:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="external_conversation_id must not be blank",
        )

    # Per end-user thread, not the shared service account -- otherwise 100–200 campus
    # users share one rl_message bucket and hit 429 while the GPU queue is still empty.
    settings = get_settings()
    allowed = await check_rate_limit(
        get_redis(request),
        scope="message",
        user_id=f"{user.id}:{external_ref}",
        limit_per_window=settings.rl_message_per_min,
        window_seconds=60,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests, please slow down",
            headers={"Retry-After": str(seconds_until_window_reset(60))},
        )

    conv = await _get_or_create_conversation_by_external_ref(session, user, external_ref)
    user_msg = await _append_message(
        session, conv, "user", {"text": payload.text}, payload.client_message_id
    )
    result = await process_routed_message(
        session,
        conv,
        user,
        queue,
        gemini,
        user_msg,
        model_profile=payload.model_profile,
        model_overrides=payload.model_overrides,
        skip_prompt_design=payload.skip_prompt_design,
        assume_image=payload.assume_image,
        exact_text=payload.exact_text,
        aspect_ratio=payload.aspect_ratio,
        resolution=payload.resolution,
    )

    if payload.wait and result.job is not None:
        # NOTE: this holds the request's checked-out DB connection (from get_db_session)
        # idle for the whole wait -- acceptable at this endpoint's current low call
        # volume (see module docstring), but would exhaust the pool under many
        # concurrent waiters. Not a concern for smart-message/generations, which never
        # take this path.
        timeout_s = min(
            max(payload.wait_timeout_s or WAIT_DEFAULT_TIMEOUT_S, WAIT_MIN_TIMEOUT_S),
            WAIT_MAX_TIMEOUT_S,
        )
        job = await _wait_for_terminal_state(queue, uuid.UUID(result.job.id), timeout_s)
        if job is not None:
            result.job = result.job.model_copy(
                update={
                    "state": job.state,
                    "error_code": job.error_code,
                    "error_detail": job.error_detail,
                }
            )

    return result
