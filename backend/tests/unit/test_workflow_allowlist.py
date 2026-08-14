import pytest

from app.domain.jobs.workflow_registry import (
    UnknownWorkflowError,
    kinds_for_backend,
    resolve_workflow,
)


def test_known_workflow_resolves():
    wf = resolve_workflow("txt2img_basic", "v1")
    assert wf.model_family == "sdxl"


def test_unknown_workflow_rejected():
    with pytest.raises(UnknownWorkflowError):
        resolve_workflow("arbitrary_client_graph", "v99")


def test_kinds_for_backend_splits_comfyui_and_gemini():
    comfy_kinds = kinds_for_backend("comfyui")
    gemini_kinds = kinds_for_backend("gemini")
    assert "image_basic" in comfy_kinds
    assert "txt2img_basic" in comfy_kinds
    assert "poster_infographic" in gemini_kinds
    # No overlap -- every registered kind routes to exactly one backend.
    assert comfy_kinds.isdisjoint(gemini_kinds)


def test_kinds_for_backend_unknown_backend_returns_empty():
    assert kinds_for_backend("some_backend_that_does_not_exist") == frozenset()
