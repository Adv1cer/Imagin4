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

    # JobQueue backend. "memory" (default) = InMemoryJobQueue, process-local, gone on
    # restart -- fine for unit/e2e tests and quick local smoke-testing without Postgres
    # up, but NOT safe with >1 API replica or across restarts (see README "Known
    # limitations"). "postgres" = PostgresJobQueue (app/adapters/queue/postgres.py),
    # durable and shared across every API/scheduler/reconciler process -- required
    # before running more than one API replica or trusting job state to survive a
    # deploy/crash. Selected via app/adapters/queue/factory.py:build_job_queue, used by
    # app/main.py, app/services/scheduler.py, and app/services/reconciler.py so all
    # three agree on which backend is active from the same Settings value.
    queue_backend: Literal["memory", "postgres"] = "memory"

    # Object storage backend, same shape/reasoning as queue_backend directly above:
    # "memory" = InMemoryObjectStorage, process-local -- a generated image is only
    #   visible to whichever process uploaded it. Fine for tests/local smoke-testing.
    # "s3" = S3ObjectStorage (app/adapters/storage/s3.py) against settings.s3_* (MinIO in
    #   dev, real S3 in cloud) -- REQUIRED once queue_backend=postgres runs the
    #   scheduler/reconciler as separate containers/processes from the API, or a
    #   generated image succeeds but 404s when the API tries to serve it back (see
    #   GET /v1/jobs/{id}/asset) -- this is exactly as load-bearing as queue_backend for
    #   making that split-process deployment actually work, not an independent knob.
    storage_backend: Literal["memory", "s3"] = "memory"

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

    # Admission / fairness defaults.
    #
    # Enforcement status (2026-08-20, see app/domain/jobs/admission.py's
    # CapacityExceededError docstring): max_queued_jobs_per_user and global_queue_cap
    # ARE enforced, at admission time -- a new job is rejected (CapacityExceededError,
    # mapped to HTTP 429) if it would push either counter over its limit. This is what
    # stops one user's large batch of jobs from sitting ahead of every other user's jobs
    # in Scheduler._claim's queued_at-ordered claim query indefinitely: capped backlog
    # size means a heavy user must wait for their own jobs to clear before submitting
    # more, self-throttling instead of crowding everyone else out. <= 0 disables the
    # respective check entirely (unlimited).
    #
    # max_active_jobs_per_user (2026-08-20, added same day as the two above): enforced
    # at CLAIM time, not admission -- Scheduler._claim_for_backend passes this value as
    # JobQueue.claim_next_with_lease's `max_active_per_user`, which skips a candidate
    # job whose user already has this many jobs in dispatched/running state (see that
    # param's docstring, and app/adapters/queue/postgres.py's `_claim` for the SQL --
    # tested against a real local Postgres 16, including a concurrent-claim race test,
    # given that method's documented history of subtle concurrency bugs). Matters most
    # once GPU concurrency > 1 (see default_comfy_active_slots above) or for the Gemini
    # lane (default_gemini_active_slots=3) -- at ComfyUI concurrency 1 there's only ever
    # one active job for ANYONE at a time regardless of this setting.
    max_active_jobs_per_user: int = 1
    max_queued_jobs_per_user: int = 3
    global_queue_cap: int = 5000
    # Rough ETA estimate powering GenerationOut/JobOut's `estimated_wait_seconds`
    # (2026-08-20, see app/domain/jobs/admission.py:estimate_wait_seconds) --
    # `(queue_position // active_slots_for_backend) * estimated_job_duration_s`. This is
    # NOT measured from real completion times anywhere in this codebase yet, just a flat
    # per-job assumption -- 60s is a rough placeholder for one ComfyUI txt2img/img2img
    # job at the default comfy_steps=20. Benchmark your own actual wall-clock time per
    # job (see default_comfy_active_slots' comment above re: DGX Spark GPU-contention
    # benchmarking) and tune this to match your deployment. Deliberately a single knob,
    # not split per-backend (a Gemini poster job likely takes a different real duration
    # than a ComfyUI one) -- this is meant to be "better than silence" for a client
    # deciding whether to wait or come back later, not a scheduling promise.
    estimated_job_duration_s: float = 60.0
    # How many image_basic jobs the scheduler may claim+dispatch concurrently. Only
    # meaningful up to how many independent ComfyUI execution engines actually exist to
    # run them -- see comfy_worker_base_urls_csv above. With a single ComfyUI process
    # (comfy_worker_base_urls_csv empty), keep this at 1 until benchmarked otherwise: a
    # single GPU can't run two full generations in parallel just because two jobs were
    # dispatched to the same process. With N workers configured, this can in principle go
    # up to N (one in-flight job per worker) -- but per the same single-GPU caveat, if
    # those N workers share one physical GPU, more concurrent jobs does not mean
    # proportionally faster completion, only more overlap of otherwise-idle time (I/O,
    # model loading, etc.). Benchmark actual wall-clock throughput at each step rather
    # than assuming.
    #
    # Hard ceiling, not just a suggestion (2026-08-20): Scheduler._reserve_capacity_by_
    # backend now clamps live/postgres-mode capacity to this value too (previously it only
    # summed each online comfy_workers row's slots, uncapped -- see that method's
    # docstring for the "Gap closed" note and the ~5x step-time regression that showed up
    # on Chet's DGX Spark when 2 workers sharing one GB10 sampled at the same time). Only
    # raise this past 1 after confirming your workers don't actually share a physical GPU.
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

    # Second, optional "model profile" -- see app/domain/jobs/comfy_profiles.py for why
    # this exists (2026-08-19): an external agentflow team's own master-prompt/UI
    # decides, per user role, whether a job should use the "student" model above (the
    # default, comfy_* fields, unchanged) or this better/slower "personnel" one, and
    # sends us that decision as `inputs.model_profile: "personnel"` on
    # POST /v1/generations -- validated against a small server-side allowlist, never a
    # raw filename from the caller (same invariant as comfy_checkpoint_name above).
    # Leave comfy_personnel_checkpoint_name AND comfy_personnel_diffusion_model_name
    # both empty (the default) to not register this tier at all -- a request for
    # model_profile="personnel" then fails admission with 400 "unknown model_profile"
    # instead of silently reusing the student model. Same field shape/meaning as the
    # comfy_* block above; see those fields' comments for what each one does.
    comfy_personnel_checkpoint_name: str = ""
    comfy_personnel_model_family: Literal["checkpoint", "qwen_image"] = "checkpoint"
    comfy_personnel_diffusion_model_name: str = ""
    comfy_personnel_clip_name: str = ""
    comfy_personnel_vae_name: str = ""
    comfy_personnel_model_sampling_shift: float = 3.1
    comfy_personnel_sampler_name: str = "euler"
    comfy_personnel_scheduler: str = "normal"
    comfy_personnel_steps: int = 30
    comfy_personnel_cfg_scale: float = 7.0
    comfy_personnel_negative_prompt: str = ""

    # Per-request field overrides on top of whichever model_profile was resolved -- see
    # app/domain/jobs/comfy_overrides.py for the full design/rationale. Every *_CSV
    # allowlist below is empty by default, which means that field CANNOT be overridden
    # at all (a request that tries gets 400 "overrides are not enabled..."); list the
    # exact filenames actually installed on your ComfyUI worker(s) to open it up --
    # there is no discovery from our side, same caveat as comfy_checkpoint_name etc.
    # model_family is deliberately NOT in this list -- see the module docstring for why
    # (switching families changes the whole graph shape, not a per-field tweak).
    comfy_allowed_checkpoints_csv: str = ""
    comfy_allowed_diffusion_models_csv: str = ""
    comfy_allowed_clips_csv: str = ""
    comfy_allowed_vaes_csv: str = ""
    comfy_allowed_samplers_csv: str = ""
    comfy_allowed_schedulers_csv: str = ""
    # Bounds for the numeric overrides (steps/cfg_scale) -- see the 2026-08-18
    # burst-test incident notes in app/core/rate_limit.py for why "no cap" on
    # compute-cost-per-job is a proven real risk on this deployment, not theoretical.
    comfy_override_min_steps: int = 1
    comfy_override_max_steps: int = 50
    comfy_override_min_cfg_scale: float = 1.0
    comfy_override_max_cfg_scale: float = 15.0
    comfy_override_max_negative_prompt_chars: int = 2000

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

    # Which model powers chat replies + the semantic intent router (app/adapters/gemini.py's
    # GeminiTextClient vs app/adapters/qwen.py's QwenTextClient -- see app/main.py's
    # _build_state). "gemini" (default) is the original, fully-featured path (grounded
    # Google Search research included). "qwen" points app.state.gemini_text_client (name
    # kept for now -- see app/api/deps.py's get_gemini_text_client, still the one
    # dependency chat_router.py/agent_router.py/conversations.py use) at a self-hosted
    # Qwen3.8-27B served over vLLM's OpenAI-compatible API instead, for cost control and
    # to keep chat traffic off an external API entirely (Chet, 2026-08-18).
    #
    # KNOWN GAP, read before flipping this in production: QwenTextClient.research_missing_fields
    # ALWAYS raises (there is no equivalent of Gemini's built-in google_search grounding
    # tool wired up here) -- chat_router.py already treats research failures as
    # best-effort/optional and falls back to just asking the user, so this degrades
    # gracefully rather than breaking anything, but it does mean POSTER/INFOGRAPHIC jobs
    # lose the "auto-fill a publicly-known missing field" capability entirely under
    # brain_backend="qwen". Wiring a real search tool (e.g. Serper/Brave + vLLM tool
    # calling) is a separate future increment, not done here.
    brain_backend: Literal["gemini", "qwen"] = "gemini"

    # vLLM OpenAI-compatible server hosting Qwen3.8-27B (see docker-compose.yml's
    # `qwen-brain` service) -- only used when brain_backend="qwen". Released 2026-08-14,
    # Apache 2.0, native hybrid-attention (48 Gated DeltaNet linear-attention layers + 16
    # full Gated Attention layers) architecture -- much lighter KV cache per token than a
    # plain transformer of this size, but NOT the reason it was picked here (chat/routing
    # messages are short; long-context KV cost barely matters for this use case).
    #
    # QUANTIZATION CHOICE, read before changing qwen_text_model: default below points at
    # an FP8 checkpoint (~27-33GB weights per public reports), NOT the NVFP4 checkpoints
    # also on the Hub (~19GB, more VRAM headroom on paper). This is deliberate: GB10/DGX
    # Spark's tensor cores are sm_121 ("consumer Blackwell"), and NVIDIA's own tcgen05 FP4
    # tensor-core path is documented as hard-restricted to sm_100a/sm_103a -- explicitly
    # EXCLUDING sm_121 (confirmed via NVIDIA dev forum, 2026-08). NVFP4 weights would
    # likely load and run, but may silently fall back to a slower dequant path rather than
    # getting real FP4 tensor-core acceleration on this specific chip -- unconfirmed
    # either way as of this writing, so FP8 (broadly supported since Hopper, safer bet
    # pending a real benchmark) is the conservative default. Benchmark NVFP4 for real
    # before trusting the smaller footprint in production.
    #
    # VRAM BUDGET, read before choosing worker count alongside this: DGX Spark has 128GB
    # unified memory shared between CPU and GPU. FP8 Qwen3.8-27B needs roughly 30-35GB
    # (weights + modest KV cache for short chat/routing messages, not the model's full
    # 262144-token context). 4 ComfyUI workers via MPS were measured using most of the
    # remaining budget with little headroom (see README's MPS section) -- running the
    # brain alongside 4 image workers is tight. Alongside 2 ComfyUI workers there is
    # comfortable headroom. This is the other half of the still-open "2 vs 4 workers"
    # decision (see README's "Known issue: worker-3/4 CUBLAS crash" section) -- adding the
    # brain is a real argument for settling on 2.
    qwen_base_url: str = "http://localhost:8100"
    qwen_text_model: str = "Qwen/Qwen3.8-27B-FP8"
    qwen_request_timeout_s: float = 30.0
    # See the research_missing_fields gap noted above -- kept for interface parity with
    # gemini_research_timeout_s even though QwenTextClient's research call always raises
    # immediately (no network call actually made), so this value doesn't do much yet.
    qwen_research_timeout_s: float = 20.0

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
    # NOTE (2026-08-18): these settings existed but were never wired into any endpoint --
    # app/core/rate_limit.py:check_rate_limit / app/api/deps.py:rate_limited() is the
    # enforcement, applied at the POST /v1/generations, /smart-message, /agent/message
    # call sites (see app/api/v1/generations.py, chat_router.py, agent_router.py).
    rl_login_per_min: int = 10
    rl_refresh_per_min: int = 30
    rl_message_per_min: int = 60
    rl_generation_per_min: int = 10
    rl_sse_connect_per_min: int = 30

    # Admission control -- see app/core/rate_limit.py:AdmissionGate's docstring for the
    # 2026-08-18 burst-test incident this exists to prevent. FLEET-WIDE cap (shared via
    # Redis across every API replica), not per-process: size it to the shared PgBouncer/
    # Postgres capacity (pgbouncer DEFAULT_POOL_SIZE in docker-compose.yml), not to a
    # single replica's own db_pool_size + db_max_overflow. 0 disables the gate.
    # Preview/final two-stage generation (2026-08-20, see PR discussion following
    # Chet's 100-user architecture proposal: "separate preview (low steps/resolution,
    # fast/distilled model) vs final (full quality) generation"). See
    # app/domain/jobs/admission.py:admit_generation_job's `inputs.preview` handling and
    # POST /v1/generations/{id}/finalize in app/api/v1/generations.py for the actual
    # flow. SCOPE NOTE, read before assuming this matches the full proposal: this only
    # fast-forwards the STEPS knob (an existing, allowlisted per-request override --
    # see app/domain/jobs/comfy_overrides.py) down to preview_steps. It deliberately
    # does NOT touch resolution/width/height (no such knob is plumbed through to the
    # ComfyUI graph builder anywhere in this codebase -- app/adapters/comfyui/live.py's
    # graph templates are fixed-resolution) and does NOT lock a random seed for
    # preview/final visual consistency (no seed control exists in ComfyProfile
    # either) -- both would require real ComfyUI-graph surgery, deliberately out of
    # scope for this increment. A preview and its later finalize therefore render the
    # SAME prompt at DIFFERENT random seeds, not a visually-identical-but-lower-quality
    # pair -- documented here as an explicit, known trade-off, not a bug to silently
    # "fix" later without planning the graph change (width/height + seed passthrough)
    # first.
    preview_enabled: bool = True
    preview_steps: int = 6

    admission_max_inflight: int = 150
    admission_gate_ttl_s: int = 60
    # `Retry-After` header value on the 503 AdmissionGate itself returns (2026-08-20,
    # see app/api/deps.py:check_admission_capacity) -- deliberately a small FIXED
    # guess, not derived from admission_gate_ttl_s (that's how long a LEAKED/stale
    # counter takes to self-heal, an unrelated worst case -- see AdmissionGate's own
    # docstring). Under normal operation the in-flight counter churns as fast as
    # requests complete, so a short fixed retry is the right advice almost all the
    # time; a client that's still shed after retrying can simply retry again.
    admission_gate_retry_after_s: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
