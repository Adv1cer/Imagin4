"""FastAPI application entrypoint.

`create_app()` builds an app with live adapters wired from Settings (used by
`uvicorn app.main:app`). Tests instead build a bare FastAPI app and override
`app.state.*` / dependency overrides with the in-memory fakes in
`app.adapters.{queue,storage,comfyui}` -- see backend/tests/e2e/test_smoke.py.
"""

from __future__ import annotations

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

logger = logging.getLogger("imaginv")


def _build_state(app: FastAPI) -> None:
    """Wires app.state with live-mode adapters. Swapped in tests via app.state overrides."""
    settings = get_settings()
    engine = get_engine()
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    if settings.comfy_mode == "mock":
        app.state.comfy_client = MockComfyUIClient()
    else:
        # Live ComfyUI HTTP adapter would be constructed here from settings.comfy_base_url.
        app.state.comfy_client = MockComfyUIClient()

    # NOTE: production job queue/storage should be swapped for the Postgres-SKIP-LOCKED
    # and S3/MinIO-backed adapters respectively; in-memory fakes keep `create_app()`
    # runnable without external infra for local dev smoke-testing.
    app.state.job_queue = InMemoryJobQueue()
    app.state.storage = InMemoryObjectStorage()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=get_settings().log_level)
    _build_state(app)
    logger.info("imaginv backend starting up")
    yield
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
