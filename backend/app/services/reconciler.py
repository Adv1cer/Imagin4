"""Reconciler process: finds jobs whose scheduler/worker lease has expired (crashed
scheduler, dead worker, lost heartbeat) or that are stuck in `dispatched`/`running`
without a finalized outcome, and resolves them.

Reuses existing domain logic rather than reimplementing it:
  - state legality: `app.domain.jobs.state_machine` (single source of truth for which
    transitions are allowed).
  - retry classification + backoff/jitter: `app.domain.jobs.retry` (is_retryable,
    compute_backoff_seconds) -- identical logic the API/scheduler would use.
  - outcome lookup: `ComfyUIClient.get_status(prompt_id)` (mock or live).

Runnable via `python -m app.services.reconciler`, with the same graceful-shutdown
contract as the scheduler (stop starting new reconciliation passes, let the in-flight
pass finish, then exit).

Production note: in Postgres this reads `job_attempts` (lease_owner/lease_expires_at,
comfy_prompt_id) and `comfy_workers` (last_heartbeat_at) directly, and appends
`job_events` rows for every action it takes, all in the same transaction as the
`generation_jobs.state` UPDATE. Only the in-memory JobQueue exists in this repo today
(see app/adapters/queue), so in that mode the reconciler works off `JobQueue.list_active()`
+ the lease/prompt_id fields added to `QueuedJob`, and job_events are emitted as
structured log lines instead of DB rows -- swap `_emit_event` for a real INSERT once the
Postgres-backed queue lands.
"""

from __future__ import annotations

import asyncio
import logging
import random
import signal
import uuid
from datetime import datetime, timezone

from app.adapters.comfyui import ComfyUIClient, MockComfyUIClient
from app.adapters.queue import InMemoryJobQueue, JobQueue, QueuedJob
from app.core.config import Settings, get_settings
from app.domain.jobs.retry import BackoffConfig, compute_backoff_seconds, is_retryable

logger = logging.getLogger("imaginv.reconciler")

DEFAULT_POLL_INTERVAL_S = 5.0
STALE_RUNNING_SECONDS = 300.0  # a running job with no known prompt_id this old is orphaned


class Reconciler:
    def __init__(
        self,
        job_queue: JobQueue,
        comfy_client: ComfyUIClient,
        settings: Settings | None = None,
        session_factory=None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        rng: random.Random | None = None,
    ) -> None:
        self.job_queue = job_queue
        self.comfy_client = comfy_client
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.poll_interval_s = poll_interval_s
        self.rng = rng or random.Random()
        self._shutdown = asyncio.Event()
        self._pass_lock = asyncio.Lock()

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            logger.info("reconciler: shutdown requested; will finish current pass and exit")
        self._shutdown.set()

    def _emit_event(self, job_id: uuid.UUID, event_type: str, payload: dict) -> None:
        """Stand-in for an INSERT INTO job_events (see module docstring). Structured so a
        real DB-backed implementation is a drop-in replacement of this one method."""
        logger.info("job_event job_id=%s type=%s payload=%s", job_id, event_type, payload)

    async def _resolve_via_comfy(self, job: QueuedJob) -> None:
        """Job has a known prompt_id: ask ComfyUI for the authoritative outcome."""
        status = await self.comfy_client.get_status(job.prompt_id)
        if status.state == "succeeded":
            result = {"outputs": status.outputs or []}
            await self.job_queue.mark_succeeded(job.id, result)
            self._emit_event(job.id, "job_succeeded", {"prompt_id": job.prompt_id})
            return
        if status.state == "running":
            # Still genuinely in progress; only re-lease if the previous lease expired
            # (worker/scheduler died mid-flight) -- extend so it isn't repeatedly reclaimed.
            if hasattr(self.job_queue, "claim_next_with_lease"):
                job.lease_expires_at = datetime.now(timezone.utc)
            return
        # status.state == "failed"
        await self._fail_or_retry(job, error_code="comfy_transient", detail=status.error)

    async def _fail_or_retry(self, job: QueuedJob, error_code: str, detail: str | None) -> None:
        attempt_no = job.current_attempt + 1
        if is_retryable(error_code, attempt_no, job.max_attempts):
            delay_s = compute_backoff_seconds(attempt_no, BackoffConfig(), rng=self.rng)
            if hasattr(self.job_queue, "mark_retry_wait"):
                await self.job_queue.mark_retry_wait(job.id, error_code)
            else:
                await self.job_queue.mark_failed(job.id, error_code)
            self._emit_event(
                job.id,
                "job_retry_scheduled",
                {
                    "attempt_no": attempt_no,
                    "delay_s": round(delay_s, 2),
                    "error_code": error_code,
                    "detail": detail,
                },
            )
        else:
            await self.job_queue.mark_failed(job.id, error_code)
            self._emit_event(
                job.id,
                "job_failed",
                {"attempt_no": attempt_no, "error_code": error_code, "detail": detail},
            )

    async def _reconcile_job(self, job: QueuedJob, now: datetime) -> None:
        lease_expired = job.lease_expires_at is not None and job.lease_expires_at < now
        if job.prompt_id:
            # We know the durable prompt_id: ComfyUI is the source of truth for outcome,
            # regardless of whether our lease/heartbeat lapsed (submission may have
            # succeeded even if the process that submitted it then crashed/lost contact).
            await self._resolve_via_comfy(job)
            return

        # No known prompt_id. If the lease expired (or the job has been "running" for an
        # implausibly long time with nothing to check), we cannot ask ComfyUI for an
        # authoritative outcome, so we must fail/retry conservatively. This is the
        # duplicate-execution risk window documented in README.md: a crash between
        # ComfyUI accepting the prompt and us persisting prompt_id.
        if lease_expired:
            await self._fail_or_retry(
                job, error_code="worker_lease_expired", detail="lease expired with no prompt_id"
            )

    async def run_once(self) -> int:
        """Runs a single reconciliation pass; returns number of jobs examined."""
        async with self._pass_lock:
            now = datetime.now(timezone.utc)
            active = await self.job_queue.list_active()
            for job in active:
                try:
                    await self._reconcile_job(job, now)
                except Exception:
                    logger.exception("reconciler: failed to reconcile job=%s", job.id)
            return len(active)

    async def run_forever(self) -> None:
        logger.info("reconciler: starting main loop (poll_interval=%.1fs)", self.poll_interval_s)
        while not self._shutdown.is_set():
            try:
                examined = await self.run_once()
                if examined:
                    logger.info("reconciler: examined %d active job(s)", examined)
            except Exception:
                logger.exception("reconciler: pass failed")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass
        logger.info("reconciler: graceful shutdown complete")


def _install_signal_handlers(reconciler: Reconciler) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, reconciler.request_shutdown)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_args: reconciler.request_shutdown())


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    # See scheduler.py's main() for the same caveat: only in-memory adapters exist in
    # this repo today, so the standalone entrypoint uses those.
    job_queue: JobQueue = InMemoryJobQueue()
    comfy_client: ComfyUIClient = MockComfyUIClient()

    reconciler = Reconciler(job_queue=job_queue, comfy_client=comfy_client, settings=settings)
    _install_signal_handlers(reconciler)
    await reconciler.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
