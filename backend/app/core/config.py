"""Application settings, loaded and validated from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    env: Literal["dev", "test", "staging", "prod"] = "dev"
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql+asyncpg://imaginv:imaginv@localhost:5432/imaginv"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout_s: int = 5
    db_pool_pre_ping: bool = True

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_connect_timeout_s: float = 0.5

    # Sessions / auth
    session_cookie_name: str = "imaginv_session"
    session_ttl_hours: int = 12
    session_idle_ttl_hours: int = 2
    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_cookie_name: str = "imaginv_csrf"

    # CORS (comma-separated in the env var; pydantic-settings would otherwise try to
    # JSON-decode a `list[str]` field before any validator runs, which breaks plain
    # comma-separated values like "http://a,http://b" -- so we store it as `str` here
    # and expose the parsed list via a computed property instead.)
    cors_allow_origins_csv: str = "http://localhost:3000"

    @property
    def cors_allow_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins_csv.split(",") if o.strip()]

    # Admission / fairness defaults
    max_active_jobs_per_user: int = 1
    max_queued_jobs_per_user: int = 3
    global_queue_cap: int = 5000
    # How many image_basic jobs the scheduler may claim+dispatch concurrently. Only
    # meaningful up to how many independent ComfyUI execution engines actually exist to
    # run them -- see comfy_worker_base_urls_csv above. With a single ComfyUI process
    # (comfy_worker_base_urls_csv empty), keep this at 1 until benchmarked otherwise: a
    # single GPU can't run two full generations in parallel just because two jobs were
    # dispatched to the same process. With N workers configured, this can go up to N (one
    # in-flight job per worker) -- but per the same single-GPU caveat, if those N workers
    # share one physical GPU, more concurrent jobs does not mean proportionally faster
    # completion, only more overlap of otherwise-idle time (I/O, model loading, etc.).
    # Benchmark actual wall-clock throughput at each step rather than assuming.
    default_comfy_active_slots: int = 1
    # SEPARATE from the ComfyUI slot count above on purpose: ComfyUI dispatch is
    # GPU-bound (one local device, one job at a time until benchmarked otherwise -- see
    # default_comfy_active_slots' own caveat about single-GPU boxes), but Gemini image
    # generation (poster_infographic, see app/domain/jobs/workflow_registry.py) is just
    # an outbound HTTPS call to Google -- it doesn't touch this machine's GPU at all.
    # Before this existed, both shared one scheduler capacity number (see
    # Scheduler._reserve_capacity_by_backend in app/services/scheduler.py), so a slow
    # poster/infographic job would needlessly block an unrelated general-image job (and
    # vice versa) even though neither competes for the other's resource. Higher than 1
    # by default since Gemini concurrency is limited by API rate limits/cost, not local
    # hardware -- tune down if you hit 429s, up if your quota allows more.
    default_gemini_active_slots: int = 3
    priority_tiers: int = 4
    aging_increment_per_minute: float = 0.5

    # Object storage
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "imaginv-assets"
    s3_region: str = "us-east-1"
    signed_url_ttl_s: int = 300

    # ComfyUI
    comfy_mode: Literal["mock", "live"] = "mock"
    comfy_base_url: str = "http://localhost:8188"
    # Comma-separated base URLs for MULTIPLE independent ComfyUI instances (e.g. the
    # comfyui-worker-1/comfyui-worker-2 docker-compose services), so the scheduler can
    # actually dispatch concurrently-claimed jobs (see default_comfy_active_slots) to
    # more than one execution engine instead of serializing everything behind one
    # process. Empty (default) means "use comfy_base_url as a single worker" --
    # unchanged, backward-compatible behavior. When set, this WINS over comfy_base_url
    # entirely (see app/main.py); all listed workers share every other comfy_* setting
    # below (same model/checkpoint config assumed identical across workers).
    #
    # IMPORTANT (single-GPU caveat, see README/project instructions): more worker
    # processes does not multiply GPU compute. If they all share one physical GPU,
    # expect at best partial overlap, not linear speedup -- start with 2 and benchmark
    # actual wall-clock throughput before scaling to more.
    comfy_worker_base_urls_csv: str = ""
    comfy_request_timeout_s: float = 10.0
    # Only used when comfy_mode == "live" (see app/adapters/comfyui/live.py). Filenames
    # below MUST match exactly what's installed on the target ComfyUI instance -- there
    # is no discovery/validation of this from our side, a wrong name fails the job with a
    # ComfyUI-side graph error.
    #
    # "checkpoint" = a single-file model (CheckpointLoaderSimple) bundling UNet+CLIP+VAE
    #   together, e.g. classic SDXL. Only comfy_checkpoint_name is used.
    # "qwen_image" = Qwen-Image's split-file architecture (separate diffusion model, text
    #   encoder, and VAE, loaded via UNETLoader/CLIPLoader/VAELoader, plus a required
    #   ModelSamplingAuraFlow node) -- graph verified against the official workflow at
    #   https://docs.comfy.org/tutorials/image/qwen/qwen-image (2026-08). Uses
    #   comfy_diffusion_model_name/comfy_clip_name/comfy_vae_name/comfy_model_sampling_shift.
    comfy_model_family: Literal["checkpoint", "qwen_image"] = "qwen_image"
    comfy_checkpoint_name: str = "sd_xl_base_1.0.safetensors"
    comfy_diffusion_model_name: str = "qwen_image_2512_fp8_e4m3fn.safetensors"
    comfy_clip_name: str = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
    comfy_vae_name: str = "qwen_image_vae.safetensors"
    # ModelSamplingAuraFlow's "shift" widget -- Qwen-Image's official template ships 3.1;
    # changing it shifts the noise schedule and is a quality/style tuning knob, not
    # something to guess differently without reason.
    comfy_model_sampling_shift: float = 3.1
    comfy_sampler_name: str = "euler"
    # "simple" is Qwen-Image's official default scheduler; "normal" is the classic
    # SDXL-era default -- kept as a single setting since only one family is active at a
    # time, but worth remembering if you switch comfy_model_family later.
    comfy_scheduler: str = "simple"
    comfy_steps: int = 20
    # Qwen-Image's official template uses cfg=4.0 (vs the classic SDXL default of ~7.0).
    comfy_cfg_scale: float = 4.0
    comfy_negative_prompt: str = ""

    @property
    def comfy_worker_base_urls(self) -> list[str]:
        return [u.strip() for u in self.comfy_worker_base_urls_csv.split(",") if u.strip()]

    # Gemini (Google AI Studio) -- when gemini_api_key is set, it replaces both the
    # image-generation backend (in place of ComfyUI) and powers real text chat
    # replies. Model names/quotas change over time -- as of 2026-08, gemini-2.0-flash
    # and gemini-2.5-flash were both retired for new API keys/projects (confirmed via
    # live 404s from the API, not guessed), and the current generation is 3.x
    # (gemini-3.6-flash for text, gemini-3.1-flash-image aka "Nano Banana 2" for
    # image). If generation starts failing with a 404 "no longer available" error
    # again, check https://ai.google.dev/gemini-api/docs/models for the current model
    # list and update APP_GEMINI_TEXT_MODEL / APP_GEMINI_IMAGE_MODEL in your .env --
    # this is expected to keep happening periodically as Google rotates models, it is
    # not something to "fix" in code. Get a key at https://aistudio.google.com/.
    gemini_api_key: str | None = None
    gemini_text_model: str = "gemini-3.6-flash"
    gemini_image_model: str = "gemini-3.1-flash-image"
    # Text replies are short and fast; 30s is comfortable. Image generation -- especially
    # a long, detail-heavy poster/infographic prompt asking for multiple rendered text
    # elements and a specific layout -- routinely takes noticeably longer than a plain
    # "draw a cat" prompt, and was observed hitting the shared 30s timeout in production
    # use (asyncio.TimeoutError, confirmed via live logs, not guessed) even on the
    # cheaper gemini-3.1-flash-image model, not just the pro tier. Split the two so a
    # slow poster generation doesn't need to piggyback on the tight chat-latency budget.
    gemini_request_timeout_s: float = 30.0
    gemini_image_request_timeout_s: float = 90.0
    # Grounded Google Search research call (app/domain/chat/routing.py's
    # RESEARCH_SYSTEM_INSTRUCTION / GeminiTextClient.research_missing_fields) -- a
    # SEPARATE, shorter budget from gemini_request_timeout_s. This call is best-effort
    # (a POSTER/INFOGRAPHIC with missing_fields always falls back to asking the user if
    # it fails or is slow -- see app/api/v1/chat_router.py:_research_augment), so it's
    # capped tighter than a normal chat/classification call rather than sharing the
    # full 30s, keeping the worst-case smart-message latency (classify + research +
    # re-classify, three sequential calls) bounded to roughly 30+20+30=80s instead of 90s.
    gemini_research_timeout_s: float = 20.0

    # Image generation provider for "Poster / Infographic" jobs specifically (see
    # app/domain/jobs/workflow_registry.py's `backend` field / CompositeComfyUIClient in
    # app/adapters/routing_comfyui.py). "gemini" (default) calls Google's Gemini image
    # models directly via gemini_* above. "openrouter" instead routes through
    # OpenRouter's unified Image API (one API key, many underlying models -- Gemini,
    # Seedream, FLUX, etc. -- see openrouter_* below), useful if you want a different
    # underlying model or a separate billing account without touching code. Chat replies
    # and the semantic router (GeminiTextClient) are UNCHANGED by this setting -- they
    # always use Gemini, since OpenRouter's image endpoint is image-only. Ordinary
    # "Image" generation (workflow image_basic) is also unaffected -- it always uses
    # ComfyUI (comfy_mode above) regardless of this setting. Whichever provider is NOT
    # selected here does not need its API key configured; app/main.py logs clearly which
    # provider is actually wired at startup.
    image_provider: Literal["gemini", "openrouter"] = "gemini"

    # OpenRouter (https://openrouter.ai/) -- alternative to Gemini for "Poster /
    # Infographic" generation, selected via image_provider="openrouter" above. Uses
    # OpenRouter's dedicated Image API (POST {openrouter_base_url}/images, NOT the
    # chat/completions endpoint) -- a single synchronous call returning base64-encoded
    # image data (response["data"][0]["b64_json"]), same "submit does the whole thing,
    # get_status just looks up the cached result" shape as GeminiImageComfyUIClient (see
    # app/adapters/openrouter.py). Billing is all-or-nothing per OpenRouter's docs: a
    # failed generation errors out and is not billed, so there's no separate "partial
    # charge" case to handle. Get a key at https://openrouter.ai/keys.
    #
    # Model slugs are OpenRouter's own catalog names, not Google's/ByteDance's/BFL's raw
    # model names -- confirmed via https://openrouter.ai/models?output_modalities=image
    # (2026-08): "google/gemini-3-pro-image-preview" ("Nano Banana Pro"),
    # "google/gemini-2.5-flash-image" ("Nano Banana"), "bytedance-seed/seedream-4.5",
    # "black-forest-labs/flux.2-pro" are all real, current slugs as of 2026-08 -- but
    # like Gemini's own model names (see gemini_image_model's comment above), these
    # rotate over time; a 400/404 "model not found" is a config fix via this env var, not
    # a bug. Default picked for output quality parity with the direct-Gemini path's
    # default; switch to the flash variant if cost/latency matters more than quality.
    openrouter_api_key: str | None = None
    openrouter_image_model: str = "google/gemini-3-pro-image-preview"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Same reasoning as gemini_image_request_timeout_s directly above: a detail-heavy
    # poster/infographic prompt routinely takes longer than a simple image.
    openrouter_image_request_timeout_s: float = 90.0

    # Rate limiting (requests per window per identity)
    rl_login_per_min: int = 10
    rl_refresh_per_min: int = 30
    rl_message_per_min: int = 60
    rl_generation_per_min: int = 10
    rl_sse_connect_per_min: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
