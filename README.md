# Imaginv4 backend

University multi-user AI image-generation platform backend (FastAPI + Postgres + Redis +
MinIO + ComfyUI). See `backend/` for the application and `docs/` (to be expanded) for
architecture notes.

## Status (see PR/commit history for detail)

This increment implements the foundational domain layer with full unit-test coverage:
job state machine, retry/backoff, fair scheduling + priority aging, worker scoring,
Argon2id password hashing, opaque session-token issuance/validation, idempotency-key
comparison, cursor pagination, ownership checks, workflow allowlist, rate-limit key
derivation, plus the complete SQLAlchemy schema (10 tables) and a hand-written initial
Alembic migration. The FastAPI app, live DB/Redis/MinIO/ComfyUI wiring, SSE endpoints,
and docker-compose stack are the next increment (see `docs/NEXT_STEPS.md` once added).

## Local dev

```
cd backend
python -m venv .venv && . .venv/bin/activate   # use a fast local disk, not a network share
pip install -e ".[dev]"
pytest tests/unit -q
```

Migrations (requires a running Postgres, see docker-compose once added):

```
alembic upgrade head
```
