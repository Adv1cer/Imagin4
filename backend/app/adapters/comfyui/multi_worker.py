"""MultiWorkerComfyUIClient: spreads generation jobs round-robin across several
independently-addressed ComfyUI instances (e.g. several `comfyui-worker-N` docker
services, each pinned to the same shared GPU or, eventually, one GPU each), while
presenting one `ComfyUIClient` to the scheduler/reconciler -- neither needs to know more
than one worker exists. Mirrors the ownership-tracking pattern already used by
`app.adapters.routing_comfyui.CompositeComfyUIClient` for ComfyUI-vs-Gemini routing.

Why this exists (2026-08): a single external ComfyUI process serialized ALL image_basic
jobs one at a time regardless of `Settings.default_comfy_active_slots`, since the
scheduler's concurrency cap has always been about how many jobs the SCHEDULER may claim
concurrently, not how many independent execution engines exist to run them. Running
several ComfyUI instances (see docker-compose.yml's `comfyui-worker-*` services) gives
the scheduler somewhere to actually send concurrently-claimed jobs.

Correctness note: a ComfyUI `prompt_id` is only meaningful against the SAME instance that
accepted it -- `/history/{prompt_id}` and `/queue` are per-process state, not shared
across a cluster. So `get_status()`/`cancel()` must be routed back to whichever worker
actually owns each prompt_id, tracked here exactly like CompositeComfyUIClient tracks
which backend (ComfyUI vs Gemini) owns each prompt_id -- never re-derived or guessed.

Single-GPU caveat (see project instructions / README): running N workers does not
multiply GPU compute -- if every worker shares one physical GPU, expect at best partial
overlap, not linear speedup. Benchmark with a small worker count before scaling up (see
Settings.default_comfy_active_slots' docstring in app/core/config.py).
"""

from __future__ import annotations

import logging

from app.adapters.comfyui import ComfyStatus, ComfySubmitResult, ComfyUIClient

logger = logging.getLogger("imaginv.comfyui_multi_worker")


class MultiWorkerComfyUIClient:
    """`ComfyUIClient` implementation that round-robins `submit()` across `workers`."""

    def __init__(self, workers: list[ComfyUIClient]) -> None:
        if not workers:
            raise ValueError("MultiWorkerComfyUIClient requires at least one worker")
        self._workers = list(workers)
        self._next_index = 0
        # Tracks which underlying worker accepted each prompt_id so get_status/cancel
        # always ask the SAME instance that owns it -- see module docstring.
        self._owner: dict[str, ComfyUIClient] = {}

    def _pick_worker(self) -> ComfyUIClient:
        """Plain round-robin: no health/load awareness needed here -- a worker that's
        actually down will fail submit() or the resulting job's polling, which the
        existing retry/backoff path (app/domain/jobs/retry.py) already handles by
        re-queuing the job for a fresh scheduler dispatch, which calls submit() again and
        so naturally has a chance to land on a different (healthy) worker next attempt."""
        worker = self._workers[self._next_index % len(self._workers)]
        self._next_index += 1
        return worker

    async def submit(self, workflow_payload: dict, kind: str | None = None) -> ComfySubmitResult:
        worker = self._pick_worker()
        result = await worker.submit(workflow_payload, kind=kind)
        self._owner[result.prompt_id] = worker
        logger.info(
            "comfyui_multi_worker: kind=%s routed to worker=%s prompt_id=%s",
            kind,
            self._workers.index(worker),
            result.prompt_id,
        )
        return result

    async def get_status(self, prompt_id: str) -> ComfyStatus:
        worker = self._owner.get(prompt_id)
        if worker is None:
            return ComfyStatus(prompt_id=prompt_id, state="failed", error="unknown_prompt_id")
        return await worker.get_status(prompt_id)

    async def cancel(self, prompt_id: str) -> None:
        # Deliberately does NOT forget ownership here (unlike a hypothetical "cancel also
        # unregisters" design) -- a caller may reasonably call get_status() right after
        # cancel() to confirm the terminal state, which must still route back to the same
        # worker. Matches CompositeComfyUIClient.cancel()'s equivalent choice.
        worker = self._owner.get(prompt_id)
        if worker is not None:
            await worker.cancel(prompt_id)

    async def health(self) -> bool:
        """True if AT LEAST ONE worker is healthy -- one worker being briefly down
        shouldn't flip the whole API's readiness probe to unhealthy (see
        GET /health/ready) when the rest can still take jobs. A job that happens to
        round-robin onto the down worker fails and retries onto another one -- see
        _pick_worker's docstring."""
        for worker in self._workers:
            try:
                if await worker.health():
                    return True
            except Exception:
                continue
        return False
