"""CompositeComfyUIClient routes jobs to ComfyUI vs Gemini based on the workflow's
`backend` field (app/domain/jobs/workflow_registry.py), per explicit product decision:
ordinary "Image" generation always goes to ComfyUI; "Poster / Infographic" always goes
to Gemini, and fails clearly (not silently falls back to ComfyUI) if Gemini isn't
configured."""

from __future__ import annotations

import pytest

from app.adapters.comfyui import MockComfyUIClient
from app.adapters.routing_comfyui import CompositeComfyUIClient


@pytest.mark.asyncio
async def test_image_basic_routes_to_comfyui() -> None:
    comfyui = MockComfyUIClient()
    router = CompositeComfyUIClient(comfyui_client=comfyui, gemini_client=None)

    result = await router.submit({"prompt": "a cat"}, kind="image_basic")
    status = await router.get_status(result.prompt_id)

    assert status.state == "succeeded"
    assert result.prompt_id in comfyui._payloads  # actually reached the ComfyUI adapter


@pytest.mark.asyncio
async def test_poster_infographic_routes_to_gemini_when_configured() -> None:
    comfyui = MockComfyUIClient()

    class FakeGemini:
        def __init__(self) -> None:
            self.submitted: list[dict] = []

        async def submit(self, workflow_payload, kind=None):
            from app.adapters.comfyui import ComfySubmitResult

            self.submitted.append(workflow_payload)
            return ComfySubmitResult(prompt_id="gemini-prompt-1")

        async def get_status(self, prompt_id):
            from app.adapters.comfyui import ComfyStatus

            return ComfyStatus(prompt_id=prompt_id, state="succeeded", outputs=[])

        async def cancel(self, prompt_id):
            pass

        async def health(self):
            return True

    gemini = FakeGemini()
    router = CompositeComfyUIClient(comfyui_client=comfyui, gemini_client=gemini)

    result = await router.submit({"prompt": "a poster"}, kind="poster_infographic")
    status = await router.get_status(result.prompt_id)

    assert status.state == "succeeded"
    assert len(gemini.submitted) == 1
    assert result.prompt_id not in comfyui._payloads  # did NOT reach ComfyUI


@pytest.mark.asyncio
async def test_poster_infographic_fails_clearly_without_gemini_configured() -> None:
    comfyui = MockComfyUIClient()
    router = CompositeComfyUIClient(comfyui_client=comfyui, gemini_client=None)

    result = await router.submit({"prompt": "a poster"}, kind="poster_infographic")
    status = await router.get_status(result.prompt_id)

    assert status.state == "failed"
    assert status.error == "gemini_not_configured"
    assert result.prompt_id not in comfyui._payloads  # did NOT silently fall back
