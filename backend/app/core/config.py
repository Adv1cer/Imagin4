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

    # Rate limiting (requests per window per identity)
    rl_login_per_min: int = 10
    rl_refresh_per_min: int = 30
    rl_message_per_min: int = 60
    rl_generation_per_min: int = 10
    rl_sse_connect_per_min: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
