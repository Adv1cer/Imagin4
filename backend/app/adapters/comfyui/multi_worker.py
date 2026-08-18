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
actually owns each prompt_id.

Ownership is encoded directly IN the returned prompt_id (`"<worker_index>:<real_id>"`),
not tracked in an in-memory dict -- see CompositeComfyUIClient (app/adapters/
routing_comfyui.py) for the parallel fix and the full story. Under
queue_backend=postgres, submit() (called by the scheduler process) and get_status()/
cancel() (called by the reconciler process) run in separate OS processes, each with its
own MultiWorkerComfyUIClient instance -- a dict populated by submit() in one process is
invisible to get_status() in the other, which is exactly the bug this encoding avoids.
Confirmed live (2026-08): every real job failed with error="unknown_prompt_id" because
the reconciler's own (empty) ownership dict never saw what the scheduler's submit() call
recorded in ITS process memory.

Single-GPU caveat (see project instructions / README): running N workers does not
multiply GPU compute -- if every worker shares one physical GPU, expect at best partial
overlap, not linear speedup. Benchmark with a small worker count before scaling up (see
Settings.default_comfy_active_slots' docstring in app/core/config.py).
"""

from __future__ import annotations

import logging

from app.adapters.comfyui import ComfyStatus, ComfySubmitResult, ComfyUIClient

logger = logging.getLogger("imaginv.comfyui_multi_worker")

_SEP = ":"


class MultiWorkerComfyUIClient:
    """`ComfyUIClient` implementation that round-robins `submit()` across `workers`."""

    def __init__(self, workers: list[ComfyUIClient]) -> None:
        if not workers:
            raise ValueError("MultiWorkerComfyUIClient requires at least one worker")
        self._workers = list(workers)
        self._next_index = 0

    def _pick_worker(self) -> tuple[int, ComfyUIClient]:
        """Plain round-robin: no health/load awareness needed here -- a worker that's
        actually down will fail submit() or the resulting job's polling, which the
        existing retry/backoff path (app/domain/jobs/retry.py) already handles by
        re-queuing the job for a fresh scheduler dispatch, which calls submit() again and
        so naturally has a chance to land on a different (healthy) worker next attempt."""
        index = self._next_index % len(self._workers)
        self._next_index += 1
        return index, self._workers[index]

    def _resolve(self, prompt_id: str) -> tuple[ComfyUIClient | None, str]:
        """Splits the leading `"<worker_index>:"` tag back off. `self._workers`' order is
        rebuilt identically (same settings.comfy_worker_base_urls order) in every
        process's own build_comfy_client() call, so an index minted by submit() in the
        scheduler process resolves to the same worker when decoded in the reconciler
        process -- no shared state required. Returns (None, prompt_id) if the tag is
        missing/unparseable (e.g. a prompt_id from some other source), which callers treat
        as "unknown"."""
        index_str, sep, real_id = prompt_id.partition(_SEP)
        if not sep:
            return None, prompt_id
        try:
            index = int(index_str)
            return self._workers[index], real_id
        except (ValueError, IndexError):
            return None, prompt_id

    async def submit(self, workflow_payload: dict, kind: str | None = None) -> ComfySubmitResult:
        index, worker = self._pick_worker()
        result = await worker.submit(workflow_payload, kind=kind)
        composite_id = f"{index}{_SEP}{result.prompt_id}"
        logger.info(
            "comfyui_multi_worker: kind=%s routed to worker=%s prompt_id=%s",
            kind,
            index,
            composite_id,
        )
        return ComfySubmitResult(prompt_id=composite_id)

    async def get_status(self, prompt_id: str) -> ComfyStatus:
        worker, real_id = self._resolve(prompt_id)
        if worker is None:
            return ComfyStatus(prompt_id=prompt_id, state="failed", error="unknown_prompt_id")
        status = await worker.get_status(real_id)
        # Re-wrap under the original composite id -- callers (reconciler, JobOut, etc.)
        # only ever know this job by the id submit() returned.
        return ComfyStatus(
            prompt_id=prompt_id, state=status.state, outputs=status.outputs, error=status.error
        )

    async def cancel(self, prompt_id: str) -> None:
        worker, real_id = self._resolve(prompt_id)
        if worker is not None:
            await worker.cancel(real_id)

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
