"""FastAPI dependency-injection wiring.

All state is attached to `app.state` in the lifespan handler (backend/app/main.py) so
that tests can override individual dependencies (get_job_queue, get_storage,
get_comfy_client, get_db) with fakes without touching the router code.
"""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.comfyui import ComfyUIClient
from app.adapters.queue import JobQueue
from app.adapters.storage import ObjectStorage
from app.core.config import Settings, get_settings
from app.db.models import AuthSession, User
from app.domain.auth.sessions import hash_token, is_session_valid


def get_app_settings() -> Settings:
    return get_settings()


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


def get_job_queue(request: Request) -> JobQueue:
    return request.app.state.job_queue


def get_storage(request: Request) -> ObjectStorage:
    return request.app.state.storage


def get_comfy_client(request: Request) -> ComfyUIClient:
    return request.app.state.comfy_client


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
) -> User:
    """Resolves the caller from an opaque bearer/cookie session token.

    Accepts either the `X-Session-Token` header (used in tests/API clients) or the
    configured session cookie, matching the pattern documented in docs/architecture.md.
    """
    settings = get_settings()
    raw_token = x_session_token or request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")

    token_hash = hash_token(raw_token)
    result = await session.execute(
        select(AuthSession).where(AuthSession.token_hash == token_hash)
    )
    auth_session = result.scalar_one_or_none()
    if auth_session is None or not is_session_valid(
        auth_session.expires_at, auth_session.revoked_at
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session invalid")

    user_result = await session.execute(select(User).where(User.id == auth_session.user_id))
    user = user_result.scalar_one_or_none()
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not active")
    return user
