"""Builds the composite ComfyUIClient (mock/live ComfyUI + Gemini/OpenRouter for
poster/infographic) from Settings + an ObjectStorage instance. Extracted from
app/main.py::_build_state (2026-08-17) so app/services/scheduler.py's and
app/services/reconciler.py's standalone `main()` entrypoints can build the SAME real
client the API process uses, instead of the MockComfyUIClient they were hardcoded to.

Why this mattered: once `settings.queue_backend == "postgres"` (see
app/adapters/queue/factory.py), the standalone `scheduler`/`reconciler`
docker-compose services become the processes that actually dispatch jobs -- if they
still built MockComfyUIClient regardless of `settings.comfy_mode`/`APP_GEMINI_API_KEY`,
every job would "succeed" against a fake immediately, silently, with no error, while a
real GPU/API call never happened. Confirmed as a live risk (not just theoretical) via
Chet's own deployment, 2026-08-17: he switched `APP_QUEUE_BACKEND=postgres` in `.env`
before this factory existed.

`gemini_text_client` (used for chat replies / semantic routing / "design a better
prompt first") is deliberately NOT part of this function's return value -- that's an
API-request-serving concern (app/api/v1/chat_router.py etc.), not something the
scheduler/reconciler dispatch loop needs directly. It's still built INTERNALLY here
where needed as the `comfy_prompt_designer`/`prompt_designer` callback passed into the
image clients, exactly matching what app/main.py did inline before this extraction.
"""

from __future__ import annotations

import logging

from app.adapters.comfyui import ComfyUIClient, MockComfyUIClient
from app.adapters.comfyui.live import LiveComfyUIClient
from app.adapters.comfyui.multi_worker import MultiWorkerComfyUIClient
from app.adapters.routing_comfyui import CompositeComfyUIClient
from app.adapters.storage import ObjectStorage
from app.core.config import Settings
from app.domain.jobs.comfy_overrides import build_allowlists
from app.domain.jobs.comfy_profiles import build_profiles

logger = logging.getLogger("imaginv")


def build_comfy_client(settings: Settings, storage: ObjectStorage) -> ComfyUIClient:
    # Ordinary "Image" generation (image_basic) always routes through ComfyUI (mock or
    # live) regardless of image_provider -- see CompositeComfyUIClient's docstring.
    if settings.comfy_mode == "mock":
        comfyui_client: ComfyUIClient = MockComfyUIClient(storage=storage)
    else:
        worker_urls = settings.comfy_worker_base_urls or [settings.comfy_base_url]
        # See app/domain/jobs/comfy_profiles.py -- "student" always exists (mirrors the
        # comfy_* fields below exactly), "personnel" only if configured. Passed into every
        # worker client so a submission's model_profile picks its checkpoint/steps/cfg
        # per request instead of only ever using the fields below.
        profiles = build_profiles(settings)
        # See app/domain/jobs/comfy_overrides.py -- empty *_CSV settings (the default)
        # means every field starts non-overridable; list actual worker filenames in
        # .env to open specific fields up to per-request model_overrides.
        override_allowlists = build_allowlists(settings)

        def _build_live_client(base_url: str) -> LiveComfyUIClient:
            return LiveComfyUIClient(
                base_url=base_url,
                storage=storage,
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
                profiles=profiles,
                override_allowlists=override_allowlists,
            )

        live_clients = [_build_live_client(url) for url in worker_urls]
        comfyui_client = (
            live_clients[0] if len(live_clients) == 1 else MultiWorkerComfyUIClient(live_clients)
        )
        logger.info(
            "comfyui: live mode, workers=%s family=%s diffusion_model=%s model_profiles=%s",
            worker_urls,
            settings.comfy_model_family,
            (
                settings.comfy_diffusion_model_name
                if settings.comfy_model_family == "qwen_image"
                else settings.comfy_checkpoint_name
            ),
            sorted(profiles.keys()),
        )

    # Text-brain client used ONLY for prompt design (comfy_prompt_designer below +
    # poster/infographic's prompt_designer) -- separate instance from
    # app.state.gemini_text_client (chat replies/routing, wired in app/main.py), same
    # split app/main.py's own comment documents. Originally Gemini-only; extended
    # 2026-08 to fall back to self-hosted Qwen (settings.brain_backend == "qwen")
    # since removing APP_GEMINI_API_KEY previously left this step silently disabled --
    # confirmed live as the root cause of "เจนแมวได้คน" (asked for a cat, ComfyUI drew a
    # person): with no prompt designer wired, the raw/untranslated Thai request text
    # (e.g. "เจนรูป แมว", including the instruction word, not just the subject) was sent
    # straight into CLIPTextEncode. SDXL-family CLIP encoders are trained overwhelmingly
    # on English captions, so non-English text is close to noise to them -- the
    # checkpoint then falls back to its own strongest prior instead of following the
    # prompt, which for a portrait-oriented checkpoint (e.g. the "personnel" profile,
    # named/tuned for staff-photo-style output) is a human face regardless of what was
    # actually asked for. Wiring a real prompt-design step (Gemini or Qwen, whichever is
    # configured) fixes this by translating/cleaning the prompt into English before it
    # ever reaches ComfyUI, the same way it always did when Gemini was configured.
    prompt_design_client = None
    if settings.brain_backend == "qwen":
        from app.adapters.qwen import QwenTextClient

        prompt_design_client = QwenTextClient(
            base_url=settings.qwen_base_url,
            model=settings.qwen_text_model,
            timeout_s=settings.qwen_request_timeout_s,
            research_timeout_s=settings.qwen_research_timeout_s,
        )
        logger.info(
            "qwen: wired for comfy/poster prompt design (base_url=%s, model=%s)",
            settings.qwen_base_url,
            settings.qwen_text_model,
        )
    elif settings.gemini_api_key:
        from app.adapters.gemini import GeminiTextClient

        prompt_design_client = GeminiTextClient(
            api_key=settings.gemini_api_key,
            model=settings.gemini_text_model,
            timeout_s=settings.gemini_request_timeout_s,
            research_timeout_s=settings.gemini_research_timeout_s,
        )
        logger.info(
            "gemini: wired for prompt design (model=%s)", settings.gemini_text_model
        )
    else:
        logger.info(
            "gemini: APP_GEMINI_API_KEY not set and brain_backend != 'qwen' -- comfy/"
            "poster prompt design step skipped"
        )

    # Kept as a local alias -- everything below this line already refers to
    # `gemini_text_client`, and the object may now actually be a QwenTextClient; the
    # variable name is legacy (same reasoning as app/main.py's app.state.gemini_text_client).
    gemini_text_client = prompt_design_client

    prompt_designer = gemini_text_client.design_image_prompt if gemini_text_client else None

    if settings.image_provider == "openrouter":
        if settings.openrouter_api_key:
            from app.adapters.openrouter import OpenRouterImageComfyUIClient

            gemini_image_client = OpenRouterImageComfyUIClient(
                api_key=settings.openrouter_api_key,
                model=settings.openrouter_image_model,
                storage=storage,
                timeout_s=settings.openrouter_image_request_timeout_s,
                base_url=settings.openrouter_base_url,
                prompt_designer=prompt_designer,
            )
            logger.info(
                "image_provider=openrouter: wired for poster/infographic generation (model=%s)",
                settings.openrouter_image_model,
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
            storage=storage,
            timeout_s=settings.gemini_image_request_timeout_s,
            prompt_designer=prompt_designer,
        )
        logger.info(
            "image_provider=gemini: wired for poster/infographic generation (model=%s)",
            settings.gemini_image_model,
        )
    else:
        gemini_image_client = None
        logger.info(
            "image_provider=gemini but APP_GEMINI_API_KEY not set -- poster/infographic "
            "generation will fail clearly (job failed, error_detail=gemini_not_configured) "
            "instead of silently degrading"
        )

    return CompositeComfyUIClient(
        comfyui_client=comfyui_client,
        gemini_client=gemini_image_client,
        image_provider_name=settings.image_provider,
        comfy_prompt_designer=(
            gemini_text_client.design_comfyui_prompt if gemini_text_client else None
        ),
    )
