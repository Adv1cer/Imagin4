"""Argon2id password hashing (never store or log raw passwords)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(raw: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, raw)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)
