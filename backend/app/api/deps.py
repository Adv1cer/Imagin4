"""FastAPI dependency-injection wiring.

All state is attached to `app.state` in the lifespan handler (backend/app/main.py) so
that tests can override individual dependencies (get_job_queue, get_storage,
get_comfy_client, get_db) with fakes without touching the router code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator, Callable

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.comfyui import ComfyUIClient
from app.adapters.queue import JobQueue
from app.adapters.storage import ObjectStorage
from app.core.config import Settings, get_settings
from app.core.rate_limit import AdmissionGate, check_rate_limit, seconds_until_window_reset
from app.db.models import ApiKey, AuthSession, User
from app.domain.auth.api_keys import KEY_PREFIX, hash_api_key, is_api_key_active
from app.domain.auth.sessions import hash_token, is_session_valid

if TYPE_CHECKING:
    import redis.asyncio as redis


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


def get_gemini_text_client(request: Request):
    """None when GEMINI_API_KEY isn't configured (see app/main.py:_build_state) --
    callers must handle that case explicitly rather than assuming it's always wired."""
    return getattr(request.app.state, "gemini_text_client", None)


def get_redis(request: Request) -> "redis.Redis | None":
    return getattr(request.app.state, "redis", None)


async def check_admission_capacity(request: Request) -> AsyncIterator[None]:
    """Global concurrency gate -- must run BEFORE any DB-touching dependency, including
    get_current_user. Deliberately does not depend on the authenticated user: rejecting
    excess load has to happen before a DB connection is spent resolving who's asking,
    which is exactly the code path (SELECT ... FROM api_keys) that exhausted the pool
    during the 2026-08-18 100-VU burst test. See app/core/rate_limit.py:AdmissionGate.

    Add `dependencies=[Depends(check_admission_capacity)]` to any route that ends up
    calling admit_generation_job (app/domain/jobs/admission.py) -- currently POST
    /v1/generations, /conversations/{id}/smart-message, /pending-actions/{id}/confirm,
    and /agent/message.

    `app.state.admission_gate` may not exist at all -- e.g. tests/e2e/test_smoke.py
    builds a bare FastAPI() app with only the fakes it needs, not the full
    app.main._build_state wiring -- in which case this is a no-op, same as an
    AdmissionGate built with no Redis client.
    """
    gate: AdmissionGate | None = getattr(request.app.state, "admission_gate", None)
    if gate is None:
        yield
        return
    if not await gate.try_acquire():
        # Retry-After (2026-08-20, see Settings.admission_gate_retry_after_s' docstring
        # for why this is a small fixed guess rather than derived from the gate's TTL) --
        # previously this 503 gave a client no signal at all for how long to back off,
        # same gap CapacityExceededError closed for the per-user/global queue caps (see
        # app/domain/jobs/admission.py).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server is at capacity, please retry shortly",
            headers={"Retry-After": str(get_settings().admission_gate_retry_after_s)},
        )
    try:
        yield
    finally:
        await gate.release()


def rate_limited(scope: str, limit_attr: str) -> Callable:
    """Dependency factory for the per-user fixed-window limits in Settings (rl_* fields,
    e.g. rl_generation_per_min) -- those settings existed but were never enforced
    anywhere before 2026-08-18; this is the enforcement. Runs AFTER get_current_user
    (needs an identity), unlike check_admission_capacity above which runs before it.
    """

    async def _dependency(
        request: Request,
        user: User = Depends(get_current_user),
    ) -> None:
        settings = get_settings()
        limit = getattr(settings, limit_attr)
        allowed = await check_rate_limit(
            get_redis(request),
            scope=scope,
            user_id=str(user.id),
            limit_per_window=limit,
            window_seconds=60,
        )
        if not allowed:
            # Retry-After (2026-08-20, see app/core/rate_limit.py:seconds_until_window_
            # reset's docstring) -- this is a FIXED-WINDOW limiter, so "how long until
            # you can retry" has an exact answer (time left in the current window), not
            # a guess like AdmissionGate's 503 above.
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests, please slow down",
                headers={"Retry-After": str(seconds_until_window_reset(60))},
            )

    return _dependency


async def _resolve_api_key_user(session: AsyncSession, raw_key: str) -> User | None:
    """`Authorization: Bearer imgn_...` path -- machine-to-machine callers (see
    app/api/v1/agent_router.py, app/domain/auth/api_keys.py). Deliberately a completely
    separate lookup from the session-token path below: an API key is bound to a service
    account and revoked independently of any human's sessions. Returns None (never
    raises) on any lookup failure so the caller can fall through to session-token auth
    instead of hard-failing just because a non-API-key bearer value was sent."""
    key_hash = hash_api_key(raw_key)
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()
    if api_key is None or not is_api_key_active(api_key.revoked_at):
        return None
    user_result = await session.execute(select(User).where(User.id == api_key.user_id))
    user = user_result.scalar_one_or_none()
    if user is None or user.status != "active":
        return None
    return user


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
    authorization: str | None = Header(default=None),
) -> User:
    """Resolves the caller from either a machine-to-machine API key or a human
    session token/cookie.

    `Authorization: Bearer imgn_...` is checked first and is reserved exclusively for API
    keys (see app/domain/auth/api_keys.py) -- it is never treated as a session token, so
    there's no ambiguity between the two credential types. Anything else falls through to
    the original session-token path: the `X-Session-Token` header (used in tests/API
    clients) or the configured session cookie, matching the pattern documented in
    docs/architecture.md.
    """
    if authorization is not None and authorization.startswith("Bearer "):
        raw = authorization[len("Bearer ") :].strip()
        if raw.startswith(KEY_PREFIX):
            user = await _resolve_api_key_user(session, raw)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
                )
            return user

    settings = get_settings()
    raw_token = x_session_token or request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")

    token_hash = hash_token(raw_token)
    result = await session.execute(select(AuthSession).where(AuthSession.token_hash == token_hash))
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
