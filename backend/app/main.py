"""FastAPI application entrypoint.

`create_app()` builds an app with live adapters wired from Settings (used by
`uvicorn app.main:app`). Tests instead build a bare FastAPI app and override
`app.state.*` / dependency overrides with the in-memory fakes in
`app.adapters.{queue,storage,comfyui}` -- see backend/tests/e2e/test_smoke.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.adapters.comfyui.factory import build_comfy_client
from app.adapters.queue.factory import build_job_queue
from app.adapters.storage import InMemoryObjectStorage
from app.adapters.storage.s3 import S3ObjectStorage
from app.api.v1 import (
    agent_router,
    auth,
    chat_router,
    conversations,
    generations,
    health,
    jobs,
    metrics,
)
from app.core.config import get_settings
from app.db.base import get_engine
from app.services.reconciler import Reconciler
from app.services.scheduler import Scheduler

logger = logging.getLogger("imaginv")


def _build_state(app: FastAPI) -> None:
    """Wires app.state with live-mode adapters. Swapped in tests via app.state overrides."""
    settings = get_settings()
    engine = get_engine()
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # settings.queue_backend selects InMemoryJobQueue (dev/test, no Postgres needed) vs
    # PostgresJobQueue (durable, shared across every API/scheduler/reconciler process --
    # see app/adapters/queue/factory.py and app/adapters/queue/postgres.py).
    app.state.job_queue = build_job_queue(settings, session_factory=app.state.session_factory)
    # settings.storage_backend is the SAME kind of process-locality gap, for generated
    # image bytes instead of job state -- see Settings.storage_backend's docstring and
    # app/adapters/storage/s3.py's module docstring. Both this and queue_backend must be
    # switched together for a split API/scheduler/reconciler deployment to actually work.
    app.state.storage = (
        S3ObjectStorage(settings) if settings.storage_backend == "s3" else InMemoryObjectStorage()
    )

    # app.state.gemini_text_client is used directly by chat_router.py (chat replies +
    # semantic routing), separate from the (also Gemini-backed, when configured) prompt
    # design step build_comfy_client wires up internally for the image clients below --
    # two thin client instances, not two different code paths; see
    # app/adapters/comfyui/factory.py's module docstring for why the split is correct.
    if settings.gemini_api_key:
        from app.adapters.gemini import GeminiTextClient

        app.state.gemini_text_client = GeminiTextClient(
            api_key=settings.gemini_api_key,
            model=settings.gemini_text_model,
            timeout_s=settings.gemini_request_timeout_s,
            research_timeout_s=settings.gemini_research_timeout_s,
        )
        logger.info(
            "gemini: wired for chat replies + routing (model=%s)", settings.gemini_text_model
        )
    else:
        app.state.gemini_text_client = None
        logger.info(
            "gemini: APP_GEMINI_API_KEY not set -- chat replies will fail clearly "
            "(POST .../assistant-reply 503) instead of silently degrading"
        )

    # ComfyUI (mock/live) + Gemini/OpenRouter poster-infographic wiring -- see
    # app/adapters/comfyui/factory.py (shared with app/services/scheduler.py's and
    # app/services/reconciler.py's standalone entrypoints, so every process that can
    # dispatch a job builds the SAME real client instead of each guessing separately).
    app.state.comfy_client = build_comfy_client(settings, app.state.storage)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=get_settings().log_level)
    _build_state(app)
    logger.info("imaginv backend starting up")

    # settings.queue_backend == "memory": the standalone `scheduler`/`reconciler`
    # docker-compose services each construct their own InMemoryJobQueue (see
    # app/services/scheduler.py:main / app/services/reconciler.py:main), which is
    # process-local -- so they never actually see jobs enqueued by this API process.
    # Run both loops in-process here too in that mode, sharing app.state.job_queue/
    # comfy_client, so a submitted generation is still dispatched/finalized end to end
    # without needing external infra for quick local smoke-testing.
    #
    # settings.queue_backend == "postgres": job state is durable and shared via
    # PostgresJobQueue (see app/adapters/queue/factory.py), so the standalone
    # `scheduler`/`reconciler` containers now genuinely work as independent,
    # horizontally-replicable processes -- do NOT also start them in-process here, or
    # every API replica would run its own extra scheduler/reconciler loop on top of the
    # dedicated ones (harmless correctness-wise, since claims are SKIP LOCKED and every
    # transition is a conditional UPDATE, but wasteful and confusing).
    settings = get_settings()
    scheduler_task = reconciler_task = None
    scheduler = reconciler = None
    if settings.queue_backend == "memory":
        scheduler = Scheduler(job_queue=app.state.job_queue, comfy_client=app.state.comfy_client)
        reconciler = Reconciler(job_queue=app.state.job_queue, comfy_client=app.state.comfy_client)
        scheduler_task = asyncio.create_task(scheduler.run_forever())
        reconciler_task = asyncio.create_task(reconciler.run_forever())
    else:
        logger.info(
            "queue_backend=postgres: in-process scheduler/reconciler NOT started here -- "
            "run the standalone `scheduler`/`reconciler` docker-compose services"
        )

    yield

    if scheduler is not None:
        scheduler.request_shutdown()
        reconciler.request_shutdown()
        await asyncio.gather(scheduler_task, reconciler_task, return_exceptions=True)

    engine = getattr(app.state, "session_factory", None)
    if engine is not None:
        await get_engine().dispose()
    logger.info("imaginv backend shut down")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Imaginv4 Backend", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/v1")
    app.include_router(auth.router, prefix="/v1")
    app.include_router(conversations.router, prefix="/v1")
    app.include_router(generations.router, prefix="/v1")
    app.include_router(jobs.router, prefix="/v1")
    app.include_router(chat_router.router, prefix="/v1")
    app.include_router(agent_router.router, prefix="/v1")
    app.include_router(metrics.router)  # unprefixed: GET /metrics

    return app


app = create_app()
