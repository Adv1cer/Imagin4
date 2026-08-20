"""Unit tests for the 2026-08-20 preview/final two-stage generation feature:
- app/domain/jobs/admission.py: `inputs.preview` handling, PreviewNotSupportedError,
  AdmissionResult.is_preview, preview_original_overrides bookkeeping.
- app/api/v1/generations.py: POST /v1/generations/{id}/finalize.

Scope reminder (see Settings.preview_steps' docstring): this only fast-forwards the
`steps` knob down for a ComfyUI-backed job -- no resolution/seed control exists in this
codebase to preview-and-match on those axes."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Response

from app.adapters.queue import InMemoryJobQueue
from app.api.v1.generations import GenerationCreate, create_generation, finalize_generation
from app.core.config import Settings
from app.db.models import User
from app.domain.jobs.admission import PreviewNotSupportedError, admit_generation_job


def _user():
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    return u


# -- admit_generation_job: core preview semantics ---------------------------------


@pytest.mark.asyncio
async def test_preview_forces_steps_down_and_marks_is_preview():
    queue = InMemoryJobQueue()
    result = await admit_generation_job(
        queue=queue,
        user_id=uuid.uuid4(),
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "a cat", "preview": True, "model_overrides": {"steps": 30}},
        idempotency_key="k1",
    )
    assert result.is_preview is True
    job = await queue.get(uuid.UUID(result.id))
    assert job.input_payload["is_preview"] is True
    settings = Settings()
    assert job.input_payload["model_overrides"]["steps"] == settings.preview_steps
    # Caller's real request (steps=30) preserved for finalize, not lost.
    assert job.input_payload["preview_original_overrides"]["steps"] == 30


@pytest.mark.asyncio
async def test_non_preview_job_has_is_preview_false():
    queue = InMemoryJobQueue()
    result = await admit_generation_job(
        queue=queue,
        user_id=uuid.uuid4(),
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "a cat"},
        idempotency_key="k1",
    )
    assert result.is_preview is False
    job = await queue.get(uuid.UUID(result.id))
    assert job.input_payload["is_preview"] is False
    assert "preview_original_overrides" not in job.input_payload


@pytest.mark.asyncio
async def test_preview_on_gemini_backend_workflow_is_rejected():
    queue = InMemoryJobQueue()
    with pytest.raises(PreviewNotSupportedError):
        await admit_generation_job(
            queue=queue,
            user_id=uuid.uuid4(),
            workflow_name="poster_infographic",
            workflow_version="v1",
            inputs={"prompt": "a poster", "preview": True},
            idempotency_key="k1",
        )


@pytest.mark.asyncio
async def test_preview_disabled_via_settings_is_rejected(monkeypatch):
    import app.domain.jobs.admission as admission_module

    monkeypatch.setattr(
        admission_module, "get_settings", lambda: Settings(preview_enabled=False)
    )
    queue = InMemoryJobQueue()
    with pytest.raises(PreviewNotSupportedError):
        await admit_generation_job(
            queue=queue,
            user_id=uuid.uuid4(),
            workflow_name="image_basic",
            workflow_version="v1",
            inputs={"prompt": "a cat", "preview": True},
            idempotency_key="k1",
        )


@pytest.mark.asyncio
async def test_replay_reflects_existing_jobs_is_preview():
    queue = InMemoryJobQueue()
    user_id = uuid.uuid4()
    first = await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "x", "preview": True},
        idempotency_key="same-key",
    )
    assert first.is_preview is True
    replay = await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "x", "preview": True},
        idempotency_key="same-key",
    )
    assert replay.replayed is True
    assert replay.is_preview is True


# -- POST /v1/generations/{id}/finalize --------------------------------------------


@pytest.mark.asyncio
async def test_finalize_requires_preview_job_to_exist_and_be_owned():
    queue = InMemoryJobQueue()
    user = _user()
    with pytest.raises(HTTPException) as exc_info:
        await finalize_generation(
            str(uuid.uuid4()), Response(), idempotency_key="k", queue=queue, user=user
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_finalize_rejects_non_preview_job():
    queue = InMemoryJobQueue()
    user = _user()
    payload = GenerationCreate(
        workflow_name="image_basic", workflow_version="v1", inputs={"prompt": "x"}
    )
    created = await create_generation(payload, Response(), idempotency_key="k1", queue=queue, user=user)

    with pytest.raises(HTTPException) as exc_info:
        await finalize_generation(created.id, Response(), idempotency_key="k2", queue=queue, user=user)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_finalize_rejects_unfinished_preview_job():
    queue = InMemoryJobQueue()
    user = _user()
    payload = GenerationCreate(
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "x", "preview": True},
    )
    created = await create_generation(payload, Response(), idempotency_key="k1", queue=queue, user=user)
    assert created.state == "queued"  # not succeeded yet

    with pytest.raises(HTTPException) as exc_info:
        await finalize_generation(created.id, Response(), idempotency_key="k2", queue=queue, user=user)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_finalize_creates_full_quality_job_restoring_original_overrides():
    queue = InMemoryJobQueue()
    user = _user()
    payload = GenerationCreate(
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "a cat", "preview": True, "model_overrides": {"steps": 25}},
    )
    created = await create_generation(payload, Response(), idempotency_key="k1", queue=queue, user=user)

    preview_job = await queue.get(uuid.UUID(created.id))
    settings = Settings()
    assert preview_job.input_payload["model_overrides"]["steps"] == settings.preview_steps

    await queue.mark_running(preview_job.id)
    await queue.mark_succeeded(preview_job.id, {"outputs": [{"object_key": "x"}]})

    final = await finalize_generation(
        created.id, Response(), idempotency_key="k2", queue=queue, user=user
    )
    assert final.is_preview is False
    assert final.id != created.id

    final_job = await queue.get(uuid.UUID(final.id))
    assert final_job.input_payload["is_preview"] is False
    # The caller's REAL steps request (25), not the forced preview_steps, survives.
    assert final_job.input_payload["model_overrides"]["steps"] == 25
    assert final_job.input_payload["preview_of"] == str(preview_job.id)
    assert "preview" not in final_job.input_payload


@pytest.mark.asyncio
async def test_finalize_cannot_be_used_by_a_different_user():
    queue = InMemoryJobQueue()
    owner = _user()
    stranger = _user()
    payload = GenerationCreate(
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "x", "preview": True},
    )
    created = await create_generation(payload, Response(), idempotency_key="k1", queue=queue, user=owner)
    job = await queue.get(uuid.UUID(created.id))
    await queue.mark_running(job.id)
    await queue.mark_succeeded(job.id, {"outputs": []})

    with pytest.raises(HTTPException) as exc_info:
        await finalize_generation(created.id, Response(), idempotency_key="k2", queue=queue, user=stranger)
    assert exc_info.value.status_code == 404
