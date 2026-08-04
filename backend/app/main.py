"""FastAPI application entrypoint.

`create_app()` builds an app with live adapters wired from Settings (used by
`uvicorn app.main:app`). Tests instead build a bare FastAPI app and override
`app.state.*` / dependency overrides with the in-memory fakes in
`app.adapters.{queue,storage,comfyui}` -- see backend/tests/e2e/test_smoke.py.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.adapters.comfyui import MockComfyUIClient
from app.adapters.queue import InMemoryJobQueue
from app.adapters.storage import InMemoryObjectStorage
from app.api.v1 import auth, conversations, generations, health, jobs, metrics
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

    # NOTE: production job queue/storage should be swapped for the Postgres-SKIP-LOCKED
    # and S3/MinIO-backed adapters respectively; in-memory fakes keep `create_app()`
    # runnable without external infra for local dev smoke-testing.
    app.state.job_queue = InMemoryJobQueue()
    app.state.storage = InMemoryObjectStorage()

    if settings.gemini_api_key:
        # Gemini (Google AI Studio) takes over as both the image-generation backend
        # (implements the same ComfyUIClient port as MockComfyUIClient / a real
        # ComfyUI adapter would, so the scheduler/reconciler need no changes) and as
        # the text chat completion backend. Takes priority over `comfy_mode` when set.
        from app.adapters.gemini import GeminiImageComfyUIClient, GeminiTextClient

        app.state.comfy_client = GeminiImageComfyUIClient(
            api_key=settings.gemini_api_key,
            model=settings.gemini_image_model,
            storage=app.state.storage,
            timeout_s=settings.gemini_request_timeout_s,
        )
        app.state.gemini_text_client = GeminiTextClient(
            api_key=settings.gemini_api_key,
            model=settings.gemini_text_model,
            timeout_s=settings.gemini_request_timeout_s,
        )
        logger.info(
            "gemini: wired as image backend (model=%s) and chat backend (model=%s)",
            settings.gemini_image_model,
            settings.gemini_text_model,
        )
    else:
        if settings.comfy_mode == "mock":
            app.state.comfy_client = MockComfyUIClient()
        else:
            # Live ComfyUI HTTP adapter would be constructed here from
            # settings.comfy_base_url.
            app.state.comfy_client = MockComfyUIClient()
        app.state.gemini_text_client = None
        logger.info(
            "gemini: APP_GEMINI_API_KEY not set -- image generation uses %s, "
            "chat replies are unavailable (POST .../assistant-reply returns 503)",
            "mock ComfyUI" if settings.comfy_mode == "mock" else "ComfyUI",
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=get_settings().log_level)
    _build_state(app)
    logger.info("imaginv backend starting up")

    # The standalone `scheduler`/`reconciler` docker-compose services each construct
    # their own InMemoryJobQueue (see app/services/scheduler.py:main /
    # app/services/reconciler.py:main), which is process-local -- so in dev/in-memory
    # mode they never actually see jobs enqueued by this API process. Until the
    # Postgres-backed JobQueue lands (see README "Known limitations"), run both loops
    # in-process here too, sharing app.state.job_queue/comfy_client, so a submitted
    # generation is actually dispatched and finalized end to end without requiring the
    # separate containers to coincidentally share state (they don't).
    scheduler = Scheduler(job_queue=app.state.job_queue, comfy_client=app.state.comfy_client)
    reconciler = Reconciler(job_queue=app.state.job_queue, comfy_client=app.state.comfy_client)
    scheduler_task = asyncio.create_task(scheduler.run_forever())
    reconciler_task = asyncio.create_task(reconciler.run_forever())

    yield

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
    app.include_router(metrics.router)  # unprefixed: GET /metrics

    return app


app = create_app()
