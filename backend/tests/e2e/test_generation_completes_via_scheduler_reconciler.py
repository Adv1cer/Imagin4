"""Regression test for the app.main lifespan wiring: previously `app.services.scheduler`
and `app.services.reconciler` each constructed their own private InMemoryJobQueue, so a
job submitted through the API's app.state.job_queue was never claimed or finalized by
either loop even when both were "running" (see app/main.py lifespan docstring). This
test drives the same Scheduler/Reconciler classes against the *same* queue instance the
API uses, the way app.main.lifespan now does, and asserts a submitted job actually
reaches a terminal state end to end."""

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
from app.services.reconciler import Reconciler
from app.services.scheduler import Scheduler


def build_app() -> tuple[FastAPI, InMemoryJobQueue, MockComfyUIClient]:
    job_queue = InMemoryJobQueue()
    comfy_client = MockComfyUIClient()  # polls_to_complete=0 -> succeeds on first poll

    app = FastAPI()
    app.state.job_queue = job_queue
    app.state.storage = InMemoryObjectStorage()
    app.state.comfy_client = comfy_client
    app.state.session_factory = None

    app.include_router(health.router, prefix="/v1")
    app.include_router(generations.router, prefix="/v1")
    app.include_router(jobs.router, prefix="/v1")

    fake_user = User(
        id=uuid.uuid4(), email="student@example.edu", display_name="Test Student", status="active"
    )

    async def _fake_current_user() -> User:
        return fake_user

    app.dependency_overrides[get_current_user] = _fake_current_user
    return app, job_queue, comfy_client


@pytest.mark.asyncio
async def test_job_reaches_succeeded_when_scheduler_and_reconciler_share_the_queue() -> None:
    app, job_queue, comfy_client = build_app()
    client = TestClient(app)

    resp = client.post(
        "/v1/generations",
        headers={"Idempotency-Key": "sched-recon-key"},
        json={
            "workflow_name": "txt2img_basic",
            "workflow_version": "v1",
            "inputs": {"prompt": "a friendly robot"},
        },
    )
    assert resp.status_code == 202
    job_id = resp.json()["id"]
    assert resp.json()["state"] == "queued"

    scheduler = Scheduler(job_queue=job_queue, comfy_client=comfy_client)
    reconciler = Reconciler(job_queue=job_queue, comfy_client=comfy_client)

    # One scheduler tick claims the job and kicks off dispatch (mark_running + submit to
    # ComfyUI) as a background task -- await it explicitly rather than assuming it has
    # finished by the time `_tick()` returns.
    import asyncio

    await scheduler._tick()
    if scheduler._inflight:
        await asyncio.gather(*list(scheduler._inflight))
    dispatched = client.get(f"/v1/jobs/{job_id}").json()
    assert dispatched["state"] == "running"

    # One reconciler pass resolves the now-known prompt_id via ComfyUI and finalizes it.
    await reconciler.run_once()
    finished = client.get(f"/v1/jobs/{job_id}").json()
    assert finished["state"] == "succeeded"
    assert finished["result"] is not None
    assert finished["result"]["outputs"][0]["object_key"].startswith("generated/")
