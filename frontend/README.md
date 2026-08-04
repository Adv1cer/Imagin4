# Imaginv4 Frontend

A small React + TypeScript + Tailwind chat UI for the Imaginv4 backend (`../backend`).

## Running in dev

```bash
npm install
npm run dev
```

The dev server runs on **http://localhost:3000** — this port is intentional (configured
in `vite.config.ts` via `server.port = 3000`), not the Vite default of 5173. It matches
the backend's current CORS default (`APP_CORS_ALLOW_ORIGINS_CSV=http://localhost:3000` in
`backend/app/core/config.py` / `backend/.env.example`), so no backend `.env` edit is
needed to run this app out of the box.

**If you change the frontend's port**, you must add the new origin to
`APP_CORS_ALLOW_ORIGINS_CSV` in `backend/.env` (comma-separated), or the browser will
block every request with a CORS error.

## Backend must be running first

This app has no backend of its own. Start the Imaginv4 backend stack before using it:

```bash
cd ../backend
cp .env.example .env   # first time only
docker compose up -d postgres pgbouncer redis minio minio-init mock-comfyui
docker compose run --rm api alembic upgrade head
docker compose up -d api scheduler reconciler
```

The API is expected at `http://127.0.0.1:8000` (see `docker-compose.yml`'s `api` service).

## How it talks to the backend

- Base URL: `http://127.0.0.1:8000` by default, overridable via the `VITE_API_BASE_URL`
  env var (see `.env.example` — copy to `.env.local` to change it).
- Auth is **cookie-based**, not token-based: every request is sent with
  `credentials: 'include'` (see `src/api/client.ts`), and no token is ever stored in
  `localStorage`/`sessionStorage`. Login sets an `httpOnly` session cookie server-side.
- All fetch calls go through the single wrapper in `src/api/client.ts`, which centralizes
  the base URL, `credentials: 'include'`, JSON encoding, and error handling
  (`ApiError` with `status`/`detail`).

## What's real vs. mocked

Verified directly against the backend source before wiring anything up
(`backend/app/api/v1/*.py`, `backend/app/core/config.py`):

- **Real / wired to the backend:** login, `GET /v1/auth/me`, logout, conversation
  creation (`POST /v1/conversations`), message history loading
  (`GET /v1/conversations/{id}/messages`, cursor-paginated via `next_cursor`), and image
  generation end-to-end (`POST /v1/generations` with an `Idempotency-Key` header, then
  polling `GET /v1/jobs/{id}` every ~1.5s until a terminal state).
- **Mocked (backend does not support these — see "Known backend gaps" below):** the
  paperclip attach button, and every Tools-menu item except "Image generation" (Create
  graph/dashboard, Web search, Create Word/Excel/PowerPoint) — all show a "coming soon"
  toast and do nothing else. Plain text chat replies are also mocked client-side (see
  below).

## Known backend gaps discovered during research (not fixed here — backend was not touched)

- **No endpoint to create a chat message.** `backend/app/api/v1/conversations.py` only
  has `GET /{conversation_id}/messages`; there is no `POST`. There is also no
  chat-completion / LLM endpoint anywhere in the backend. As a result, plain text
  messages in this UI exist only in local React state (`src/types/chat.ts`,
  `src/screens/ChatScreen.tsx`) — the "assistant" reply to a text message is a canned
  local string, not a real model response, and nothing is persisted server-side for text
  turns. Recommendation: add `POST /v1/conversations/{id}/messages` (and, if actual
  chat/LLM behavior is wanted, a completion endpoint) before this can be a truly
  persisted chat.
- **No asset/signed-URL endpoint.** `GET /v1/jobs/{id}` returns `result.outputs[].object_key`
  (an internal storage key), but there is no route that turns that into a fetchable image
  URL (e.g. a signed S3/MinIO URL). This UI shows the raw `object_key` as text once a job
  succeeds rather than an `<img>` preview. Recommendation: add something like
  `GET /v1/assets/{object_key}` or embed a signed URL in the job result.
- **Scheduler/reconciler processes each construct their own `InMemoryJobQueue()`**
  (see `backend/app/services/scheduler.py` and `reconciler.py`), separate from the one
  built in `app.state` by `app/main.py`'s `_build_state()`. In the current in-memory/dev
  wiring (`docker-compose.yml` runs `api`, `scheduler`, and `reconciler` as separate
  processes), a job submitted via the API will likely sit at `state: "queued"` forever,
  since the scheduler polling a *different* queue instance never sees it. This is a
  backend/infra issue, not a frontend one — the frontend polls correctly and will simply
  show "queued" indefinitely until the backend wires a shared queue (e.g. the documented
  Postgres/Redis-backed adapter).

## Other setup notes

- Node 18+ recommended (built and tested with Node 22).
- `npm run build` runs `tsc -b && vite build` — verified to complete with zero
  TypeScript errors.
- `npm run lint` runs `oxlint` — verified to complete with zero warnings/errors.
