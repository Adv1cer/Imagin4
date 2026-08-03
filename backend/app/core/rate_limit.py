"""Redis-backed rate limiting: fixed-window key derivation.

Identity is always the authenticated user_id (never a client-supplied header), so keys
cannot be spoofed. If Redis is unavailable, callers must fail *open* for read paths and
fail *closed conservatively* is NOT required here because Postgres-side admission control
(max_active/max_queued per user, global_queue_cap) remains authoritative regardless of
whether Redis rate limiting is up.
"""

from __future__ import annotations

import time


def rate_limit_key(scope: str, user_id: str, window_seconds: int = 60) -> str:
    window = int(time.time() // window_seconds)
    return f"rl:{scope}:{user_id}:{window}"
