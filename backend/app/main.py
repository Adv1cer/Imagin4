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
from app.adapters.comfyui.live import LiveComfyUIClient
from app.adapters.comfyui.multi_worker import MultiWorkerComfyUIClient
from app.adapters.queue import InMemoryJobQueue
from app.adapters.routing_comfyui import CompositeComfyUIClient
from app.adapters.storage import InMemoryObjectStorage
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

    # NOTE: production job queue/storage should be swapped for the Postgres-SKIP-LOCKED
    # and S3/MinIO-backed adapters respectively; in-memory fakes keep `create_app()`
    # runnable without external infra for local dev smoke-testing.
    app.state.job_queue = InMemoryJobQueue()
    app.state.storage = InMemoryObjectStorage()

    # ComfyUI adapter always exists (mock or, eventually, live) -- ordinary "Image"
    # generation always routes here regardless of whether Gemini is configured.
    if settings.comfy_mode == "mock":
        # storage=app.state.storage so a "succeeded" mock job actually has a real (if
        # trivial) image behind its object_key -- see MockComfyUIClient's docstring.
        comfyui_client = MockComfyUIClient(storage=app.state.storage)
    else:
        # comfy_worker_base_urls_csv (when set) wins over the single comfy_base_url --
        # one LiveComfyUIClient per worker URL, all sharing the same model/checkpoint
        # config, wrapped so the scheduler/reconciler still see one ComfyUIClient. See
        # app/adapters/comfyui/multi_worker.py's docstring for why a single client can't
        # just round-robin HTTP requests to different base_urls: prompt_id ownership must
        # be tracked per-worker.
        worker_urls = settings.comfy_worker_base_urls or [settings.comfy_base_url]

        def _build_live_client(base_url: str) -> LiveComfyUIClient:
            return LiveComfyUIClient(
                base_url=base_url,
                storage=app.state.storage,
                checkpoint_name=settings.comfy_checkpoint_name,
                model_family=settings.comfy_model_family,
                diffusion_model_name=settings.comfy_diffusion_model_name,
                clip_name=settings.comfy_clip_name,
                vae_name=settings.comfy_vae_name,
                model_sampling_shift=settings.comfy_model_sampling_shift,
                sampler_name=settings.comfy_sampler_name,
                scheduler=settings.comfy_scheduler,
                steps=settings.comfy_steps,
                cfg_scale=settings.comfy_cfg_scale,
                negative_prompt=settings.comfy_negative_prompt,
                request_timeout_s=settings.comfy_request_timeout_s,
            )

        live_clients = [_build_live_client(url) for url in worker_urls]
        comfyui_client = (
            live_clients[0] if len(live_clients) == 1 else MultiWorkerComfyUIClient(live_clients)
        )
        logger.info(
            "comfyui: live mode, workers=%s family=%s diffusion_model=%s",
            worker_urls,
            settings.comfy_model_family,
            (
                settings.comfy_diffusion_model_name
                if settings.comfy_model_family == "qwen_image"
                else settings.comfy_checkpoint_name
            ),
        )

    # GeminiTextClient (chat replies + semantic router + both providers' "design a
    # better prompt first" step) is wired whenever a Gemini key exists, REGARDLESS of
    # settings.image_provider -- OpenRouter's Image API is image-only, it has no
    # chat/completions role here, so text/routing/research always stays on Gemini. Only
    # which client actually generates poster/infographic PIXELS switches below.
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

    prompt_designer = (
        app.state.gemini_text_client.design_image_prompt
        if app.state.gemini_text_client is not None
        else None
    )

    # Poster/infographic PIXEL-generation backend -- switches on settings.image_provider
    # (see its comment in app/core/config.py). Historically this variable only ever held
    # a Gemini client (hence CompositeComfyUIClient's constructor param still being named
    # `gemini_client` below); it now holds whichever provider is actually selected.
    if settings.image_provider == "openrouter":
        if settings.openrouter_api_key:
            from app.adapters.openrouter import OpenRouterImageComfyUIClient

            gemini_image_client = OpenRouterImageComfyUIClient(
                api_key=settings.openrouter_api_key,
                model=settings.openrouter_image_model,
                storage=app.state.storage,
                timeout_s=settings.openrouter_image_request_timeout_s,
                base_url=settings.openrouter_base_url,
                prompt_designer=prompt_designer,
            )
            logger.info(
                "image_provider=openrouter: wired for poster/infographic generation "
                "(model=%s); ordinary image generation still uses %s",
                settings.openrouter_image_model,
                "mock ComfyUI" if settings.comfy_mode == "mock" else "ComfyUI",
            )
        else:
            gemini_image_client = None
            logger.info(
                "image_provider=openrouter but APP_OPENROUTER_API_KEY not set -- "
                "poster/infographic generation will fail clearly (job failed, "
                "error_detail=openrouter_not_configured) instead of silently degrading"
            )
    elif settings.gemini_api_key:
        from app.adapters.gemini import GeminiImageComfyUIClient

        gemini_image_client = GeminiImageComfyUIClient(
            api_key=settings.gemini_api_key,
            model=settings.gemini_image_model,
            storage=app.state.storage,
            timeout_s=settings.gemini_image_request_timeout_s,
            prompt_designer=prompt_designer,
        )
        logger.info(
            "image_provider=gemini: wired for poster/infographic generation (model=%s); "
            "ordinary image generation still uses %s",
            settings.gemini_image_model,
            "mock ComfyUI" if settings.comfy_mode == "mock" else "ComfyUI",
        )
    else:
        gemini_image_client = None
        logger.info(
            "image_provider=gemini but APP_GEMINI_API_KEY not set -- poster/infographic "
            "generation will fail clearly (job failed, error_detail=gemini_not_configured) "
            "instead of silently degrading"
        )

    app.state.comfy_client = CompositeComfyUIClient(
        comfyui_client=comfyui_client,
        gemini_client=gemini_image_client,
        image_provider_name=settings.image_provider,
        # Same best-effort "design a good prompt first" step as the poster/infographic
        # path, applied to ordinary ComfyUI (GENERAL_IMAGE) generation -- None when
        # Gemini isn't configured, in which case CompositeComfyUIClient just uses the
        # router's prompt unmodified. Deliberately still keyed off gemini_text_client
        # (not image_provider) since this is a text-only design step, unrelated to which
        # provider renders the final poster pixels.
        comfy_prompt_designer=(
            app.state.gemini_text_client.design_comfyui_prompt
            if app.state.gemini_text_client is not None
            else None
        ),
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
    app.include_router(chat_router.router, prefix="/v1")
    app.include_router(agent_router.router, prefix="/v1")
    app.include_router(metrics.router)  # unprefixed: GET /metrics

    return app


app = create_app()
