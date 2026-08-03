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


_REGISTRY: dict[tuple[str, str], WorkflowDefinition] = {
    ("txt2img_basic", "v1"): WorkflowDefinition(
        name="txt2img_basic", version="v1", model_family="sdxl", graph_template={}
    ),
    ("img2img_basic", "v1"): WorkflowDefinition(
        name="img2img_basic", version="v1", model_family="sdxl", graph_template={}
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
