"""Idempotency-key comparison for POST /v1/generations.

Two requests with the same (user_id, idempotency_key, kind) must be treated as the same
logical request. If the payload matches (by canonical hash) the caller gets back the
existing job (still 202/200, no new job created). If the payload differs, this is a
client bug/conflict and must be rejected deterministically (409-style) rather than
silently creating a second job or silently ignoring the difference.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum


def canonical_payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyOutcome(str, Enum):
    NEW = "new"
    REPLAY = "replay"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class IdempotencyCheck:
    outcome: IdempotencyOutcome
    existing_job_id: str | None = None


def check_idempotency(
    existing_job_id: str | None, existing_payload_hash: str | None, new_payload: dict
) -> IdempotencyCheck:
    if existing_job_id is None:
        return IdempotencyCheck(outcome=IdempotencyOutcome.NEW)
    new_hash = canonical_payload_hash(new_payload)
    if new_hash == existing_payload_hash:
        return IdempotencyCheck(outcome=IdempotencyOutcome.REPLAY, existing_job_id=existing_job_id)
    return IdempotencyCheck(outcome=IdempotencyOutcome.CONFLICT, existing_job_id=existing_job_id)
