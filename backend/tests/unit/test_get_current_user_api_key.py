"""Tests for the Authorization: Bearer imgn_... branch added to
app/api/deps.py:get_current_user, using a fake AsyncSession (no real DB) so this
security-critical dispatch logic is covered without needing Postgres. The pre-existing
X-Session-Token/cookie branch is left exactly as it was and isn't re-tested here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from app.api.deps import get_current_user
from app.db.models import ApiKey, User
from app.domain.auth.api_keys import issue_api_key


@dataclass
class _FakeResult:
    value: object

    def scalar_one_or_none(self):
        return self.value


class _FakeSession:
    """Returns canned results in call order -- mirrors the two sequential
    `session.execute(select(...))` calls `_resolve_api_key_user` makes (ApiKey lookup,
    then User lookup)."""

    def __init__(self, results: list[object]):
        self._results = list(results)

    async def execute(self, _stmt):
        return _FakeResult(self._results.pop(0))


def _make_user(status: str = "active") -> User:
    return User(
        id=uuid.uuid4(),
        email="svc@example.internal",
        display_name="svc",
        status=status,
        plan_code="standard",
    )


async def test_valid_api_key_resolves_to_its_bound_active_user():
    user = _make_user()
    issued = issue_api_key()
    api_key = ApiKey(id=uuid.uuid4(), user_id=user.id, key_hash=issued.key_hash, label="test")
    session = _FakeSession([api_key, user])

    resolved = await get_current_user(
        request=None,
        session=session,
        x_session_token=None,
        authorization=f"Bearer {issued.raw_key}",
    )
    assert resolved.id == user.id


async def test_unknown_api_key_is_rejected():
    session = _FakeSession([None])
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(
            request=None,
            session=session,
            x_session_token=None,
            authorization="Bearer imgn_does-not-exist",
        )
    assert exc_info.value.status_code == 401


async def test_revoked_api_key_is_rejected():
    user = _make_user()
    issued = issue_api_key()
    api_key = ApiKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_hash=issued.key_hash,
        label="test",
        revoked_at="2026-08-11T00:00:00+00:00",
    )
    session = _FakeSession([api_key])
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(
            request=None,
            session=session,
            x_session_token=None,
            authorization=f"Bearer {issued.raw_key}",
        )
    assert exc_info.value.status_code == 401


async def test_api_key_bound_to_inactive_user_is_rejected():
    user = _make_user(status="suspended")
    issued = issue_api_key()
    api_key = ApiKey(id=uuid.uuid4(), user_id=user.id, key_hash=issued.key_hash, label="test")
    session = _FakeSession([api_key, user])
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(
            request=None,
            session=session,
            x_session_token=None,
            authorization=f"Bearer {issued.raw_key}",
        )
    assert exc_info.value.status_code == 401


async def test_non_imgn_bearer_value_falls_through_to_session_auth_and_fails_without_one():
    """A bearer value that isn't imgn_-prefixed must never be treated as an API key --
    it falls through to the session-token path, which then correctly 401s since no
    session token/cookie was supplied either."""
    session = _FakeSession([])  # never called: falls through before any DB lookup
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(
            request=_FakeRequestNoCookies(),
            session=session,
            x_session_token=None,
            authorization="Bearer some-other-token",
        )
    assert exc_info.value.status_code == 401


class _FakeRequestNoCookies:
    def __init__(self) -> None:
        self.cookies: dict = {}
