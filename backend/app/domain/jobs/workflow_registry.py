"""Server-side versioned workflow allowlist. Clients select a workflow by name+version;
they never supply an arbitrary ComfyUI graph. Unknown (name, version) pairs are rejected."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    version: str
    model_family: str
    graph_template: dict  # the actual ComfyUI graph, server-controlled
    # Which generation backend this workflow routes to. "comfyui" -> the
    # ComfyUI/MockComfyUIClient adapter; "gemini" -> Gemini image generation. See
    # app/adapters/routing_comfyui.py:CompositeComfyUIClient, which reads this via the
    # job's kind (== workflow name) to pick the right underlying adapter per job while
    # the scheduler/reconciler stay unaware there's more than one backend.
    backend: str = "comfyui"


_REGISTRY: dict[tuple[str, str], WorkflowDefinition] = {
    ("txt2img_basic", "v1"): WorkflowDefinition(
        name="txt2img_basic",
        version="v1",
        model_family="sdxl",
        graph_template={},
        backend="comfyui",
    ),
    ("img2img_basic", "v1"): WorkflowDefinition(
        name="img2img_basic",
        version="v1",
        model_family="sdxl",
        graph_template={},
        backend="comfyui",
    ),
    # Ordinary image generation ("Image" tab in the UI) -- plain photographic/artistic
    # images, routed to ComfyUI (mock or live).
    ("image_basic", "v1"): WorkflowDefinition(
        name="image_basic", version="v1", model_family="sdxl", graph_template={}, backend="comfyui"
    ),
    # Poster/infographic generation ("Poster / Infographic" tab) -- per explicit user
    # instruction, this always routes to the Gemini image model instead of ComfyUI
    # (Gemini tends to be much better at in-image text/layout than typical SDXL
    # workflows). Fails with a clear error if APP_GEMINI_API_KEY isn't configured
    # rather than silently falling back to ComfyUI -- see CompositeComfyUIClient.
    ("poster_infographic", "v1"): WorkflowDefinition(
        name="poster_infographic",
        version="v1",
        model_family="gemini-image",
        graph_template={},
        backend="gemini",
    ),
}


class UnknownWorkflowError(ValueError):
    pass


def resolve_workflow(name: str, version: str) -> WorkflowDefinition:
    key = (name, version)
    if key not in _REGISTRY:
        raise UnknownWorkflowError(f"unknown workflow {name}@{version}")
    return _REGISTRY[key]


def list_workflows() -> list[WorkflowDefinition]:
    return list(_REGISTRY.values())


def backend_for_kind(kind: str) -> str | None:
    """The backend ("comfyui"/"gemini") that `kind` (== workflow name, e.g.
    QueuedJob.kind) routes to -- the reverse direction of kinds_for_backend, needed by
    the 2026-08-20 queue_position/estimated_wait_seconds feature (see
    app/domain/jobs/admission.py:estimate_wait_seconds and GET /v1/jobs/{id} in
    app/api/v1/jobs.py) to pick the right capacity divisor
    (Settings.default_comfy_active_slots vs default_gemini_active_slots) for a given
    job. Same "only ever look at whichever definition is registered, multiple versions
    disagreeing on backend isn't supported" simplifying assumption as kinds_for_backend
    and CompositeComfyUIClient._resolve_backend above -- picks the first match.

    Returns None if `kind` isn't a registered workflow name at all. Callers reach this
    well after app/domain/jobs/admission.py's own resolve_workflow validation already
    accepted the kind, so None here should not normally happen in practice -- treat it
    defensively (skip the ETA fields) rather than raising.
    """
    for w in _REGISTRY.values():
        if w.name == kind:
            return w.backend
    return None


def kinds_for_backend(backend: str) -> frozenset[str]:
    """All registered workflow *names* (== QueuedJob.kind) whose current version routes
    to `backend` ("comfyui" or "gemini") -- used by app/services/scheduler.py to claim
    queued jobs per-backend so ComfyUI's GPU-bound capacity and Gemini's network-bound
    capacity can be limited independently (see Settings.default_comfy_active_slots /
    default_gemini_active_slots in app/core/config.py). A name with multiple versions
    that disagree on backend isn't supported today (matches the existing assumption in
    app/adapters/routing_comfyui.py:CompositeComfyUIClient._resolve_backend, which also
    only ever looks at the "v1" definition)."""
    return frozenset(w.name for w in _REGISTRY.values() if w.backend == backend)
