"""Single place that decides which JobQueue backend is active, so app/main.py,
app/services/scheduler.py, and app/services/reconciler.py can never disagree with each
other about it (which was exactly the bug this replaces -- see app/main.py's old
`lifespan` comment: the standalone scheduler/reconciler containers each built their OWN
InMemoryJobQueue, invisible to the API process and to each other).

`settings.queue_backend` ("memory" | "postgres", see app/core/config.py) is the only
switch. "postgres" requires a `session_factory` (an `async_sessionmaker` bound to
`settings.database_url`, e.g. `app.state.session_factory` or a freshly-built one for a
standalone process -- see `build_session_factory` below).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.adapters.queue import InMemoryJobQueue, JobQueue
from app.adapters.queue.postgres import PostgresJobQueue
from app.core.config import Settings


def build_job_queue(settings: Settings, session_factory: async_sessionmaker | None = None) -> JobQueue:
    if settings.queue_backend == "memory":
        return InMemoryJobQueue()
    if settings.queue_backend == "postgres":
        if session_factory is None:
            # Standalone-process case (scheduler.py/reconciler.py's `main()`) -- unlike
            # the FastAPI app, they have no `app.state.session_factory` already built by
            # `_build_state`, so fall back to the same module-level singleton
            # `get_db`/other DB-touching code already shares (app/db/base.py) rather
            # than constructing a second, independent engine/pool per process.
            from app.db.base import get_session_factory

            session_factory = get_session_factory()
        return PostgresJobQueue(session_factory=session_factory)
    raise ValueError(f"unknown queue_backend={settings.queue_backend!r}")  # unreachable: Literal-typed
