"""Regression tests for the scheduler/reconciler race condition root-caused from Chet's
live production logs (2026-08): a poster_infographic job dispatched FOUR separate Gemini
image-generation attempts (should have been at most max_attempts=3), including one
dispatch that happened AFTER the job had already reached a terminal `succeeded` state.
See app/adapters/queue/__init__.py's module docstring for the full root-cause writeup.

The bug had two parts, tested separately here:
  1. `job.prompt_id` from a previous (already-resolved) attempt wasn't cleared when a
     new attempt started dispatching, so a reconciler pass landing mid-dispatch could
     resolve the job using the STALE prior attempt's outcome.
  2. mark_succeeded/mark_failed/mark_retry_wait had no guard against a job that had
     already reached a terminal state, so that stale resolution could downgrade/
     overwrite an already-succeeded (or already-failed) job.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.adapters.comfyui import ComfyStatus
from app.adapters.queue import InMemoryJobQueue, QueuedJob
from app.services.reconciler import Reconciler


def _job(**overrides) -> QueuedJob:
    defaults = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "kind": "poster_infographic",
        "state": "queued",
        "priority": 0,
        "effective_priority": 0.0,
        "input_payload": {"prompt": "a poster"},
        "idempotency_key": str(uuid.uuid4()),
        "queued_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return QueuedJob(**defaults)


@pytest.mark.asyncio
async def test_mark_running_clears_stale_prompt_id_from_a_previous_attempt():
    queue = InMemoryJobQueue()
    job = _job(state="dispatched", current_attempt=1)
    job.prompt_id = "attempt-1-prompt-id"  # left over from a previous, already-resolved attempt
    await queue.enqueue(job)

    await queue.mark_running(job.id)

    updated = await queue.get(job.id)
    assert updated.state == "running"
    assert updated.prompt_id is None


@pytest.mark.asyncio
async def test_mark_succeeded_is_a_noop_once_job_already_failed():
    queue = InMemoryJobQueue()
    job = _job(state="failed", error_code="comfy_transient", error_detail="gemini_error:TimeoutError")
    await queue.enqueue(job)

    # A stale/late reconciliation pass for a superseded attempt must not resurrect a
    # terminally-failed job back into "succeeded".
    await queue.mark_succeeded(job.id, {"outputs": [{"object_key": "generated/x.png"}]})

    updated = await queue.get(job.id)
    assert updated.state == "failed"
    assert updated.result is None


@pytest.mark.asyncio
async def test_mark_failed_is_a_noop_once_job_already_succeeded():
    queue = InMemoryJobQueue()
    job = _job(state="succeeded", result={"outputs": [{"object_key": "generated/real.png"}]})
    await queue.enqueue(job)

    # The exact bug from Chet's logs: a stale prior-attempt outcome must not overwrite
    # a job that a later, legitimate attempt already completed successfully.
    await queue.mark_failed(job.id, "comfy_transient", "gemini_error:TimeoutError")

    updated = await queue.get(job.id)
    assert updated.state == "succeeded"
    assert updated.result == {"outputs": [{"object_key": "generated/real.png"}]}


@pytest.mark.asyncio
async def test_mark_retry_wait_is_a_noop_once_job_already_terminal_and_does_not_bump_attempt():
    queue = InMemoryJobQueue()
    job = _job(state="cancelled", current_attempt=2)
    await queue.enqueue(job)

    await queue.mark_retry_wait(job.id, "comfy_transient", "gemini_error:TimeoutError")

    updated = await queue.get(job.id)
    assert updated.state == "cancelled"
    # No-op means current_attempt must not have been bumped either -- a stale retry
    # decision shouldn't corrupt the attempt counter of a job that's already done.
    assert updated.current_attempt == 2


@pytest.mark.asyncio
async def test_reconciler_ignores_job_with_no_prompt_id_even_if_a_stale_result_exists():
    """Simulates the actual race: a new attempt has started (job.state == "running",
    prompt_id cleared by mark_running) while comfy_client.submit() for that new attempt
    is still in flight. A reconciler pass landing in this exact window must NOT act on
    the job at all (no known prompt_id yet) -- it must not, for example, notice a
    *different*, stale prompt_id's cached failed outcome sitting in the comfy client."""

    class _StaleResultComfyClient:
        """Stands in for GeminiImageComfyUIClient: has a cached FAILED result for an old
        prompt_id, but the job itself no longer references that prompt_id."""

        async def get_status(self, prompt_id: str) -> ComfyStatus:
            if prompt_id == "attempt-1-stale-prompt-id":
                return ComfyStatus(prompt_id=prompt_id, state="failed", error="gemini_error:TimeoutError")
            raise AssertionError(f"reconciler should not have looked up prompt_id={prompt_id}")

    queue = InMemoryJobQueue()
    job = _job(state="running", current_attempt=1)
    job.lease_owner = "scheduler-x"
    job.lease_expires_at = None  # no expiry set yet -- attempt is actively in flight
    await queue.enqueue(job)
    await queue.mark_running(job.id)  # clears prompt_id, as the real dispatch path does

    reconciler = Reconciler(job_queue=queue, comfy_client=_StaleResultComfyClient(), poll_interval_s=0.01)
    examined = await reconciler.run_once()

    assert examined == 1  # job is in list_active() (state == "running")...
    updated = await queue.get(job.id)
    # ...but untouched: still running, no premature failure from a stale prompt_id.
    assert updated.state == "running"
