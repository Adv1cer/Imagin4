"""Liveness/readiness endpoints. No auth required; used by container healthchecks."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict:
    """Process is up and event loop is responsive. Never touches the DB/Redis."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict:
    """Process can serve traffic: DB and job queue backends are reachable."""
    checks: dict[str, str] = {}
    healthy = True

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is not None:
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # pragma: no cover - depends on live infra
            checks["database"] = f"error: {exc}"
            healthy = False
    else:
        checks["database"] = "not configured"

    comfy_client = getattr(request.app.state, "comfy_client", None)
    if comfy_client is not None:
        try:
            ok = await comfy_client.health()
            checks["comfyui"] = "ok" if ok else "unhealthy"
            healthy = healthy and ok
        except Exception as exc:  # pragma: no cover
            checks["comfyui"] = f"error: {exc}"
            healthy = False

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "checks": checks}
