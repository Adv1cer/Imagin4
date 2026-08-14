# Imaginv4 Backend

FastAPI backend for a university multi-user AI image-generation platform: chat-style
conversations that can request image generations, which are queued, fairly scheduled
across a pool of ComfyUI workers, retried on transient failure, and served back through
object storage.

## Architecture overview

```
                 ┌────────────┐        ┌──────────────┐
   client ──────▶│  FastAPI   │───────▶│  Postgres     │ (users, conversations,
                 │  api (v1)  │        │  (via pgbouncer)│ generation_jobs, job_attempts,
                 └─────┬──────┘        └──────────────┘  job_events, comfy_workers, assets,
                       │ enqueue                          scheduler_leases, ...)
                       ▼
                 ┌────────────┐   claim+dispatch   ┌──────────────┐
                 │  JobQueue   │◀───────────────────│  scheduler    │──submit──▶ ComfyUI
                 │  (port)     │                    │  (app/services)│           (mock/live)
                 └─────┬──────┘                    └──────────────┘
                       │ lease/expiry, prompt_id
                       ▼
                 ┌──────────────┐   finalize/retry/fail (job_events)
                 │  reconciler   │◀── polls ComfyUI status for known prompt_id
                 │  (app/services)│
                 └──────────────┘

   Redis: rate limiting (app/core/rate_limit.py) + SSE fan-out backing store (future)
   MinIO/S3: generated image bytes (app/adapters/storage, ObjectStorage port)
```

Everything that talks to Postgres/Redis/S3/ComfyUI is expressed as a `Protocol` "port"
(`app/adapters/{queue,storage,comfyui}/__init__.py`) with an in-memory fake used by
tests and local dev without external infra. The API layer (`app/api/v1/*`), scheduler
(`app/services/scheduler.py`), and reconciler (`app/services/reconciler.py`) all depend
only on the ports, never on a concrete backend, so swapping in a real Postgres-backed
`JobQueue` (SKIP LOCKED-based claiming) or a live ComfyUI HTTP client is a matter of
implementing the Protocol -- no call-site changes.

Domain logic is deliberately factored out of I/O and is exercised directly by unit
tests:
- `app/domain/jobs/state_machine.py` -- legal `generation_jobs.state` transitions.
- `app/domain/jobs/fairness.py` -- priority aging / weighted round robin.
- `app/domain/jobs/retry.py` -- retryable-error classification + exponential backoff with jitter.
- `app/domain/jobs/idempotency.py`, `ownership.py`, `workflow_registry.py` -- admission rules.
- `app/domain/workers/scoring.py` -- worker eligibility + selection (lowest score wins).
- `app/domain/auth/*` -- Argon2id password hashing, opaque session tokens (not JWT).
- `app/core/rate_limit.py` -- rate-limit key derivation.

## Schema (10 tables)

| Table | Purpose |
|---|---|
| `users` | Accounts; Argon2id `password_hash`, `status` (active/suspended/deleted). |
| `auth_sessions` | Opaque session tokens (hashed), sliding TTL, revocation. |
| `conversations` | Chat threads owned by a user. |
| `chat_messages` | Messages within a conversation; ordered by `sequence_no`. |
| `generation_jobs` | One row per image-generation request; the core state machine. |
| `job_attempts` | One row per dispatch attempt of a job (lease, worker, prompt_id, outcome). |
| `job_events` | Append-only audit trail of every state transition / action taken. |
| `comfy_workers` | Registered ComfyUI worker capacity/health for scoring & scheduling. |
| `assets` | Object-storage references (never binary data) for generated outputs. |
| `scheduler_leases` | Coordination row so multiple scheduler replicas don't double-admit. |

Full column/index/constraint definitions: `app/db/models.py`. Migrations:
`alembic/versions/0001_initial_schema.py` creates all 10 tables in one migration.

## Job state machine

```
 queued ──▶ admitted ──▶ dispatched ──▶ running ──▶ succeeded
   │            │             │            │
   │            │             │            ├──▶ retry_wait ──▶ queued (backoff+jitter)
   │            │             │            │
   ├──▶ cancelled            ├──▶ failed   ├──▶ cancelling ──▶ cancelled
   │            │             │            │
   └──▶ failed  └──▶ failed   └──▶ retry_wait
```

`app/domain/jobs/state_machine.ALLOWED_TRANSITIONS` is the single source of truth; both
the API and the scheduler/reconciler call `assert_transition`/`can_transition` rather
than hand-rolling logic, so they can never disagree. `retry_wait ──▶ queued` re-enters
the fair scheduling queue with `effective_priority` boosted by elapsed wait time (aging),
so retried jobs don't get starved behind newer high-priority jobs forever.

## Local dev commands

```bash
cd backend
cp .env.example .env            # edit as needed

# Full stack, mock ComfyUI (no GPU needed -- local dev / CI):
docker compose --profile mock up -d postgres pgbouncer redis minio minio-init mock-comfyui
docker compose run --rm api alembic upgrade head
docker compose up -d api scheduler reconciler

# Full stack, real GPU worker(s) (see docker/comfyui-worker/Dockerfile and set
# APP_COMFY_MODE=live + APP_COMFY_WORKER_BASE_URLS_CSV in .env first -- NEVER run this
# alongside mock-comfyui, both bind host port 8188 and will collide silently):
docker compose up -d postgres pgbouncer redis minio minio-init comfyui-worker-1 comfyui-worker-2
docker compose run --rm api alembic upgrade head
docker compose up -d api scheduler reconciler

# Or run the API/scheduler/reconciler directly against in-memory fakes (no infra needed
# -- this is how app/main.py and app/services/{scheduler,reconciler}.py's main() are
# wired today; see "Known limitations" below):
uvicorn app.main:app --reload
python -m app.services.scheduler
python -m app.services.reconciler
```

## Migrations

```bash
alembic upgrade head           # apply all migrations
alembic downgrade -1           # roll back one revision
alembic revision --autogenerate -m "description"   # generate a new migration
```

## Tests

```bash
pytest                         # unit + contract + e2e smoke (in-memory fakes, no infra)
pytest tests/unit               # domain logic only
pytest tests/e2e                # FastAPI TestClient smoke tests
pytest tests/integration        # requires a real Postgres (auth/conversations use CITEXT/JSONB)
ruff check .
black --check .
```

## Load tests

```bash
# Requires k6 (https://k6.io) and a running stack (BASE_URL defaults to
# http://localhost:8000).
k6 run backend/load_tests/smoke.js
k6 run backend/load_tests/baseline.js
k6 run backend/load_tests/spike.js
k6 run backend/load_tests/soak.js

# campus_peak.js is deliberately gated -- see the file header. Only run it against a
# disposable environment, never in CI:
RUN_CAMPUS_PEAK=1 k6 run backend/load_tests/campus_peak.js

# Or via the opt-in docker-compose profile:
docker compose --profile load-test run --rm k6 run /scripts/baseline.js
```

## Mock vs. live ComfyUI vs. Gemini

Controlled by `APP_COMFY_MODE` (`mock` | `live`, see `app/core/config.py`) **unless**
`APP_GEMINI_API_KEY` is set, in which case Gemini takes over image generation entirely
regardless of `APP_COMFY_MODE` (see `app/main.py::_build_state`):

- `mock` (default, no Gemini key): `app.adapters.comfyui.MockComfyUIClient`, a
  deterministic in-process fake -- every submit "completes" after `polls_to_complete`
  polls and produces a fake asset keyed by a hash of the input payload (so identical
  inputs produce identical fake outputs, useful for idempotency tests). No network
  calls, no GPU required.
- `live`: intended to talk to a real ComfyUI instance at `APP_COMFY_BASE_URL`. **The live
  HTTP adapter is not yet implemented** -- `app/main.py::_build_state` currently
  constructs `MockComfyUIClient` unconditionally with a `# Live ComfyUI HTTP adapter
  would be constructed here` comment marking the gap. Implementing
  `app.adapters.comfyui.ComfyUIClient` against ComfyUI's real HTTP API is the next
  concrete increment (see "Next safe increment" in the final task report).
- **Gemini** (`APP_GEMINI_API_KEY` set): `app.adapters.gemini.GeminiImageComfyUIClient`
  implements the same `ComfyUIClient` port using Google's Gemini API
  (`APP_GEMINI_IMAGE_MODEL`, default `gemini-3.1-flash-image`, aka "Nano Banana 2")
  instead of ComfyUI -- the scheduler/reconciler/job-state-machine code is completely
  unaware of the swap. Get a free-tier key at <https://aistudio.google.com/>. Also
  enables real text chat replies via `POST /v1/conversations/{id}/assistant-reply`
  using `app.adapters.gemini.GeminiTextClient` (`APP_GEMINI_TEXT_MODEL`, default
  `gemini-3.6-flash`) -- without a key, that endpoint returns `503`. **Google retires
  model names periodically** (`gemini-2.0-flash` then `gemini-2.5-flash` both stopped
  working for new API keys during development of this feature) -- if you see a `404
  ... no longer available` error, check https://ai.google.dev/gemini-api/docs/models
  for the current list and update `APP_GEMINI_TEXT_MODEL`/`APP_GEMINI_IMAGE_MODEL` in
  your `.env`; this is expected maintenance, not a bug. Generated images are streamed
  back to the client via `GET /v1/jobs/{id}/asset` (ownership-checked).

## Capacity planning

The core sizing formula for how many concurrent ComfyUI generation slots you need:

```
required_generation_slots ≈ generation_arrival_rate_per_second × p95_generation_seconds ÷ target_utilization
```

Example: if generations arrive at 0.5/s on average during a campus peak, a single
generation takes ~12s at p95, and you target 70% utilization (headroom for variance and
worker churn): `required_generation_slots ≈ 0.5 × 12 ÷ 0.7 ≈ 8.6 → 9 slots`. Slots come
from `comfy_workers.max_slots` summed across online, eligible workers (see
`app/domain/workers/scoring.py::is_eligible`); size the worker pool so total slots meet
or exceed this number with margin for draining/failed workers.

**10,000 registered users does NOT imply 10,000 simultaneous generations.** Registered
users are a ceiling on possible demand, not a concurrency estimate. Realistic
concurrency assumptions for this platform:
- Only a small fraction of registered users are active at any given moment (campus
  usage is bursty around class schedules/deadlines, not uniformly distributed).
- `max_active_jobs_per_user = 1` (see `app/core/config.py`) caps each user to one
  in-flight generation, so concurrency is bounded by *active, currently-waiting* users,
  not total registrations.
- `rl_generation_per_min` and `global_queue_cap` further bound the arrival rate and
  total outstanding work regardless of how many users are technically online.
- Capacity planning should be driven by measured `generation_arrival_rate_per_second`
  from real traffic (or a conservative load-test assumption, e.g. 1-3% of registered
  users generating within the same 60s window during a peak), not by the full user count.
  Provisioning for 10,000 simultaneous generations when realistic peak concurrency is in
  the tens-to-low-hundreds would be a large and unnecessary overprovisioning of GPU
  capacity.

## Duplicate-execution window (caveat)

There is a narrow window between (a) ComfyUI accepting a submitted prompt and returning
a `prompt_id`, and (b) that `prompt_id` being durably persisted (in production, an
`UPDATE job_attempts SET comfy_prompt_id = ...` committed to Postgres). If the scheduler
process crashes in that window, ComfyUI may be executing (or may complete) a generation
that our system has no durable record of having submitted.

**Risk:** on reconciliation, the job looks like "dispatched but no prompt_id" -- from our
side indistinguishable from "never actually submitted". The conservative choice (see
`app/services/reconciler.py::_reconcile_job`) is to treat this as a failure and either
retry or fail the job once its lease expires. Retrying re-submits the workflow to
ComfyUI, so in the crash-after-accept-before-persist scenario, the *original* accepted
prompt may still complete and produce an orphaned/duplicate asset that no job/user is
ever shown, wasting GPU time but not corrupting user-visible state.

**Mitigation:**
1. Keep the accept-to-persist window as short as possible: submit to ComfyUI and persist
   `prompt_id` in the same logical step, with the DB write immediately following the
   HTTP response (already the shape of `Scheduler._dispatch`).
2. Idempotency keys upstream of admission (`app/domain/jobs/idempotency.py`) prevent the
   *user-visible* symptom (a duplicate job) even if ComfyUI itself double-executes.
3. A periodic orphan sweep against ComfyUI's own history/queue endpoint (beyond
   per-job reconciliation) can detect and cancel prompts with no matching
   `job_attempts.comfy_prompt_id` -- not yet implemented; see "Next safe increment".
4. This is a known, accepted risk class for any system driving an external job runner
   without two-phase commit; it is not fully eliminable without ComfyUI supporting
   idempotent submission natively.

## Backup / restore

**Postgres:**
```bash
docker compose exec postgres pg_dump -U imaginv -Fc imaginv > imaginv_$(date +%Y%m%d).dump
docker compose exec -T postgres pg_restore -U imaginv -d imaginv --clean < imaginv_20260101.dump
```
Run `pg_dump` against a replica or during low-traffic windows in production; `job_events`
is append-only and will grow quickly under load, consider partitioning/archiving it
separately from the rest of the schema.

**MinIO / object storage:**
```bash
docker compose exec minio-init mc mirror local/imaginv-assets /backup/imaginv-assets
# restore:
docker compose exec minio-init mc mirror /backup/imaginv-assets local/imaginv-assets
```
In production, prefer S3 versioning + cross-region replication over manual mirroring.
`assets.sha256` lets you verify restored objects match what's recorded in Postgres.

## Production readiness checklist

- [ ] Replace `InMemoryJobQueue` with a Postgres `SELECT ... FOR UPDATE SKIP LOCKED`
      implementation of the `JobQueue` port (see comments in `app/adapters/queue/__init__.py`).
- [ ] Replace `InMemoryObjectStorage` with an S3/MinIO-backed `ObjectStorage` implementation.
- [x] Implement the live `ComfyUIClient` HTTP adapter (`APP_COMFY_MODE=live`).
- [x] Run more than one ComfyUI execution engine at once -- `MultiWorkerComfyUIClient`
      (`app/adapters/comfyui/multi_worker.py`, wired via `APP_COMFY_WORKER_BASE_URLS_CSV`)
      round-robins across a fixed, statically-configured list of worker URLs (e.g.
      `comfyui-worker-1`/`comfyui-worker-2` in docker-compose.yml). This is deliberately
      simpler than the `comfy_workers`-table-backed scoring/heartbeat system described
      below -- fine for a small fixed worker count, but doesn't do capability-aware
      routing, health-based selection, or dynamic registration. Graduate to the real
      thing (next line) if the worker pool needs to grow dynamically or route by model
      capability.
- [ ] Wire `app/services/scheduler.py` / `reconciler.py` to a real `session_factory` so
      `_reserve_capacity_by_backend` scores live `comfy_workers` rows instead of falling
      back to `default_comfy_active_slots` (Gemini capacity is separate --
      `default_gemini_active_slots` -- and always used regardless of this).
- [ ] Wire reconciler `job_events` emission to real DB inserts instead of log lines.
- [ ] Set `APP_COOKIE_SECURE=true`, real `APP_CORS_ALLOW_ORIGINS`, and rotate
      `POSTGRES_PASSWORD` / `MINIO_ROOT_PASSWORD` away from the `.env.example` placeholders.
- [ ] Configure Postgres backups (see above) and verify a restore drill.
- [ ] Load-test with `baseline.js` + `spike.js` against a staging environment sized per
      the capacity-planning formula above, before any real campus rollout.
- [ ] Run `campus_peak.js` deliberately at least once against staging to validate SSE
      fan-out behavior under peak concurrency.
- [ ] Add the orphan-prompt sweep described in the duplicate-execution mitigation above.
- [ ] Add structured logging/metrics dashboards for `job_events` rates by `event_type`
      and scheduler/reconciler loop latency.

## Troubleshooting

- **`pytest` fails with `CITEXT`/`JSONB` errors**: `tests/integration` requires real
  Postgres (SQLite/in-memory can't represent these types); run only `tests/unit` and
  `tests/e2e` without a DB, or bring up `postgres` via docker-compose first.
- **`docker compose up` services never become healthy**: check `docker compose logs
  <service>`; the most common cause locally is `pgbouncer` starting before `postgres`
  finishes initializing on first run -- it has a `depends_on: condition:
  service_healthy` guard, but a first-run volume init can still take a few extra seconds.
- **Generations stuck in `queued` with `scheduler` running**: confirm
  `default_comfy_active_slots` (or live `comfy_workers` capacity) is > 0, and check
  scheduler logs for `dispatch failed` entries -- a failing `ComfyUIClient.submit` marks
  the job `retry_wait`/`failed` rather than leaving it `queued` forever.
- **Jobs stuck `dispatched`/`running` forever**: confirm the `reconciler` process is
  running; it is what finds expired leases and finalizes/retries orphaned jobs.
- **`k6` scripts fail at login**: they expect a seeded test user (`student@example.edu`
  / `correct-horse-battery-staple` by default, overridable via `SMOKE_EMAIL`/
  `SMOKE_PASSWORD` env vars for k6 and `--email`/`--password` for the seed script).
  Seed one with:
  `docker compose run --rm api python -m scripts.seed_dev_user`
  Safe to re-run (idempotent -- updates the existing user's password hash instead of
  failing on the unique email constraint).
- **429s under normal load**: check `rl_generation_per_min` / `global_queue_cap` in
  `.env` -- these are intentionally conservative defaults for a shared campus deployment.

## Known limitations (of this repo, as of this change)

- Docker/Postgres/Redis/k6 were not installed in the sandbox this change was authored
  in. `docker-compose.yml` was schema-validated (`python -c "import yaml; ...")` but
  never run end-to-end; the k6 scripts were validated with `node --check` for syntax
  only, never executed against a live stack.
- Only in-memory fakes exist for `JobQueue`/`ObjectStorage`/`ComfyUIClient` today; the
  scheduler/reconciler are written against the `Protocol` ports so a Postgres/S3/live
  ComfyUI adapter is a drop-in swap, but that swap has not been implemented in this change.
- `job_events` emission from the reconciler is currently a structured log line, not a DB
  row (see production readiness checklist).
