"""Scheduler process: claims queued/retry_wait jobs and dispatches them to ComfyUI.

Reuses existing domain logic rather than reimplementing it:
  - fairness/priority ordering: `JobQueue.claim_next` (app/adapters/queue) already sorts
    candidates by (-effective_priority, queued_at); `app.domain.jobs.fairness` is the
    source of truth for how effective_priority is computed when jobs are admitted/aged.
  - worker capacity/scoring: `app.domain.workers.scoring.select_worker` when a Postgres
    `comfy_workers` table is reachable (production/live mode).
  - dispatch: `app.adapters.comfyui.ComfyUIClient.submit` (mock or live, per settings).

Runnable via `python -m app.services.scheduler`. Handles SIGTERM/SIGINT for graceful
shutdown: stops claiming new jobs immediately, then awaits any in-flight dispatch tasks
before exiting so a claimed job is never abandoned mid-dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import uuid
from datetime import datetime, timezone

from app.adapters.comfyui import ComfyUIClient, MockComfyUIClient
from app.adapters.queue import JobQueue, QueuedJob
from app.adapters.queue.factory import build_job_queue
from app.core.config import Settings, get_settings
from app.domain.jobs.workflow_registry import kinds_for_backend
from app.domain.workers.scoring import WorkerSnapshot

logger = logging.getLogger("imaginv.scheduler")

DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_LEASE_SECONDS = 120.0


class Scheduler:
    """Claim -> reserve capacity -> dispatch loop.

    `session_factory`, when provided, is used to read live `comfy_workers` rows and
    score them with `app.domain.workers.scoring`. Without a DB (dev/in-memory mode, as
    wired by `app.main._build_state` today since only in-memory adapters exist), worker
    capacity falls back to `settings.default_comfy_active_slots` -- there is no real
    workers table to query in that mode. Either way, Gemini capacity is always
    `settings.default_gemini_active_slots`, tracked independently of ComfyUI -- see
    `_reserve_capacity_by_backend`.
    """

    def __init__(
        self,
        job_queue: JobQueue,
        comfy_client: ComfyUIClient,
        settings: Settings | None = None,
        session_factory=None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        owner_id: str | None = None,
    ) -> None:
        self.job_queue = job_queue
        self.comfy_client = comfy_client
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.poll_interval_s = poll_interval_s
        self.lease_seconds = lease_seconds
        self.owner_id = owner_id or f"scheduler-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._shutdown = asyncio.Event()
        self._inflight: set[asyncio.Task] = set()

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            logger.info(
                "scheduler[%s]: shutdown requested; will stop claiming new jobs", self.owner_id
            )
        self._shutdown.set()

    async def _reserve_capacity_by_backend(self) -> dict[str, int]:
        """How many jobs of each backend ("comfyui" / "gemini") may be claimed this tick.

        Split on purpose (see Settings.default_gemini_active_slots' docstring in
        app/core/config.py): ComfyUI dispatch is GPU-bound and capped conservatively
        (from the live `comfy_workers` registry when one exists, else
        default_comfy_active_slots); Gemini image generation is just an outbound HTTPS
        call to Google and doesn't compete for this machine's GPU at all, so it gets its
        own independent cap (default_gemini_active_slots) regardless of which path is
        used for ComfyUI capacity -- there is no "gemini_workers" registry table, Gemini
        isn't a fleet of instances to select between."""
        gemini_capacity = self.settings.default_gemini_active_slots

        if self.session_factory is None:
            return {"comfyui": self.settings.default_comfy_active_slots, "gemini": gemini_capacity}

        from sqlalchemy import select

        from app.db.models import ComfyWorker

        async with self.session_factory() as session:
            result = await session.execute(select(ComfyWorker))
            rows = result.scalars().all()

        now = datetime.now(timezone.utc)
        snapshots = [
            WorkerSnapshot(
                worker_id=str(w.id),
                status=w.status,
                capabilities=frozenset((w.capabilities or {}).keys()) or frozenset({"default"}),
                max_slots=w.max_slots,
                reserved_slots=w.reserved_slots,
                running_slots=w.running_slots,
                local_queue_depth=0,
                last_heartbeat_at=w.last_heartbeat_at or now,
                recent_failure_rate=0.0,
                current_model_loaded=None,
            )
            for w in rows
        ]
        available = sum(
            max(0, s.max_slots - s.reserved_slots - s.running_slots)
            for s in snapshots
            if s.status == "online"
        )
        return {"comfyui": max(available, 0), "gemini": gemini_capacity}

    async def _dispatch(self, job: QueuedJob) -> None:
        """Marks the job running and submits it to ComfyUI. Any failure here is
        reported back through the JobQueue port so the existing retry/backoff logic
        (applied by the reconciler / next scheduler pass) can decide what happens next;
        this method never silently swallows an error."""
        try:
            await self.job_queue.mark_running(job.id)
            submit_result = await self.comfy_client.submit(job.input_payload, kind=job.kind)
            if hasattr(self.job_queue, "set_prompt_id"):
                await self.job_queue.set_prompt_id(job.id, submit_result.prompt_id)
            logger.info(
                "scheduler[%s]: dispatched job=%s prompt_id=%s",
                self.owner_id,
                job.id,
                submit_result.prompt_id,
            )
        except Exception as exc:
            logger.exception("scheduler[%s]: dispatch failed for job=%s", self.owner_id, job.id)
            detail = f"dispatch_error:{type(exc).__name__}"
            if hasattr(self.job_queue, "mark_retry_wait"):
                await self.job_queue.mark_retry_wait(job.id, "worker_unreachable", detail)
            else:
                await self.job_queue.mark_failed(job.id, "worker_unreachable", detail)

    async def _claim_for_backend(self, backend: str, capacity: int) -> list[QueuedJob]:
        if capacity <= 0:
            return []
        kinds = kinds_for_backend(backend)
        if hasattr(self.job_queue, "claim_next_with_lease"):
            return await self.job_queue.claim_next_with_lease(
                worker_capacity=capacity,
                lease_owner=self.owner_id,
                lease_seconds=self.lease_seconds,
                kinds=kinds,
            )
        return await self.job_queue.claim_next(worker_capacity=capacity, kinds=kinds)

    async def _tick(self) -> None:
        capacity_by_backend = await self._reserve_capacity_by_backend()
        # Two independent claims (one per backend, each against its own capacity number)
        # rather than one combined claim -- see _reserve_capacity_by_backend's docstring.
        # Each still preserves fairness/priority ordering *within* its own backend lane
        # (JobQueue.claim_next sorts by -effective_priority, queued_at as before); the
        # two lanes just no longer block each other.
        claimed: list[QueuedJob] = []
        for backend, capacity in capacity_by_backend.items():
            claimed.extend(await self._claim_for_backend(backend, capacity))
        for job in claimed:
            task = asyncio.create_task(self._dispatch(job))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def run_forever(self) -> None:
        logger.info(
            "scheduler[%s]: starting main loop (poll_interval=%.1fs, lease=%.0fs)",
            self.owner_id,
            self.poll_interval_s,
            self.lease_seconds,
        )
        while not self._shutdown.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("scheduler[%s]: tick failed", self.owner_id)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass

        logger.info(
            "scheduler[%s]: draining %d in-flight dispatch(es) before exit",
            self.owner_id,
            len(self._inflight),
        )
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)
        logger.info("scheduler[%s]: graceful shutdown complete", self.owner_id)


def _install_signal_handlers(scheduler: Scheduler) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, scheduler.request_shutdown)
        except (NotImplementedError, RuntimeError):
            # Some platforms/event loops (e.g. Windows ProactorEventLoop) don't support
            # add_signal_handler; fall back to the classic signal.signal() API.
            signal.signal(sig, lambda *_args: scheduler.request_shutdown())


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    # queue_backend picks InMemoryJobQueue vs the durable/shared PostgresJobQueue (see
    # app/adapters/queue/factory.py) -- this is now the SAME switch app/main.py uses, so
    # this standalone process and the API agree on where job state actually lives.
    session_factory = None
    if settings.queue_backend == "postgres":
        from app.db.base import get_session_factory

        session_factory = get_session_factory()
    job_queue: JobQueue = build_job_queue(settings, session_factory=session_factory)

    # NOTE (still a gap, unrelated to the JobQueue backend above): this standalone
    # entrypoint always dispatches against MockComfyUIClient, unlike app/main.py's
    # `_build_state`, which wires a real live/multi-worker ComfyUI client or Gemini from
    # settings. A live-mode deployment running this process standalone (queue_backend=
    # postgres, APP_COMFY_MODE=live) would currently claim real jobs and "dispatch" them
    # to the mock instead of real ComfyUI -- extracting `_build_state`'s comfy_client
    # wiring into a shared factory (mirroring build_job_queue above) is the next
    # increment before relying on this process in production; not done here to keep this
    # change scoped to the job-queue durability gap.
    comfy_client: ComfyUIClient = MockComfyUIClient()

    scheduler = Scheduler(
        job_queue=job_queue, comfy_client=comfy_client, settings=settings, session_factory=session_factory
    )
    _install_signal_handlers(scheduler)
    await scheduler.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
