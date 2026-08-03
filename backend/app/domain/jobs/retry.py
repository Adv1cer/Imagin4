"""Retry classification and exponential backoff with jitter."""

from __future__ import annotations

import random
from dataclasses import dataclass

RETRYABLE_ERROR_CODES = frozenset(
    {
        "comfy_timeout",
        "comfy_disconnect",
        "comfy_transient",
        "worker_lease_expired",
        "worker_unreachable",
    }
)
NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "comfy_rejected",
        "workflow_invalid",
        "quota_exceeded",
        "malformed_response",
        "comfy_permanent",
    }
)


def is_retryable(error_code: str, attempt_no: int, max_attempts: int) -> bool:
    if attempt_no >= max_attempts:
        return False
    if error_code in NON_RETRYABLE_ERROR_CODES:
        return False
    return error_code in RETRYABLE_ERROR_CODES


@dataclass(frozen=True)
class BackoffConfig:
    base_seconds: float = 2.0
    factor: float = 2.0
    max_seconds: float = 120.0
    jitter_ratio: float = 0.2  # +/- 20%


def compute_backoff_seconds(
    attempt_no: int, cfg: BackoffConfig = BackoffConfig(), rng: random.Random | None = None
) -> float:
    """attempt_no is 1-indexed (the attempt that just failed)."""
    rng = rng or random
    raw = min(cfg.base_seconds * (cfg.factor ** max(attempt_no - 1, 0)), cfg.max_seconds)
    jitter_span = raw * cfg.jitter_ratio
    return max(0.0, raw + rng.uniform(-jitter_span, jitter_span))
