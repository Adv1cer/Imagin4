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
    default_comfy_active_slots: int = 1
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
    comfy_request_timeout_s: float = 10.0
    # Only used when comfy_mode == "live" (see app/adapters/comfyui/live.py). The
    # checkpoint filename MUST match exactly what's installed on the target ComfyUI
    # instance (its models/checkpoints directory) -- there is no discovery/validation of
    # this from our side, a wrong name fails the job with a ComfyUI-side graph error.
    comfy_checkpoint_name: str = "sd_xl_base_1.0.safetensors"
    comfy_sampler_name: str = "euler"
    comfy_scheduler: str = "normal"
    comfy_steps: int = 20
    comfy_cfg_scale: float = 7.0
    comfy_negative_prompt: str = ""

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

    # Rate limiting (requests per window per identity)
    rl_login_per_min: int = 10
    rl_refresh_per_min: int = 30
    rl_message_per_min: int = 60
    rl_generation_per_min: int = 10
    rl_sse_connect_per_min: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
