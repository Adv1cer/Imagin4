"""Conversation CRUD + message listing with cursor pagination."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.db.models import ChatMessage, Conversation, User
from app.domain.conversations.pagination import Cursor, InvalidCursorError
from app.domain.jobs.ownership import NotOwnerError, assert_owner

router = APIRouter(prefix="/conversations", tags=["conversations"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class ConversationCreate(BaseModel):
    title: str | None = None


class ConversationOut(BaseModel):
    id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def from_model(c: Conversation) -> "ConversationOut":
        return ConversationOut(
            id=str(c.id),
            title=c.title,
            status=c.status,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )


class MessageOut(BaseModel):
    id: str
    role: str
    sequence_no: int
    content: dict
    status: str
    created_at: datetime

    @staticmethod
    def from_model(m: ChatMessage) -> "MessageOut":
        return MessageOut(
            id=str(m.id),
            role=m.role,
            sequence_no=m.sequence_no,
            content=m.content,
            status=m.status,
            created_at=m.created_at,
        )


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None


class MessageCreate(BaseModel):
    role: str = "user"
    content: dict
    client_message_id: str | None = None


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> ConversationOut:
    conv = Conversation(user_id=user.id, title=payload.title or "New conversation")
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return ConversationOut.from_model(conv)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> list[ConversationOut]:
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
        .order_by(Conversation.updated_at.desc())
    )
    return [ConversationOut.from_model(c) for c in result.scalars().all()]


async def _get_owned_conversation(
    session: AsyncSession, conversation_id: str, user: User
) -> Conversation:
    try:
        conv_uuid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    result = await session.execute(select(Conversation).where(Conversation.id == conv_uuid))
    conv = result.scalar_one_or_none()
    if conv is None or conv.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    try:
        assert_owner(str(conv.user_id), str(user.id))
    except NotOwnerError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return conv


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> ConversationOut:
    conv = await _get_owned_conversation(session, conversation_id, user)
    return ConversationOut.from_model(conv)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> None:
    conv = await _get_owned_conversation(session, conversation_id, user)
    conv.deleted_at = datetime.now(timezone.utc)
    conv.status = "deleted"
    await session.commit()


@router.get("/{conversation_id}/messages", response_model=MessagePage)
async def list_messages(
    conversation_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, le=MAX_PAGE_SIZE, gt=0),
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> MessagePage:
    conv = await _get_owned_conversation(session, conversation_id, user)

    stmt = select(ChatMessage).where(ChatMessage.conversation_id == conv.id)
    if cursor:
        try:
            decoded = Cursor.decode(cursor)
        except InvalidCursorError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cursor")
        stmt = stmt.where(ChatMessage.sequence_no > int(decoded.sort_key))
    stmt = stmt.order_by(ChatMessage.sequence_no.asc()).limit(limit + 1)

    result = await session.execute(stmt)
    rows = result.scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = Cursor(sort_key=last.sequence_no, id=str(last.id)).encode()

    return MessagePage(items=[MessageOut.from_model(m) for m in rows], next_cursor=next_cursor)


_ALLOWED_ROLES = {"user", "assistant", "system", "tool"}


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_message(
    conversation_id: str,
    payload: MessageCreate,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> MessageOut:
    if payload.role not in _ALLOWED_ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid role")

    conv = await _get_owned_conversation(session, conversation_id, user)

    # Replay of a client-generated id: return the existing row instead of erroring, so a
    # retried POST (flaky network, double-click) is idempotent rather than duplicating a
    # message or surfacing the unique-constraint violation to the client.
    if payload.client_message_id:
        existing = (
            await session.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv.id,
                    ChatMessage.client_message_id == payload.client_message_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return MessageOut.from_model(existing)

    # Lock the conversation row so concurrent inserts for the same conversation
    # serialize on sequence_no allocation instead of racing on MAX(sequence_no)+1.
    await session.execute(
        select(Conversation.id).where(Conversation.id == conv.id).with_for_update()
    )
    next_seq = (
        await session.execute(
            select(func.coalesce(func.max(ChatMessage.sequence_no), 0) + 1).where(
                ChatMessage.conversation_id == conv.id
            )
        )
    ).scalar_one()

    message = ChatMessage(
        conversation_id=conv.id,
        role=payload.role,
        sequence_no=next_seq,
        client_message_id=payload.client_message_id,
        content=payload.content,
        status="complete",
    )
    session.add(message)
    conv.updated_at = datetime.now(timezone.utc)
    try:
        await session.commit()
    except IntegrityError:
        # Lost a race on client_message_id despite the row lock (e.g. lock not held on
        # SQLite in unit tests, or a concurrent request slipped in) -- fall back to
        # returning the row that won rather than surfacing a 500.
        await session.rollback()
        if payload.client_message_id:
            existing = (
                await session.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == conv.id,
                        ChatMessage.client_message_id == payload.client_message_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return MessageOut.from_model(existing)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="message conflict")

    await session.refresh(message)
    return MessageOut.from_model(message)
