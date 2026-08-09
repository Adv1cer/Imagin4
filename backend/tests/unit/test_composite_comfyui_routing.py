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


# --- comfy_prompt_designer: the "refine the prompt before sending to ComfyUI" step
# (mirrors GeminiImageComfyUIClient's prompt_designer on the Gemini side). ---


@pytest.mark.asyncio
async def test_comfy_prompt_designer_refines_prompt_before_reaching_comfyui() -> None:
    comfyui = MockComfyUIClient()
    calls: list[tuple[str, list[str]]] = []

    async def fake_designer(prompt: str, exact_text: list[str]) -> str:
        calls.append((prompt, exact_text))
        return "highly detailed, cinematic lighting, sharp focus, a cat in a spacesuit"

    router = CompositeComfyUIClient(
        comfyui_client=comfyui, gemini_client=None, comfy_prompt_designer=fake_designer
    )

    result = await router.submit({"prompt": "a cat in a spacesuit"}, kind="image_basic")

    assert calls == [("a cat in a spacesuit", [])]
    sent_payload = comfyui._payloads[result.prompt_id]
    assert sent_payload["prompt"] == (
        "highly detailed, cinematic lighting, sharp focus, a cat in a spacesuit"
    )


@pytest.mark.asyncio
async def test_comfy_prompt_designer_never_applied_to_gemini_backed_jobs() -> None:
    """The ComfyUI-specific designer must not touch poster/infographic jobs -- those
    already get their own, differently-instructed design step inside
    GeminiImageComfyUIClient itself."""
    comfyui = MockComfyUIClient()
    designer_calls = []

    async def fake_designer(prompt, exact_text):
        designer_calls.append((prompt, exact_text))
        return "should never be used"

    class FakeGemini:
        def __init__(self):
            self.submitted = []

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
    router = CompositeComfyUIClient(
        comfyui_client=comfyui, gemini_client=gemini, comfy_prompt_designer=fake_designer
    )

    await router.submit({"prompt": "a poster"}, kind="poster_infographic")

    assert designer_calls == []
    assert gemini.submitted[0]["prompt"] == "a poster"


@pytest.mark.asyncio
async def test_comfy_prompt_designer_failure_falls_back_to_original_prompt() -> None:
    comfyui = MockComfyUIClient()

    async def failing_designer(prompt, exact_text):
        raise RuntimeError("gemini_error:TimeoutError")

    router = CompositeComfyUIClient(
        comfyui_client=comfyui, gemini_client=None, comfy_prompt_designer=failing_designer
    )

    result = await router.submit({"prompt": "a cat"}, kind="image_basic")
    status = await router.get_status(result.prompt_id)

    # Never fatal -- still succeeds, using the original unrefined prompt.
    assert status.state == "succeeded"
    assert comfyui._payloads[result.prompt_id]["prompt"] == "a cat"


@pytest.mark.asyncio
async def test_comfy_prompt_designer_skipped_when_no_prompt_or_exact_text() -> None:
    """No point calling the designer (and burning a Gemini call) for an empty payload --
    submit()'s own empty-prompt handling in the underlying adapter is what should fail
    it, not a wasted design call."""
    comfyui = MockComfyUIClient()
    designer_calls = []

    async def fake_designer(prompt, exact_text):
        designer_calls.append((prompt, exact_text))
        return "irrelevant"

    router = CompositeComfyUIClient(
        comfyui_client=comfyui, gemini_client=None, comfy_prompt_designer=fake_designer
    )

    await router.submit({"prompt": ""}, kind="image_basic")
    assert designer_calls == []
