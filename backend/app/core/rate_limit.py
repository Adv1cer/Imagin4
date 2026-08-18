"""Redis-backed admission control: a global concurrency gate plus per-user fixed-window
rate limiting.

Both fail OPEN if Redis is unreachable -- Redis is a soft admission optimization here,
not the source of truth. (Postgres-side per-user/global job caps -- Settings.
max_active_jobs_per_user / max_queued_jobs_per_user / global_queue_cap -- are meant to be
the authoritative backstop once implemented; as of 2026-08-18 those settings exist but are
NOT yet enforced anywhere in app/domain/jobs/admission.py -- see that module's TODO. This
file does not fix that gap, it only fixes the gap directly responsible for the 100-VU
burst-test incident below.)

Incident context (2026-08-18): a k6 burst of 100 concurrent POST /v1/generations hit a
single API process whose SQLAlchemy engine had only db_pool_size + db_max_overflow = 10
connections. Nothing rejected the excess requests before they reached the DB layer, so
all 100 piled onto get_current_user's `SELECT ... FROM api_keys` at once; PgBouncer/
Postgres, also under-provisioned for that burst, closed connections out from under
in-flight queries (asyncpg.exceptions.ConnectionDoesNotExistError), and every request hung
until k6's own 60s client timeout fired. Nothing here changes pool sizing (see
app/db/base.py / Settings.db_pool_size) -- it stops a burst from exceeding whatever the
pool can serve in the first place, and does so BEFORE any DB connection is touched.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis.asyncio as redis

logger = logging.getLogger("imaginv.rate_limit")


def rate_limit_key(scope: str, user_id: str, window_seconds: int = 60) -> str:
    window = int(time.time() // window_seconds)
    return f"rl:{scope}:{user_id}:{window}"


async def check_rate_limit(
    redis_client: "redis.Redis | None",
    scope: str,
    user_id: str,
    limit_per_window: int,
    window_seconds: int = 60,
) -> bool:
    """Fixed-window counter. Returns True when the caller is within limit (allowed).

    `limit_per_window <= 0` disables the check entirely (always allowed) -- lets a
    deployment opt out of a specific scope via Settings without touching call sites.
    """
    if redis_client is None or limit_per_window <= 0:
        return True
    key = rate_limit_key(scope, user_id, window_seconds)
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, window_seconds)
    except Exception:
        logger.warning("rate_limit: redis unavailable, failing open (scope=%s)", scope)
        return True
    return count <= limit_per_window


class AdmissionGate:
    """Global in-flight concurrency cap shared (via Redis) across every API replica.

    This is deliberately a FLEET-WIDE number, not a per-process one: it exists to protect
    the shared PgBouncer/Postgres capacity from a burst larger than the fleet can serve,
    complementing (not duplicating) each process's own db_pool_size + db_max_overflow.
    Size `max_inflight` to the shared backend capacity (e.g. PgBouncer's
    DEFAULT_POOL_SIZE), not to any single replica's local pool.

    The TTL is refreshed on every successful acquire/release rather than set once, so a
    live counter's expiry keeps sliding forward under real traffic, and a counter that
    leaked (e.g. a request crashed between acquire and the `finally: release()` in
    app/api/deps.py:check_admission_capacity) self-heals within `ttl_seconds` of the last
    activity instead of staying wedged forever.
    """

    _KEY = "admission:inflight"

    def __init__(
        self,
        redis_client: "redis.Redis | None",
        max_inflight: int,
        ttl_seconds: int = 60,
    ) -> None:
        self._redis = redis_client
        self._max_inflight = max_inflight
        self._ttl = ttl_seconds

    async def try_acquire(self) -> bool:
        if self._redis is None or self._max_inflight <= 0:
            return True
        try:
            count = await self._redis.incr(self._KEY)
            await self._redis.expire(self._KEY, self._ttl)
        except Exception:
            logger.warning("admission_gate: redis unavailable, failing open")
            return True
        if count > self._max_inflight:
            await self.release()
            return False
        return True

    async def release(self) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.decr(self._KEY)
        except Exception:
            pass
