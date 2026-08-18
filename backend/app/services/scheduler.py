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

import httpx
from sqlalchemy import text

from app.adapters.comfyui import ComfyUIClient
from app.adapters.comfyui.factory import build_comfy_client
from app.adapters.queue import JobQueue, QueuedJob
from app.adapters.queue.factory import build_job_queue
from app.adapters.storage import InMemoryObjectStorage
from app.adapters.storage.s3 import S3ObjectStorage
from app.core.config import Settings, get_settings
from app.domain.jobs.workflow_registry import kinds_for_backend
from app.domain.workers.scoring import WorkerSnapshot

logger = logging.getLogger("imaginv.scheduler")

DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_LEASE_SECONDS = 120.0
# See Scheduler._heartbeat_once's docstring: this is the gap discovered 2026-08-18 --
# _reserve_capacity_by_backend() reads comfy_workers with NO fallback in postgres mode,
# but nothing was writing to that table, so capacity was always 0 and every job stayed
# stuck in 'queued' forever. 5s keeps worker status reasonably fresh without hammering
# each ComfyUI instance's /queue endpoint on every 1s dispatch tick.
DEFAULT_HEARTBEAT_INTERVAL_S = 5.0
HEARTBEAT_HTTP_TIMEOUT_S = 3.0


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
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        owner_id: str | None = None,
    ) -> None:
        self.job_queue = job_queue
        self.comfy_client = comfy_client
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.poll_interval_s = poll_interval_s
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_s = heartbeat_interval_s
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

    @staticmethod
    def _worker_name(base_url: str) -> str:
        """Deterministic, stable-across-restarts name derived from the worker's own
        base_url (config-driven, not a random id per process) -- comfy_workers.name has
        a UNIQUE constraint, so re-running this heartbeat against the SAME worker always
        UPSERTs the SAME row instead of accumulating duplicates every restart."""
        return base_url.replace("http://", "").replace("https://", "").replace("/", "-").rstrip("-")

    async def _heartbeat_once(self) -> None:
        """UPSERTs one comfy_workers row per configured worker base_url, reflecting
        whether it currently answers /queue and whether it's mid-execution.

        Closes the gap discovered 2026-08-18 (Chet's DGX Spark): with queue_backend=
        postgres, _reserve_capacity_by_backend() computes ComfyUI capacity ONLY from
        comfy_workers rows with status='online' -- there is no fallback to
        default_comfy_active_slots like the in-memory-queue path has. Nothing in this
        repo previously wrote to comfy_workers at all, so the table stayed permanently
        empty and every job sat in 'queued' forever, even though ComfyUI itself was
        healthy and idle. A manual SQL INSERT unblocks a single test run but goes stale
        the moment a real worker dies -- this loop is the durable fix.

        Deliberately coarse for a first pass: max_slots is hardcoded to 1 (each ComfyUI
        process here executes one prompt at a time; see Settings.comfy_worker_base_urls_csv's
        docstring on the single-GPU/MPS caveat -- this does NOT attempt to read real GPU
        VRAM, since GB10/DGX Spark's NVML reporting is unreliable, see MPS_RUNBOOK.md).
        running_slots is 1 if ComfyUI's own /queue reports anything in queue_running,
        else 0 -- good enough for _reserve_capacity_by_backend's slot arithmetic without
        needing this process to track individual job->worker assignment itself.
        """
        if self.session_factory is None or self.settings.comfy_mode == "mock":
            return  # nothing to register in memory-queue/mock-comfy dev mode

        worker_urls = self.settings.comfy_worker_base_urls or (
            [self.settings.comfy_base_url] if self.settings.comfy_base_url else []
        )
        if not worker_urls:
            return

        async with httpx.AsyncClient(timeout=HEARTBEAT_HTTP_TIMEOUT_S) as client:
            for base_url in worker_urls:
                name = self._worker_name(base_url)
                status = "offline"
                running_slots = 0
                try:
                    resp = await client.get(f"{base_url}/queue")
                    resp.raise_for_status()
                    data = resp.json()
                    running_slots = 1 if data.get("queue_running") else 0
                    status = "online"
                except Exception:
                    logger.warning(
                        "scheduler[%s]: heartbeat probe failed for worker=%s (marking offline)",
                        self.owner_id,
                        base_url,
                        exc_info=True,
                    )

                try:
                    async with self.session_factory() as session, session.begin():
                        await session.execute(
                            text(
                                """
                                INSERT INTO comfy_workers
                                    (id, name, endpoint_ref, status, max_slots,
                                     reserved_slots, running_slots, last_heartbeat_at)
                                VALUES
                                    (gen_random_uuid(), :name, :endpoint_ref, :status, 1,
                                     0, :running_slots, now())
                                ON CONFLICT (name) DO UPDATE SET
                                    endpoint_ref = excluded.endpoint_ref,
                                    status = excluded.status,
                                    running_slots = excluded.running_slots,
                                    last_heartbeat_at = excluded.last_heartbeat_at
                                """
                            ),
                            {
                                "name": name,
                                "endpoint_ref": base_url,
                                "status": status,
                                "running_slots": running_slots,
                            },
                        )
                except Exception:
                    # comfy_workers.id needs pgcrypto's gen_random_uuid() (Postgres 13+
                    # has it built in; older/custom installs may not) -- log and move on
                    # rather than crashing the whole heartbeat pass over one bad row.
                    logger.exception(
                        "scheduler[%s]: failed to upsert comfy_workers row for %s", self.owner_id, name
                    )

    async def run_heartbeat_forever(self) -> None:
        if self.session_factory is None or self.settings.comfy_mode == "mock":
            logger.info(
                "scheduler[%s]: worker heartbeat loop not needed (queue_backend=memory or "
                "comfy_mode=mock) -- capacity uses default_comfy_active_slots instead",
                self.owner_id,
            )
            return
        logger.info(
            "scheduler[%s]: starting worker heartbeat loop (interval=%.1fs)",
            self.owner_id,
            self.heartbeat_interval_s,
        )
        while not self._shutdown.is_set():
            try:
                await self._heartbeat_once()
            except Exception:
                logger.exception("scheduler[%s]: heartbeat pass failed", self.owner_id)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.heartbeat_interval_s)
            except asyncio.TimeoutError:
                pass
        logger.info("scheduler[%s]: heartbeat loop shut down", self.owner_id)

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

    # settings.storage_backend must agree with the API process's -- see
    # Settings.storage_backend's docstring: a generated image uploaded by THIS process
    # (scheduler dispatches, but the LiveComfyUIClient/GeminiImageComfyUIClient upload
    # happens as part of that dispatch) must be visible to whichever process later
    # serves GET /v1/jobs/{id}/asset.
    storage = S3ObjectStorage(settings) if settings.storage_backend == "s3" else InMemoryObjectStorage()
    # Same real client app/main.py's API process builds -- see
    # app/adapters/comfyui/factory.py's module docstring for why this standalone
    # entrypoint used to hardcode MockComfyUIClient here and why that was a live risk
    # once queue_backend=postgres made this process the one actually dispatching jobs.
    comfy_client: ComfyUIClient = build_comfy_client(settings, storage)

    scheduler = Scheduler(
        job_queue=job_queue, comfy_client=comfy_client, settings=settings, session_factory=session_factory
    )
    _install_signal_handlers(scheduler)
    # Both loops share the same `_shutdown` Event (request_shutdown() sets it once for
    # both), so SIGTERM/SIGINT still drains in-flight dispatches AND stops the heartbeat
    # loop together -- see run_heartbeat_forever()'s no-op early return when
    # queue_backend=memory/comfy_mode=mock, so this is always safe to start.
    await asyncio.gather(scheduler.run_forever(), scheduler.run_heartbeat_forever())


if __name__ == "__main__":
    asyncio.run(main())
