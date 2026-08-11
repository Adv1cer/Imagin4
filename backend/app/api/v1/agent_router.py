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
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.queue import JobQueue
from app.api.deps import get_current_user, get_db_session, get_gemini_text_client, get_job_queue
from app.api.v1.chat_router import SmartMessageOut, process_routed_message
from app.api.v1.conversations import _append_message
from app.db.models import Conversation, User

router = APIRouter(prefix="/agent", tags=["agent"])


class AgentMessageIn(BaseModel):
    # Whatever the calling system already uses to identify "this end user/thread" --
    # opaque to us, just used as a stable lookup key. Required and non-blank (validated
    # below) because skipping it would mean every caller using this key shares one
    # conversation, which defeats the whole point of this endpoint.
    external_conversation_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)
    client_message_id: str | None = None


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


@router.post("/message", response_model=SmartMessageOut, status_code=status.HTTP_202_ACCEPTED)
async def agent_message(
    payload: AgentMessageIn,
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

    conv = await _get_or_create_conversation_by_external_ref(session, user, external_ref)
    user_msg = await _append_message(
        session, conv, "user", {"text": payload.text}, payload.client_message_id
    )
    return await process_routed_message(session, conv, user, queue, gemini, user_msg)
