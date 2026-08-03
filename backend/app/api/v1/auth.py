"""Auth endpoints: login, refresh (sliding session), logout, logout-all, me."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.core.config import get_settings
from app.db.models import AuthSession, User
from app.domain.auth.passwords import verify_password
from app.domain.auth.sessions import hash_ip, issue_session

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    session_token: str
    expires_at: datetime


class MeResponse(BaseModel):
    id: str
    email: str
    display_name: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> LoginResponse:
    settings = get_settings()
    result = await session.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user is None or user.password_hash is None or not verify_password(
        payload.password, user.password_hash
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account not active")

    issued = issue_session(settings.session_ttl_hours)
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=issued.token_hash,
        expires_at=issued.expires_at,
        ip_hash=hash_ip(_client_ip(request)),
        user_agent=request.headers.get("user-agent"),
    )
    session.add(auth_session)
    await session.commit()

    response.set_cookie(
        settings.session_cookie_name,
        issued.raw_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.session_ttl_hours * 3600,
    )
    return LoginResponse(session_token=issued.raw_token, expires_at=issued.expires_at)


@router.post("/refresh", response_model=LoginResponse)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> LoginResponse:
    """Sliding-window refresh: revokes the current session and issues a new token."""
    settings = get_settings()
    from app.domain.auth.sessions import hash_token

    raw_token = request.headers.get("X-Session-Token") or request.cookies.get(
        settings.session_cookie_name
    )
    if raw_token:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.token_hash == hash_token(raw_token))
            .values(revoked_at=datetime.now(timezone.utc))
        )

    issued = issue_session(settings.session_ttl_hours)
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=issued.token_hash,
        expires_at=issued.expires_at,
        ip_hash=hash_ip(_client_ip(request)),
        user_agent=request.headers.get("user-agent"),
    )
    session.add(auth_session)
    await session.commit()

    response.set_cookie(
        settings.session_cookie_name,
        issued.raw_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.session_ttl_hours * 3600,
    )
    return LoginResponse(session_token=issued.raw_token, expires_at=issued.expires_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    _: User = Depends(get_current_user),
) -> None:
    from app.domain.auth.sessions import hash_token

    settings = get_settings()
    raw_token = request.headers.get("X-Session-Token") or request.cookies.get(
        settings.session_cookie_name
    )
    if raw_token:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.token_hash == hash_token(raw_token))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await session.commit()
    response.delete_cookie(settings.session_cookie_name)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
) -> None:
    settings = get_settings()
    await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await session.commit()
    response.delete_cookie(settings.session_cookie_name)


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(id=str(user.id), email=user.email, display_name=user.display_name)
