import pytest

from app.domain.jobs.workflow_registry import UnknownWorkflowError, resolve_workflow


def test_known_workflow_resolves():
    wf = resolve_workflow("txt2img_basic", "v1")
    assert wf.model_family == "sdxl"


def test_unknown_workflow_rejected():
    with pytest.raises(UnknownWorkflowError):
        resolve_workflow("arbitrary_client_graph", "v99")
