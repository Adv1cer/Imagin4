"""Agentic chat intent-routing endpoints.

POST /conversations/{id}/smart-message is the entry point: it persists the user's
message (reusing conversations.py's `_append_message`), asks the existing conversational
Gemini model to classify intent via structured output (GeminiTextClient.route_intent),
validates that output strictly, and maps it through the pure decision layer
(app/domain/chat/decision_service.py) to exactly one of: a normal chat/clarification
reply, an immediate local ComfyUI generation (GENERAL_IMAGE), or an immediate Gemini
image generation (POSTER / INFOGRAPHIC).

PRODUCT DECISION (2026-08): POSTER/INFOGRAPHIC generation used to require a chat
clarification round-trip when RouteDecision.missing_fields was non-empty, and a separate
POST /pending-actions/{id}/confirm click before the paid Gemini call happened at all.
Both were removed by explicit request from the project owner (after being warned this
trades away the "never spend money without an explicit confirm" invariant the original
architecture spec called for): once the router confidently classifies a message as
POSTER/INFOGRAPHIC, it enqueues the paid generation immediately. The best-effort
grounded-research step (_research_augment below) and the prompt-design step
(GeminiTextClient.design_image_prompt, called from GeminiImageComfyUIClient.submit())
still run to fill in facts and produce a better prompt -- they just no longer gate
execution. The PendingAction DB model, migration, and POST /pending-actions/{id}/confirm
/cancel endpoints are kept in place (nothing currently creates new rows through them) so
a future confirmation requirement can be reinstated without another migration.

This is additive: POST /v1/generations, POST /v1/conversations/{id}/messages, and POST
/v1/conversations/{id}/assistant-reply are all unchanged and still work exactly as
before -- the existing manual "pick a tool, fill in the panel, submit" UI flow remains a
fully supported fallback alongside this one.

The actual classify/research/decide/enqueue pipeline lives in `process_routed_message`
below, factored out so POST /v1/agent/message (app/api/v1/agent_router.py) -- the
API-key-authenticated machine-to-machine entry point added 2026-08 for external systems
(e.g. a university chatbot workflow) that just want to forward raw user text and get back
a chat reply or an enqueued job -- can reuse it without duplicating this logic.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.gemini import GEMINI_OVERLOAD_ERROR_CODES
from app.adapters.queue import JobQueue
from app.api.deps import get_current_user, get_db_session, get_gemini_text_client, get_job_queue
from app.api.v1.conversations import MessageOut, _append_message, _get_owned_conversation
from app.api.v1.generations import GenerationOut
from app.db.models import ChatMessage, PendingAction, User
from app.domain.chat.decision_service import (
    EnqueueGeneralImage,
    EnqueuePaidImage,
    RespondChat,
    RespondClarification,
    decide_next_step,
)
from app.domain.chat.pending_actions import (
    ConfirmOutcome,
    PendingActionSnapshot,
    evaluate_cancellation,
    evaluate_confirmation,
)
from app.domain.chat.routing import (
    Intent,
    ReasonCode,
    RouteDecision,
    RouteDecisionError,
    build_router_system_instruction_with_research,
    parse_route_decision,
)
from app.domain.jobs.admission import (
    IdempotencyConflictError,
    UnknownWorkflowError,
    admit_generation_job,
)

logger = logging.getLogger("imaginv.chat_router")

router = APIRouter(tags=["chat-router"])

# PENDING_ACTION_TTL_MINUTES / compute_params_fingerprint()-based PendingAction creation
# were removed from smart_message() (see module docstring: POSTER/INFOGRAPHIC now
# enqueue immediately, no confirmation gate). POST /pending-actions/{id}/confirm and
# /cancel below are kept functional for any PendingAction rows that already exist, and
# as dormant infrastructure if a confirmation requirement is reinstated later, but
# nothing currently creates new rows.

# Both POSTER and INFOGRAPHIC currently execute through the one real paid-image pipeline
# this repo has (backend=gemini) -- see app/domain/jobs/workflow_registry.py. action_type
# on PendingAction stays a distinct "poster"/"infographic" label for display/audit even
# though they share a workflow; if a genuinely separate infographic pipeline is added
# later, only this mapping needs to change.
_WORKFLOW_FOR_ACTION_TYPE = {"poster": "poster_infographic", "infographic": "poster_infographic"}


class SmartMessageCreate(BaseModel):
    text: str
    client_message_id: str | None = None


class PendingActionOut(BaseModel):
    id: str
    action_type: str
    billing_category: str
    normalized_prompt: str
    exact_text: list[str]
    status: str
    expires_at: datetime

    @staticmethod
    def from_model(pa: PendingAction) -> "PendingActionOut":
        return PendingActionOut(
            id=str(pa.id),
            action_type=pa.action_type,
            billing_category=pa.billing_category,
            normalized_prompt=str(pa.normalized_params.get("prompt", "")),
            exact_text=list(pa.normalized_params.get("exact_text", [])),
            status=pa.status,
            expires_at=pa.expires_at,
        )


class SmartMessageOut(BaseModel):
    """Discriminated by `type`: "chat" carries `assistant_message`, "image_job" carries
    `job`, "confirmation_required" carries `pending_action`. Exactly one of those three
    is populated per response -- kept as one flat model (rather than a Pydantic
    discriminated union) to match this codebase's existing flat-response-model style
    (see GenerationOut/JobOut/MessageOut)."""

    type: str
    user_message: MessageOut
    assistant_message: MessageOut | None = None
    job: GenerationOut | None = None
    pending_action: PendingActionOut | None = None


# GEMINI_OVERLOAD_ERROR_CODES (app/adapters/gemini.py) are the reasons that mean "Gemini
# itself is temporarily overloaded/rate-limited" (route_intent's RuntimeError message is
# built from _sanitized_error) as opposed to "we genuinely couldn't classify this
# message". Distinguished so the customer-facing question doesn't say "I couldn't
# understand you" when the real cause is Google's API being busy -- same "don't let the
# customer blame us/themselves for Gemini's own outage" reasoning as the
# image-generation fix earlier (see gemini.py's _sanitized_error docstring for the
# original 2026-08 incident this pattern comes from). Shared (not redefined here) so
# app/api/v1/conversations.py's create_assistant_reply can apply the exact same
# classification to its own Gemini failures.
_OVERLOAD_REASONS = GEMINI_OVERLOAD_ERROR_CODES
_OVERLOAD_CLARIFICATION_QUESTION = (
    "ขอโทษค่ะ ตอนนี้ระบบ AI มีผู้ใช้งานหนาแน่นชั่วคราว กรุณาลองส่งข้อความเดิมอีกครั้งในอีกสักครู่นะคะ 🙏"
)
_GENERIC_CLARIFICATION_QUESTION = (
    "ขอโทษค่ะ ตอนนี้ระบบแยกแยะคำขอไม่ได้ ช่วยบอกอีกครั้งได้ไหมคะว่าต้องการแชทคุยเฉยๆ, "
    "สร้างภาพทั่วไป, ทำโปสเตอร์ หรือทำอินโฟกราฟิกคะ?"
)


def _fallback_clarification(reason: str) -> RouteDecision:
    """Fail-safe decision used whenever the LLM's output is missing, malformed, or the
    call itself failed. Never guesses GENERAL_IMAGE/POSTER/INFOGRAPHIC -- an
    unclassifiable message always degrades to asking the user to clarify, never to
    silently starting (let alone billing) a generation.

    `reason` is either "invalid_schema" / "llm_call_failed" (generic clarification
    question) or one of _OVERLOAD_REASONS (Gemini-is-busy-specific question) -- see
    process_routed_message's except-block, which is the only caller that can produce the
    latter."""
    question = (
        _OVERLOAD_CLARIFICATION_QUESTION if reason in _OVERLOAD_REASONS else _GENERIC_CLARIFICATION_QUESTION
    )
    return RouteDecision(
        intent=Intent.CLARIFICATION,
        normalized_prompt="",
        exact_text=[],
        missing_fields=[],
        clarification_question=question,
        reason_code=ReasonCode.AMBIGUOUS_VISUAL_DELIVERABLE,
    )


async def _load_history(session: AsyncSession, conversation_id) -> list[dict[str, str]]:
    rows = (
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.sequence_no.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {"role": m.role, "text": str(m.content.get("text", ""))}
        for m in rows
        if m.role in ("user", "assistant")
    ]


async def _research_augment(
    gemini, history: list[dict[str, str]], decision: RouteDecision, conv_id, user_id
) -> RouteDecision:
    """Best-effort: when a POSTER/INFOGRAPHIC classification has missing_fields, try a
    grounded Google Search call to fill them in with REAL facts before falling back to
    asking the user (per project instructions: "if information complete, do it; if not,
    ask; then generate" -- this is the "try to complete it first" step). Two separate
    Gemini calls are required here, not one, because the API rejects combining
    response_schema with the google_search tool -- see routing.py's
    RESEARCH_SYSTEM_INSTRUCTION docstring.

    Never fatal: any failure in either call (unreachable API, timeout, malformed JSON)
    just returns the original decision unchanged so the caller falls back to the normal
    missing_fields -> clarification path exactly as if research had never been
    attempted. Also refuses to accept a re-classification that changed `intent` -- the
    research step may only ever narrow missing_fields for the SAME intent, never be the
    mechanism that decides intent or billing category (that stays solely the job of the
    original classification call)."""
    if decision.intent not in (Intent.POSTER, Intent.INFOGRAPHIC) or not decision.missing_fields:
        return decision
    try:
        findings = await gemini.research_missing_fields(history, decision.missing_fields)
    except Exception as exc:
        logger.info(
            "chat_router: research call failed conv=%s user=%s error=%s (falling back to asking)",
            conv_id,
            user_id,
            type(exc).__name__,
        )
        return decision
    try:
        instruction = build_router_system_instruction_with_research(findings)
        raw = await gemini.route_intent(history, extra_system_instruction=instruction)
        refined = parse_route_decision(raw)
    except Exception as exc:
        logger.info(
            "chat_router: research re-classification failed conv=%s user=%s error=%s",
            conv_id,
            user_id,
            type(exc).__name__,
        )
        return decision
    if refined.intent != decision.intent:
        logger.warning(
            "chat_router: research step changed intent (%s -> %s) for conv=%s -- ignoring, "
            "keeping original classification",
            decision.intent.value,
            refined.intent.value,
            conv_id,
        )
        return decision
    logger.info(
        "chat_router: research reduced missing_fields %s -> %s for conv=%s",
        decision.missing_fields,
        refined.missing_fields,
        conv_id,
    )
    return refined


async def process_routed_message(
    session: AsyncSession,
    conv,
    user: User,
    queue: JobQueue,
    gemini,
    user_msg: ChatMessage,
) -> SmartMessageOut:
    """The actual routing pipeline: classify intent, best-effort research, map through
    the pure decision layer, then respond/enqueue. Shared by both entry points that can
    produce a user ChatMessage needing this treatment:

    - POST /conversations/{id}/smart-message (below) -- the session-authenticated FE
      chat flow, one conversation per browser thread.
    - POST /v1/agent/message (app/api/v1/agent_router.py) -- the API-key-authenticated
      machine-to-machine flow (e.g. a university chatbot workflow forwarding real user
      messages through one shared service account), which resolves/creates its own
      Conversation via `external_ref` before calling this.

    Callers are responsible for auth, resolving/creating `conv`, and persisting
    `user_msg` (via _append_message) before calling this -- this function only takes it
    from there. `gemini` must already be confirmed non-None by the caller."""
    history = await _load_history(session, conv.id)

    validation_outcome = "ok"
    try:
        raw = await gemini.route_intent(history)
        decision = parse_route_decision(raw)
    except RouteDecisionError as exc:
        validation_outcome = "invalid_schema"
        logger.warning(
            "chat_router: invalid routing output conv=%s user=%s error=%s",
            conv.id,
            user.id,
            type(exc).__name__,
        )
        decision = _fallback_clarification("invalid_schema")
    except Exception as exc:
        # route_intent wraps every failure as RuntimeError(_sanitized_error(exc)) (see
        # app.adapters.gemini._sanitized_error) -- str(exc) is therefore always one of a
        # small, controlled set of safe codes, never raw exception/response text, so
        # matching on it here doesn't violate the "no raw exception text" rule.
        sanitized_code = str(exc) if isinstance(exc, RuntimeError) else ""
        validation_outcome = sanitized_code if sanitized_code in _OVERLOAD_REASONS else "llm_call_failed"
        logger.warning(
            "chat_router: routing call failed conv=%s user=%s error_category=%s sanitized=%s",
            conv.id,
            user.id,
            type(exc).__name__,
            sanitized_code or "n/a",
        )
        decision = _fallback_clarification(validation_outcome)

    decision = await _research_augment(gemini, history, decision, conv.id, user.id)

    step = decide_next_step(decision)
    logger.info(
        "chat_router: conv=%s user=%s intent=%s reason_code=%s step=%s validation=%s "
        "normalized_prompt=%r",
        conv.id,
        user.id,
        decision.intent.value,
        decision.reason_code.value,
        type(step).__name__,
        validation_outcome,
        # Truncated -- this is purely a server-side diagnostic breadcrumb (see
        # app/adapters/comfyui/live.py's matching submit()-time prompt log) so a
        # "the wrong subject came out of the image" report can be traced end-to-end
        # (classification -> prompt-design refine -> final ComfyUI/Gemini submission)
        # from docker logs alone, without needing to separately query ComfyUI's own
        # history for the raw prompt text after the fact. Never shown to the customer.
        decision.normalized_prompt[:200] if decision.normalized_prompt else None,
    )

    if isinstance(step, RespondChat):
        try:
            reply_text = await gemini.complete(history)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="chat completion failed"
            )
        assistant_msg = await _append_message(
            session, conv, "assistant", {"text": reply_text}, client_message_id=None
        )
        return SmartMessageOut(
            type="chat",
            user_message=MessageOut.from_model(user_msg),
            assistant_message=MessageOut.from_model(assistant_msg),
        )

    if isinstance(step, RespondClarification):
        assistant_msg = await _append_message(
            session, conv, "assistant", {"text": step.question}, client_message_id=None
        )
        return SmartMessageOut(
            type="chat",
            user_message=MessageOut.from_model(user_msg),
            assistant_message=MessageOut.from_model(assistant_msg),
        )

    if isinstance(step, EnqueueGeneralImage):
        try:
            result = await admit_generation_job(
                queue=queue,
                user_id=user.id,
                workflow_name="image_basic",
                workflow_version="v1",
                inputs={"prompt": step.prompt, "exact_text": step.exact_text},
                # Deterministic per user message: a client retrying the same HTTP call
                # (e.g. after a dropped response) replays the same job instead of
                # enqueueing a second one for what was really one user action.
                idempotency_key=f"router-general-image-{user_msg.id}",
            )
        except (UnknownWorkflowError, IdempotencyConflictError):
            logger.error("chat_router: image_basic admission failed unexpectedly conv=%s", conv.id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to start image generation",
            )
        return SmartMessageOut(
            type="image_job",
            user_message=MessageOut.from_model(user_msg),
            job=GenerationOut(id=result.id, state=result.state, kind=result.kind),
        )

    # EnqueuePaidImage -- POSTER or INFOGRAPHIC. Enqueued immediately, no chat
    # clarification and no confirmation click (see this module's docstring for the
    # product decision behind removing that gate).
    assert isinstance(step, EnqueuePaidImage)
    workflow_name = _WORKFLOW_FOR_ACTION_TYPE[step.action_type]
    try:
        result = await admit_generation_job(
            queue=queue,
            user_id=user.id,
            workflow_name=workflow_name,
            workflow_version="v1",
            inputs={
                "prompt": step.prompt,
                "exact_text": step.exact_text,
                "action_type": step.action_type,
            },
            # Deterministic per user message: a client retrying the same HTTP call
            # (e.g. after a dropped response) replays the same job instead of
            # enqueueing (and billing) a second one for what was really one user action.
            idempotency_key=f"router-paid-image-{user_msg.id}",
        )
    except (UnknownWorkflowError, IdempotencyConflictError):
        logger.error(
            "chat_router: %s admission failed unexpectedly conv=%s", workflow_name, conv.id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to start image generation",
        )
    logger.info(
        "chat_router: paid image enqueued conv=%s user=%s action_type=%s billing=%s job=%s",
        conv.id,
        user.id,
        step.action_type,
        step.billing_category,
        result.id,
    )
    return SmartMessageOut(
        type="image_job",
        user_message=MessageOut.from_model(user_msg),
        job=GenerationOut(id=result.id, state=result.state, kind=result.kind),
    )


@router.post(
    "/conversations/{conversation_id}/smart-message",
    response_model=SmartMessageOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def smart_message(
    conversation_id: str,
    payload: SmartMessageCreate,
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

    conv = await _get_owned_conversation(session, conversation_id, user)
    user_msg = await _append_message(
        session, conv, "user", {"text": payload.text}, payload.client_message_id
    )
    return await process_routed_message(session, conv, user, queue, gemini, user_msg)


async def _get_pending_action_snapshot(
    session: AsyncSession, pending_action_id: str
) -> tuple[PendingAction | None, PendingActionSnapshot | None]:
    try:
        pa_uuid = uuid.UUID(pending_action_id)
    except ValueError:
        return None, None
    row = (
        await session.execute(select(PendingAction).where(PendingAction.id == pa_uuid))
    ).scalar_one_or_none()
    if row is None:
        return None, None
    snapshot = PendingActionSnapshot(
        id=str(row.id),
        user_id=str(row.user_id),
        conversation_id=str(row.conversation_id),
        status=row.status,
        expires_at=row.expires_at,
        params_fingerprint=row.params_fingerprint,
        resulting_job_id=row.resulting_job_id,
    )
    return row, snapshot


_CONFIRM_ERROR_STATUS = {
    ConfirmOutcome.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ConfirmOutcome.WRONG_OWNER: status.HTTP_404_NOT_FOUND,  # don't leak existence to non-owners
    ConfirmOutcome.CANCELLED: status.HTTP_409_CONFLICT,
    ConfirmOutcome.EXPIRED: status.HTTP_410_GONE,
    ConfirmOutcome.ALREADY_CONFIRMED: status.HTTP_409_CONFLICT,
    ConfirmOutcome.PARAMS_CHANGED: status.HTTP_409_CONFLICT,
}
_CONFIRM_ERROR_DETAIL = {
    ConfirmOutcome.NOT_FOUND: "pending action not found",
    ConfirmOutcome.WRONG_OWNER: "pending action not found",
    ConfirmOutcome.CANCELLED: "pending action was cancelled",
    ConfirmOutcome.EXPIRED: "pending action expired -- ask again to create a new one",
    ConfirmOutcome.ALREADY_CONFIRMED: "pending action already confirmed",
    ConfirmOutcome.PARAMS_CHANGED: "pending action parameters changed -- ask again",
}


@router.post("/pending-actions/{pending_action_id}/confirm", response_model=SmartMessageOut)
async def confirm_pending_action(
    pending_action_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    queue: JobQueue = Depends(get_job_queue),
) -> SmartMessageOut:
    now = datetime.now(timezone.utc)
    row, snapshot = await _get_pending_action_snapshot(session, pending_action_id)
    outcome = evaluate_confirmation(snapshot, str(user.id), now)

    # Idempotent replay: already confirmed AND a job was actually admitted for it ->
    # return that same job again instead of erroring, so a double-click or a client
    # retrying after a dropped response can't be mistaken for a real conflict.
    if outcome == ConfirmOutcome.ALREADY_CONFIRMED and row is not None and row.resulting_job_id:
        logger.info("chat_router: confirm replay pending_action=%s (idempotent)", row.id)
        return SmartMessageOut(
            type="image_job",
            user_message=MessageOut(  # confirm has no "user message" of its own; echo n/a
                id=str(row.id),
                role="system",
                sequence_no=0,
                content={},
                status="complete",
                created_at=row.created_at,
            ),
            job=GenerationOut(
                id=row.resulting_job_id,
                state="queued",
                kind=_WORKFLOW_FOR_ACTION_TYPE[row.action_type],
            ),
            pending_action=PendingActionOut.from_model(row),
        )

    if outcome != ConfirmOutcome.OK:
        logger.info(
            "chat_router: confirm rejected pending_action=%s outcome=%s",
            pending_action_id,
            outcome.value,
        )
        raise HTTPException(
            status_code=_CONFIRM_ERROR_STATUS[outcome], detail=_CONFIRM_ERROR_DETAIL[outcome]
        )

    assert row is not None
    # Atomic, race-free transition: only succeeds if the row is STILL pending and
    # unexpired at the moment of the UPDATE -- a concurrent duplicate confirm (double
    # click, retried request) can only ever win this once.
    result = await session.execute(
        update(PendingAction)
        .where(
            PendingAction.id == row.id,
            PendingAction.status == "pending",
            PendingAction.expires_at > now,
        )
        .values(status="confirmed", consumed_at=now)
    )
    await session.commit()

    if result.rowcount != 1:
        # Lost the race to a concurrent confirm (or it expired in the interim). Re-check
        # for an idempotent-replay opportunity rather than surfacing a spurious error to
        # what may just be a UI double-submit.
        await session.refresh(row)
        if row.status == "confirmed" and row.resulting_job_id:
            return SmartMessageOut(
                type="image_job",
                user_message=MessageOut(
                    id=str(row.id),
                    role="system",
                    sequence_no=0,
                    content={},
                    status="complete",
                    created_at=row.created_at,
                ),
                job=GenerationOut(
                    id=row.resulting_job_id,
                    state="queued",
                    kind=_WORKFLOW_FOR_ACTION_TYPE[row.action_type],
                ),
                pending_action=PendingActionOut.from_model(row),
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="pending action already being processed"
        )

    workflow_name = _WORKFLOW_FOR_ACTION_TYPE[row.action_type]
    try:
        admission = await admit_generation_job(
            queue=queue,
            user_id=user.id,
            workflow_name=workflow_name,
            workflow_version="v1",
            inputs={
                "prompt": row.normalized_params.get("prompt", ""),
                "exact_text": row.normalized_params.get("exact_text", []),
                # Distinct from the job's own "kind" (== workflow_name, always
                # "poster_infographic" for both action types since they share one
                # workflow -- see _WORKFLOW_FOR_ACTION_TYPE). Lets
                # GeminiImageComfyUIClient.submit()'s prompt-design step phrase its
                # instruction as "a poster"/"an infographic" instead of the generic
                # workflow name.
                "action_type": row.action_type,
            },
            # Idempotency key derived from the pending action's OWN id (not a
            # client-supplied header) -- this is what makes the endpoint idempotent
            # end-to-end even across a crash between "marked confirmed" and "job
            # enqueued": a retry lands on admit_generation_job's own replay path and
            # returns the same job rather than creating a second paid generation.
            idempotency_key=f"pending-action-{row.id}",
        )
    except (UnknownWorkflowError, IdempotencyConflictError):
        logger.error("chat_router: paid admission failed unexpectedly pending_action=%s", row.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to start generation",
        )

    row.resulting_job_id = admission.id
    await session.commit()
    logger.info(
        "chat_router: confirm executed pending_action=%s job=%s billing=%s",
        row.id,
        admission.id,
        row.billing_category,
    )
    return SmartMessageOut(
        type="image_job",
        user_message=MessageOut(
            id=str(row.id),
            role="system",
            sequence_no=0,
            content={},
            status="complete",
            created_at=row.created_at,
        ),
        job=GenerationOut(id=admission.id, state=admission.state, kind=admission.kind),
        pending_action=PendingActionOut.from_model(row),
    )


@router.post("/pending-actions/{pending_action_id}/cancel", response_model=PendingActionOut)
async def cancel_pending_action(
    pending_action_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> PendingActionOut:
    row, snapshot = await _get_pending_action_snapshot(session, pending_action_id)
    outcome = evaluate_cancellation(snapshot, str(user.id))

    if outcome == ConfirmOutcome.NOT_FOUND or outcome == ConfirmOutcome.WRONG_OWNER:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="pending action not found"
        )
    if outcome == ConfirmOutcome.ALREADY_CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="pending action already confirmed, cannot cancel",
        )

    assert row is not None
    if row.status == "pending":
        await session.execute(
            update(PendingAction)
            .where(PendingAction.id == row.id, PendingAction.status == "pending")
            .values(status="cancelled")
        )
        await session.commit()
        await session.refresh(row)
    # Cancelling an already-cancelled action is a no-op success (idempotent cancel).
    logger.info("chat_router: cancel pending_action=%s status=%s", row.id, row.status)
    return PendingActionOut.from_model(row)
