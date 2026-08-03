"""Opaque session token generation and hash-only storage.

The raw token is only ever held in memory long enough to (a) set the HttpOnly cookie in
the HTTP response and (b) hash it before persisting. It must never be logged or stored
in plaintext anywhere, including auth_sessions.token_hash which stores only the hash.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TOKEN_BYTES = 32


@dataclass(frozen=True)
class IssuedSession:
    raw_token: str
    token_hash: str
    expires_at: datetime


def generate_raw_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_session(ttl_hours: int, now: datetime | None = None) -> IssuedSession:
    now = now or datetime.now(timezone.utc)
    raw = generate_raw_token()
    return IssuedSession(
        raw_token=raw, token_hash=hash_token(raw), expires_at=now + timedelta(hours=ttl_hours)
    )


def is_session_valid(
    expires_at: datetime, revoked_at: datetime | None, now: datetime | None = None
) -> bool:
    now = now or datetime.now(timezone.utc)
    if revoked_at is not None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now < expires_at


def hash_ip(ip: str, pepper: str = "") -> str:
    """Privacy-safe IP storage: never store raw client IPs."""
    return hashlib.sha256((pepper + ip).encode("utf-8")).hexdigest()
