"""Unit tests for app/domain/jobs/admission.py -- the job-admission path shared by
POST /v1/generations (app/api/v1/generations.py) and the agentic chat router's
GENERAL_IMAGE / POSTER-INFOGRAPHIC-confirm paths (app/api/v1/chat_router.py).

Covers: unknown/unapproved workflow names are rejected (nothing resembling an arbitrary
ComfyUI graph or unregistered workflow id can reach the queue), idempotency replay
returns the same job, and a reused key with a different payload conflicts."""

from __future__ import annotations

import uuid

import pytest

from app.adapters.queue import InMemoryJobQueue
from app.domain.jobs.admission import (
    IdempotencyConflictError,
    UnknownWorkflowError,
    admit_generation_job,
)


@pytest.mark.asyncio
async def test_unknown_workflow_name_is_rejected():
    queue = InMemoryJobQueue()
    with pytest.raises(UnknownWorkflowError):
        await admit_generation_job(
            queue=queue,
            user_id=uuid.uuid4(),
            workflow_name="arbitrary_client_supplied_graph",
            workflow_version="v1",
            inputs={"prompt": "x"},
            idempotency_key="key-1",
        )


@pytest.mark.asyncio
async def test_unapproved_workflow_version_is_rejected():
    queue = InMemoryJobQueue()
    with pytest.raises(UnknownWorkflowError):
        await admit_generation_job(
            queue=queue,
            user_id=uuid.uuid4(),
            workflow_name="image_basic",
            workflow_version="v99-unapproved",
            inputs={"prompt": "x"},
            idempotency_key="key-1",
        )


@pytest.mark.asyncio
async def test_general_image_admits_via_the_comfyui_backed_workflow():
    """GENERAL_IMAGE must only ever go through "image_basic", whose registry entry is
    backend="comfyui" (see test_composite_comfyui_routing.py) -- never the paid
    "poster_infographic" workflow."""
    queue = InMemoryJobQueue()
    result = await admit_generation_job(
        queue=queue,
        user_id=uuid.uuid4(),
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "a cat"},
        idempotency_key="key-1",
    )
    assert result.kind == "image_basic"
    assert result.replayed is False


@pytest.mark.asyncio
async def test_poster_infographic_admits_via_the_gemini_backed_workflow():
    queue = InMemoryJobQueue()
    result = await admit_generation_job(
        queue=queue,
        user_id=uuid.uuid4(),
        workflow_name="poster_infographic",
        workflow_version="v1",
        inputs={"prompt": "a poster"},
        idempotency_key="key-1",
    )
    assert result.kind == "poster_infographic"


@pytest.mark.asyncio
async def test_same_idempotency_key_and_payload_replays_not_duplicates():
    queue = InMemoryJobQueue()
    user_id = uuid.uuid4()
    first = await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "same"},
        idempotency_key="dup-key",
    )
    second = await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "same"},
        idempotency_key="dup-key",
    )
    assert second.replayed is True
    assert second.id == first.id


@pytest.mark.asyncio
async def test_same_idempotency_key_different_payload_conflicts():
    """This is what makes POST /v1/pending-actions/{id}/confirm safe against a crash
    between "marked confirmed" and "job enqueued": a retry uses the SAME derived key
    (f"pending-action-{id}") and the SAME stored parameters, so it always replays --
    this test proves the inverse (different payload) correctly raises instead of
    silently admitting a second, different job under the same key."""
    queue = InMemoryJobQueue()
    user_id = uuid.uuid4()
    await admit_generation_job(
        queue=queue,
        user_id=user_id,
        workflow_name="image_basic",
        workflow_version="v1",
        inputs={"prompt": "original"},
        idempotency_key="dup-key",
    )
    with pytest.raises(IdempotencyConflictError):
        await admit_generation_job(
            queue=queue,
            user_id=user_id,
            workflow_name="image_basic",
            workflow_version="v1",
            inputs={"prompt": "different"},
            idempotency_key="dup-key",
        )
