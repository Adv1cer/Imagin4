"""Shared job-admission logic: workflow validation, idempotency-key handling, and
QueuedJob construction. Extracted from app/api/v1/generations.py so the agentic chat
router (app/api/v1/chat_router.py) admits jobs through the exact same path -- one
validated way into the queue, not two slightly-different copies that could drift.

Deliberately framework-free (no FastAPI/HTTPException here) so it stays usable from
non-HTTP callers too; raises domain-level exceptions that each caller translates into
whatever error shape is appropriate for it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.adapters.queue import JobQueue, QueuedJob
from app.core.config import get_settings
from app.domain.jobs.comfy_overrides import (
    InvalidComfyOverrideError,
    build_allowlists,
    validate_overrides,
)
from app.domain.jobs.comfy_profiles import (
    UnknownModelProfileError,
    build_profiles,
    resolve_profile_key,
)
from app.domain.jobs.idempotency import (
    IdempotencyOutcome,
    canonical_payload_hash,
    check_idempotency,
)
from app.domain.jobs.workflow_registry import (
    UnknownWorkflowError,
    kinds_for_backend,
    resolve_workflow,
)

__all__ = [
    "AdmissionResult",
    "CapacityExceededError",
    "IdempotencyConflictError",
    "InvalidComfyOverrideError",
    "PreviewNotSupportedError",
    "UnknownModelProfileError",
    "admit_generation_job",
    "estimate_wait_seconds",
    "UnknownWorkflowError",
]


def _capacity_for_backend(backend: str, settings) -> int:
    """Which Settings field bounds this backend's CONCURRENT claim capacity -- see
    default_comfy_active_slots/default_gemini_active_slots' own docstrings in
    app/core/config.py for why they're separate (GPU-bound vs network-bound)."""
    return (
        settings.default_comfy_active_slots
        if backend == "comfyui"
        else settings.default_gemini_active_slots
    )


def estimate_wait_seconds(position: int, backend: str, settings) -> float | None:
    """Rough ETA (2026-08-20) for a job sitting at 0-indexed `position` in its
    backend's queue: assumes `capacity` same-backend jobs complete roughly every
    `settings.estimated_job_duration_s` seconds (see that setting's docstring in
    app/core/config.py for exactly how rough this is -- no real observed-duration
    tracking exists anywhere in this codebase yet, this is a placeholder better than
    silence, NOT a scheduling guarantee). None if the backend's capacity is configured
    to 0 (nothing will ever claim, so any ETA would be nonsense, not just imprecise).
    """
    capacity = _capacity_for_backend(backend, settings)
    if capacity <= 0:
        return None
    return (position // capacity) * settings.estimated_job_duration_s


class IdempotencyConflictError(ValueError):
    """The same (user, idempotency_key, kind) was already used with a different
    payload -- a client bug/conflict, not a transient failure."""


class PreviewNotSupportedError(ValueError):
    """Raised when `inputs["preview"]` is truthy but either (a) the resolved workflow's
    backend isn't "comfyui" (steps has no meaning for a Gemini call -- see
    Settings.preview_steps' docstring for the full preview/final scope note), or (b)
    Settings.preview_enabled is False on this deployment. A request bug (retrying the
    identical payload will never succeed), so callers map this to HTTP 400 -- same
    treatment as UnknownModelProfileError/InvalidComfyOverrideError."""


class CapacityExceededError(RuntimeError):
    """Raised when admitting this job would exceed a configured in-flight budget --
    either this user's own backlog (Settings.max_queued_jobs_per_user) or the whole
    system's (Settings.global_queue_cap). See admit_generation_job's docstring for
    exactly what's counted.

    Deliberately its own exception, NOT lumped in with the other admission errors
    above: those (UnknownWorkflowError etc.) mean the REQUEST is wrong and retrying
    the identical payload will never succeed. This means the SYSTEM is busy right
    now -- an expected, load-dependent condition a well-behaved client should retry
    after `retry_after_s`, not treat as a bug. Callers should map this to 429 Too
    Many Requests with a `Retry-After` header, never 400/500 (see
    app/api/v1/generations.py and app/api/v1/chat_router.py's admit_generation_job
    call sites for the existing except-clause convention this slots into).

    2026-08-20 (Chet, DGX Spark): added after a 100-VU-burst-style load test made it
    obvious that Settings.max_queued_jobs_per_user/global_queue_cap existed in config
    but were never actually checked anywhere (see app/core/rate_limit.py's old module
    docstring) -- one user's large batch of jobs could occupy the FIFO-ish claim order
    (Scheduler._claim's `ORDER BY effective_priority DESC, queued_at ASC`) ahead of
    every other user's jobs indefinitely, since nothing capped how many of their own
    jobs a single user could have waiting at once. This check only caps the QUEUED
    backlog -- Settings.max_active_jobs_per_user (how many of one user's jobs may be
    simultaneously dispatched/running) is a separate, CLAIM-time concern, enforced as of
    the same day in Scheduler._claim_for_backend -> JobQueue.claim_next_with_lease's
    `max_active_per_user` param, not here -- see that param's docstring (and
    app/adapters/queue/postgres.py's `_claim` for the real-Postgres-tested SQL/advisory-
    lock implementation, since that method's claim query already has a documented
    history of subtle concurrency bugs and a naive addition here would have risked
    repeating one)."""

    def __init__(self, reason: str, retry_after_s: int = 15) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class AdmissionResult:
    id: str
    state: str
    kind: str
    replayed: bool
    # 2026-08-20: 0-indexed count of same-backend jobs ahead of this one at the moment
    # of admission, and a rough ETA derived from it -- see estimate_wait_seconds' own
    # docstring for exactly what it does and doesn't promise. Both None for a REPLAY
    # (the existing job's real position may be anywhere -- running, nearly done, or
    # itself still queued -- and recomputing it accurately isn't worth the extra query
    # for what's fundamentally a duplicate-request response) or when the active
    # JobQueue doesn't implement count_backlog_for_kinds (same fail-open posture as the
    # capacity checks above).
    queue_position: int | None = None
    estimated_wait_seconds: float | None = None
    # 2026-08-20, see PreviewNotSupportedError/Settings.preview_steps' docstrings for
    # the full preview/final two-stage flow. True when this job's `input_payload` was
    # admitted with `inputs.preview` truthy (steps force-lowered to
    # Settings.preview_steps) -- lets a client render a "this is a fast preview, not
    # final quality" affordance without having to inspect input_payload itself (which
    # GenerationOut/JobOut don't otherwise expose). Reflects whatever the EXISTING
    # job's payload says on a REPLAY, unlike queue_position/estimated_wait_seconds
    # above -- this is cheap to read off input_payload either way, no extra query.
    is_preview: bool = False


async def admit_generation_job(
    queue: JobQueue,
    user_id: uuid.UUID,
    workflow_name: str,
    workflow_version: str,
    inputs: dict,
    idempotency_key: str,
    conversation_id: uuid.UUID | None = None,
) -> AdmissionResult:
    """Validates `(workflow_name, workflow_version)` against the server-side allowlist
    (app/domain/jobs/workflow_registry.py -- callers never get to pass an arbitrary
    workflow through), applies idempotency-key replay/conflict semantics identical to
    POST /v1/generations, and enqueues a new QueuedJob if this is genuinely new.

    Raises `UnknownWorkflowError` for an unrecognized workflow,
    `UnknownModelProfileError` for an unrecognized `inputs["model_profile"]`,
    `InvalidComfyOverrideError` for an invalid/out-of-range/not-allowlisted
    `inputs["model_overrides"]` entry (both ComfyUI-only, see
    app/domain/jobs/comfy_profiles.py and app/domain/jobs/comfy_overrides.py),
    `PreviewNotSupportedError` for a truthy `inputs["preview"]` on a non-ComfyUI
    workflow or when Settings.preview_enabled is off (see that exception's docstring),
    `IdempotencyConflictError` for a key reused with a different payload, and
    `CapacityExceededError` if admitting this job would exceed this user's own
    `Settings.max_queued_jobs_per_user` backlog or the system-wide
    `Settings.global_queue_cap` (see that exception's docstring for exactly what's
    counted and why); callers map those to their own appropriate HTTP status
    (400 / 400 / 400 / 400 / 409 / 429 in the existing endpoints). A REPLAY never
    raises CapacityExceededError -- it doesn't create a new job, so it can't make
    anyone's backlog worse; only a genuinely NEW admission is checked against the caps.

    When `conversation_id` is provided (agent/smart-message paths), the per-user queued
    cap is applied to that conversation's backlog instead of the owning user_id -- so a
    shared agentflow API key does not treat every campus end user as one backlog bucket
    (2026-08-20).
    """
    workflow = resolve_workflow(workflow_name, workflow_version)  # raises UnknownWorkflowError

    is_preview = bool(inputs.get("preview"))

    if workflow.backend == "comfyui":
        # Poster/infographic (backend == "gemini") never reaches this -- model_profile
        # and model_overrides are ComfyUI-only concepts, there's nothing to pick a
        # model between when the job doesn't touch ComfyUI at all. Resolves +
        # normalizes so every downstream consumer (LiveComfyUIClient, the persisted
        # input_payload used for idempotency hashing/replay) sees concrete, already-
        # validated values, never a missing/blank/unchecked one -- same reasoning as
        # resolve_profile_key's own docstring.
        settings = get_settings()
        profiles = build_profiles(settings)
        resolved_key = resolve_profile_key(
            inputs.get("model_profile"), profiles
        )  # raises UnknownModelProfileError
        validated_overrides = validate_overrides(
            inputs.get("model_overrides"), build_allowlists(settings)
        )  # raises InvalidComfyOverrideError

        final_overrides = validated_overrides
        preview_original_overrides = None
        if is_preview:
            if not settings.preview_enabled:
                raise PreviewNotSupportedError("preview generation is disabled on this deployment")
            # Preview always wins on steps -- force-fast regardless of what the caller
            # asked for. The caller's own (already-validated) overrides are preserved
            # separately under `preview_original_overrides` so POST /v1/generations/
            # {id}/finalize (app/api/v1/generations.py) can restore real quality
            # settings later instead of the finalize job silently inheriting
            # preview_steps forever.
            preview_original_overrides = validated_overrides
            final_overrides = {**validated_overrides, "steps": settings.preview_steps}

        inputs = {
            **inputs,
            "model_profile": resolved_key,
            "model_overrides": final_overrides,
            "is_preview": is_preview,
        }
        if preview_original_overrides is not None:
            inputs["preview_original_overrides"] = preview_original_overrides
    elif is_preview:
        # preview=true on a non-ComfyUI-backend workflow (e.g. poster_infographic ->
        # gemini) -- "steps" has no meaning there, and silently ignoring the flag would
        # give a false "you got a fast preview" impression. Fail loud instead.
        raise PreviewNotSupportedError(
            f"preview generation is only supported for ComfyUI-backed workflows, "
            f"not {workflow.backend!r}"
        )

    kind = workflow_name
    existing = None
    if hasattr(queue, "find_by_idempotency_key"):
        existing = await queue.find_by_idempotency_key(user_id, idempotency_key, kind)

    existing_hash = canonical_payload_hash(existing.input_payload) if existing is not None else None
    check = check_idempotency(
        existing_job_id=str(existing.id) if existing else None,
        existing_payload_hash=existing_hash,
        new_payload=inputs,
    )

    if check.outcome == IdempotencyOutcome.CONFLICT:
        raise IdempotencyConflictError("Idempotency-Key already used with a different payload")
    if check.outcome == IdempotencyOutcome.REPLAY and existing is not None:
        return AdmissionResult(
            id=str(existing.id),
            state=existing.state,
            kind=existing.kind,
            replayed=True,
            is_preview=bool((existing.input_payload or {}).get("is_preview", False)),
        )

    # Per-user and global backlog caps -- see CapacityExceededError's docstring for the
    # 2026-08-20 incident this closes. `hasattr` guards match this module's existing
    # style just above (find_by_idempotency_key) and app/services/scheduler.py's own
    # convention: any JobQueue that doesn't implement these (e.g. a minimal test fake)
    # is simply not capacity-checked, same fail-open posture as everything else here.
    # Settings.max_active_jobs_per_user is intentionally NOT checked here -- it's
    # enforced at CLAIM time instead (Scheduler._claim_for_backend), see
    # CapacityExceededError's docstring for why.
    settings = get_settings()
    if settings.global_queue_cap > 0 and hasattr(queue, "count_global_backlog"):
        global_backlog = await queue.count_global_backlog()
        if global_backlog >= settings.global_queue_cap:
            raise CapacityExceededError(
                "the generation queue is full system-wide, please retry shortly",
                retry_after_s=30,
            )
    if settings.max_queued_jobs_per_user > 0:
        if conversation_id is not None and hasattr(queue, "count_conversation_backlog"):
            conv_backlog = await queue.count_conversation_backlog(conversation_id)
            if conv_backlog >= settings.max_queued_jobs_per_user:
                raise CapacityExceededError(
                    f"this conversation already has {conv_backlog} job(s) waiting "
                    f"(limit {settings.max_queued_jobs_per_user}) -- "
                    "wait for one to finish before submitting more",
                    retry_after_s=15,
                )
        elif hasattr(queue, "count_user_backlog"):
            user_backlog = await queue.count_user_backlog(user_id)
            if user_backlog >= settings.max_queued_jobs_per_user:
                raise CapacityExceededError(
                    f"you already have {user_backlog} job(s) waiting "
                    f"(limit {settings.max_queued_jobs_per_user}) -- "
                    "wait for one to finish before submitting more",
                    retry_after_s=15,
                )

    # queue_position/estimated_wait_seconds (2026-08-20, see AdmissionResult's
    # docstring and estimate_wait_seconds above): computed BEFORE enqueue -- every job
    # here is created with priority=0 (pure FIFO within a backend lane), so a brand-new
    # job always lands at the very back, meaning "how many same-backend jobs are
    # already queued/retry_wait right now" IS this job's position once it's inserted.
    # `hasattr` guard matches this module's existing style (find_by_idempotency_key,
    # count_user_backlog above) -- a JobQueue that doesn't implement this just doesn't
    # get ETA fields, same fail-open posture as everything else here.
    queue_position: int | None = None
    estimated_wait: float | None = None
    if hasattr(queue, "count_backlog_for_kinds"):
        queue_position = await queue.count_backlog_for_kinds(kinds_for_backend(workflow.backend))
        estimated_wait = estimate_wait_seconds(queue_position, workflow.backend, settings)

    job = QueuedJob(
        id=uuid.uuid4(),
        user_id=user_id,
        kind=kind,
        state="queued",
        priority=0,
        effective_priority=0.0,
        input_payload=inputs,
        idempotency_key=idempotency_key,
        queued_at=datetime.now(timezone.utc),
        conversation_id=conversation_id,
    )
    await queue.enqueue(job)
    return AdmissionResult(
        id=str(job.id),
        state=job.state,
        kind=job.kind,
        replayed=False,
        queue_position=queue_position,
        estimated_wait_seconds=estimated_wait,
        is_preview=is_preview,
    )
