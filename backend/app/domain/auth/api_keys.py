"""Opaque API-key generation and hash-only storage for machine-to-machine callers.

Mirrors app/domain/auth/sessions.py's session-token scheme exactly (same sha256-hex
hashing, same "raw value only ever exists in memory long enough to hand it to the
caller once and hash it before persisting" discipline) -- the two are kept as separate
modules/tables (api_keys vs auth_sessions) rather than unified because they have
different lifecycles: a session is short-lived and tied to one human's browser login; an
API key is long-lived by design and tied to a service account, revoked independently.

The `imgn_` prefix lets app/api/deps.py:get_current_user (and anyone reading a log or a
leaked-credential scan) tell an API key apart from any other bearer token at a glance,
without needing a DB round-trip first.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

KEY_PREFIX = "imgn_"
KEY_BYTES = 32


@dataclass(frozen=True)
class IssuedApiKey:
    raw_key: str
    key_hash: str


def generate_raw_api_key() -> str:
    return f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_BYTES)}"


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def issue_api_key() -> IssuedApiKey:
    raw = generate_raw_api_key()
    return IssuedApiKey(raw_key=raw, key_hash=hash_api_key(raw))


def is_api_key_active(revoked_at) -> bool:
    return revoked_at is None
