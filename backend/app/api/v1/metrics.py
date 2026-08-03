"""Prometheus text-format metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["metrics"])

try:
    from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest

    REGISTRY = CollectorRegistry()
    HTTP_REQUESTS_TOTAL = Counter(
        "http_requests_total", "Total HTTP requests", ["method", "path", "status"], registry=REGISTRY
    )
    QUEUE_DEPTH = Gauge("job_queue_depth", "Jobs currently queued or in retry_wait", registry=REGISTRY)
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover - prometheus_client optional in minimal test envs
    _PROM_AVAILABLE = False


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    if not _PROM_AVAILABLE:
        return Response(content="# prometheus_client not installed\n", media_type="text/plain")

    queue = getattr(request.app.state, "job_queue", None)
    if queue is not None and hasattr(queue, "_jobs"):
        depth = sum(1 for j in queue._jobs.values() if j.state in ("queued", "retry_wait"))
        QUEUE_DEPTH.set(depth)

    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
