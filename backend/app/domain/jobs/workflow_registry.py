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
