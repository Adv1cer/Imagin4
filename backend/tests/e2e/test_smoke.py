"""End-to-end smoke test: exercises the FastAPI app with only in-memory fakes wired in
(no Postgres/Redis/MinIO/ComfyUI required). Covers health, generation admission with
Idempotency-Key replay/conflict semantics, job polling, and cancellation.

Auth/conversations routes are DB-backed (require Postgres-only column types such as
CITEXT/JSONB) and are exercised separately in tests/integration against a real Postgres
instance; they are intentionally out of scope for this in-memory smoke test.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.comfyui import MockComfyUIClient
from app.adapters.queue import InMemoryJobQueue
from app.adapters.storage import InMemoryObjectStorage
from app.api.deps import get_current_user
from app.api.v1 import generations, health, jobs
from app.db.models import User


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.job_queue = InMemoryJobQueue()
    app.state.storage = InMemoryObjectStorage()
    app.state.comfy_client = MockComfyUIClient()
    app.state.session_factory = None  # no DB in this smoke test

    app.include_router(health.router, prefix="/v1")
    app.include_router(generations.router, prefix="/v1")
    app.include_router(jobs.router, prefix="/v1")

    fake_user = User(
        id=uuid.uuid4(), email="student@example.edu", display_name="Test Student", status="active"
    )

    async def _fake_current_user() -> User:
        return fake_user

    app.dependency_overrides[get_current_user] = _fake_current_user
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


def test_health_live(client: TestClient) -> None:
    resp = client.get("/v1/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_generation_admission_and_job_status(client: TestClient) -> None:
    resp = client.post(
        "/v1/generations",
        headers={"Idempotency-Key": "key-1"},
        json={
            "workflow_name": "txt2img_basic",
            "workflow_version": "v1",
            "inputs": {"prompt": "a friendly robot"},
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "queued"
    job_id = body["id"]

    get_resp = client.get(f"/v1/jobs/{job_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == job_id


def test_generation_idempotency_replay(client: TestClient) -> None:
    payload = {
        "workflow_name": "txt2img_basic",
        "workflow_version": "v1",
        "inputs": {"prompt": "same prompt"},
    }
    first = client.post("/v1/generations", headers={"Idempotency-Key": "dup-key"}, json=payload)
    second = client.post("/v1/generations", headers={"Idempotency-Key": "dup-key"}, json=payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("idempotency-replayed") == "true"


def test_generation_idempotency_conflict(client: TestClient) -> None:
    client.post(
        "/v1/generations",
        headers={"Idempotency-Key": "conflict-key"},
        json={"workflow_name": "txt2img_basic", "workflow_version": "v1", "inputs": {"prompt": "a"}},
    )
    resp = client.post(
        "/v1/generations",
        headers={"Idempotency-Key": "conflict-key"},
        json={"workflow_name": "txt2img_basic", "workflow_version": "v1", "inputs": {"prompt": "b"}},
    )
    assert resp.status_code == 409


def test_generation_unknown_workflow_rejected(client: TestClient) -> None:
    resp = client.post(
        "/v1/generations",
        headers={"Idempotency-Key": "k"},
        json={"workflow_name": "nonexistent", "workflow_version": "v1", "inputs": {}},
    )
    assert resp.status_code == 400


def test_job_cancel(client: TestClient) -> None:
    resp = client.post(
        "/v1/generations",
        headers={"Idempotency-Key": "cancel-key"},
        json={"workflow_name": "txt2img_basic", "workflow_version": "v1", "inputs": {"prompt": "x"}},
    )
    job_id = resp.json()["id"]
    cancel_resp = client.post(f"/v1/jobs/{job_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["state"] == "cancelled"


def test_job_not_found_for_other_user(client: TestClient) -> None:
    resp = client.get(f"/v1/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404
