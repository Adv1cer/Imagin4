"""Contract tests for LiveComfyUIClient against a fake HTTP transport (httpx.MockTransport)
-- no real ComfyUI process required. Exercises the same submit -> poll -> terminal-state
contract as tests/contract/test_mock_comfyui_client.py, plus the ComfyUI-specific
history/queue/view protocol translation and image-bytes-to-storage handoff."""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.comfyui.live import LiveComfyUIClient, _resolve_dimensions
from app.adapters.storage import InMemoryObjectStorage

BASE_URL = "http://comfy.test:8188"


def _client(handler, storage: InMemoryObjectStorage | None = None) -> LiveComfyUIClient:
    return LiveComfyUIClient(
        base_url=BASE_URL,
        storage=storage or InMemoryObjectStorage(),
        checkpoint_name="sdxl.safetensors",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_submit_posts_a_server_built_graph_never_a_client_graph() -> None:
    """The whole point of building the graph server-side: assert the POST body's
    "prompt" key is OUR graph (keyed by our fixed node ids, containing our checkpoint
    name), not anything resembling client input echoed through."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/prompt"
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"prompt_id": "p1"})

    client = _client(handler)
    result = await client.submit({"prompt": "a cat", "aspect_ratio": "1:1", "resolution": "1K"})

    assert result.prompt_id == "p1"
    graph = captured["body"]["prompt"]
    assert graph["4"]["inputs"]["ckpt_name"] == "sdxl.safetensors"
    assert graph["6"]["inputs"]["text"] == "a cat"
    # Nothing resembling a raw client-supplied graph made it into the request.
    assert "aspect_ratio" not in json.dumps(captured["body"])


@pytest.mark.asyncio
async def test_submit_uses_a_unique_filename_prefix_per_job() -> None:
    """Regression test: every job used to share the literal SaveImage filename_prefix
    "imaginv", which let ComfyUI's own asset-browser/gallery group same-prefix outputs
    into one collage thumbnail -- a later /history lookup then resolved to that
    composite image instead of this job's own single output (root-caused 2026-08
    against a live ComfyUI instance whose "Media Assets" panel showed the exact same
    collage for the colliding filename). Each submit() call must mint its own prefix."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"prompt_id": f"p{len(captured)}"})

    client = _client(handler)
    await client.submit({"prompt": "a cat", "aspect_ratio": "1:1", "resolution": "1K"})
    await client.submit({"prompt": "a dog", "aspect_ratio": "1:1", "resolution": "1K"})

    prefix_1 = captured[0]["prompt"]["9"]["inputs"]["filename_prefix"]
    prefix_2 = captured[1]["prompt"]["9"]["inputs"]["filename_prefix"]
    assert prefix_1.startswith("imaginv-")
    assert prefix_2.startswith("imaginv-")
    assert prefix_1 != prefix_2


@pytest.mark.asyncio
async def test_submit_qwen_image_family_builds_split_file_graph() -> None:
    """Qwen-Image uses a structurally different graph (UNETLoader/CLIPLoader/VAELoader +
    ModelSamplingAuraFlow, not CheckpointLoaderSimple) -- verified against the official
    docs.comfy.org workflow template, not guessed. This asserts the graph our code
    actually sends matches that structure node-for-node."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"prompt_id": "p1"})

    client = LiveComfyUIClient(
        base_url=BASE_URL,
        storage=InMemoryObjectStorage(),
        model_family="qwen_image",
        diffusion_model_name="qwen_image_2512_fp8_e4m3fn.safetensors",
        clip_name="qwen_2.5_vl_7b_fp8_scaled.safetensors",
        vae_name="qwen_image_vae.safetensors",
        model_sampling_shift=3.1,
        scheduler="simple",
        cfg_scale=4.0,
        transport=httpx.MockTransport(handler),
    )
    await client.submit({"prompt": "a poster", "aspect_ratio": "16:9", "resolution": "1K"})

    graph = captured["body"]["prompt"]
    assert graph["37"] == {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": "qwen_image_2512_fp8_e4m3fn.safetensors", "weight_dtype": "default"},
    }
    assert graph["38"] == {
        "class_type": "CLIPLoader",
        "inputs": {
            "clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
            "type": "qwen_image",
            "device": "default",
        },
    }
    assert graph["39"] == {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}}
    assert graph["66"] == {
        "class_type": "ModelSamplingAuraFlow",
        "inputs": {"model": ["37", 0], "shift": 3.1},
    }
    # KSampler must read its model from the ModelSamplingAuraFlow output (node 66),
    # NOT directly from UNETLoader (node 37) -- skipping it is a common mistake that
    # produces wrong-looking (washed out/oversaturated) Qwen-Image output.
    assert graph["3"]["inputs"]["model"] == ["66", 0]
    assert graph["3"]["inputs"]["latent_image"] == ["58", 0]
    assert graph["3"]["inputs"]["cfg"] == 4.0
    assert graph["3"]["inputs"]["scheduler"] == "simple"
    assert graph["6"]["inputs"] == {"text": "a poster", "clip": ["38", 0]}
    assert graph["58"]["class_type"] == "EmptySD3LatentImage"
    # 16:9 at 1K per Qwen-Image's own documented aspect-ratio table (1664x928).
    assert (graph["58"]["inputs"]["width"], graph["58"]["inputs"]["height"]) == (1664, 928)
    assert "CheckpointLoaderSimple" not in json.dumps(graph)


@pytest.mark.asyncio
async def test_empty_prompt_fails_without_a_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not make an HTTP call for an empty prompt")

    client = _client(handler)
    result = await client.submit({"prompt": "   "})
    status = await client.get_status(result.prompt_id)
    assert status.state == "failed"
    assert status.error == "empty_prompt"


@pytest.mark.asyncio
async def test_status_running_while_in_queue_not_yet_in_history() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/history/"):
            return httpx.Response(200, json={})
        if request.url.path == "/queue":
            return httpx.Response(
                200, json={"queue_running": [[0, "p1"]], "queue_pending": []}
            )
        raise AssertionError(f"unexpected request {request.url.path}")

    client = _client(handler)
    status = await client.get_status("p1")
    assert status.state == "running"


@pytest.mark.asyncio
async def test_status_succeeded_downloads_image_and_stores_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/history/p1":
            return httpx.Response(
                200,
                json={
                    "p1": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "imaginv_00001.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=b"\x89PNG-fake-bytes")
        raise AssertionError(f"unexpected request {request.url.path}")

    storage = InMemoryObjectStorage()
    client = _client(handler, storage=storage)
    status = await client.get_status("p1")

    assert status.state == "succeeded"
    assert status.outputs and len(status.outputs) == 1
    object_key = status.outputs[0]["object_key"]
    assert status.outputs[0]["mime_type"] == "image/png"
    stored = await storage.get_object(object_key)
    assert stored == b"\x89PNG-fake-bytes"

    # Second call is served from cache, no further HTTP calls.
    status2 = await client.get_status("p1")
    assert status2 is status


@pytest.mark.asyncio
async def test_status_execution_error_maps_to_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/history/p1":
            return httpx.Response(
                200, json={"p1": {"status": {"status_str": "error"}, "outputs": {}}}
            )
        raise AssertionError(f"unexpected request {request.url.path}")

    client = _client(handler)
    status = await client.get_status("p1")
    assert status.state == "failed"
    assert status.error == "comfy_execution_error"


@pytest.mark.asyncio
async def test_cancel_deletes_pending_job_from_queue() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/queue" and request.method == "GET":
            return httpx.Response(
                200, json={"queue_running": [], "queue_pending": [[0, "p1"]]}
            )
        if request.url.path == "/queue" and request.method == "POST":
            assert json.loads(request.content) == {"delete": ["p1"]}
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request {request.url.path}")

    client = _client(handler)
    await client.cancel("p1")
    assert ("POST", "/queue") in calls


@pytest.mark.asyncio
async def test_health_checks_system_stats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/system_stats"
        return httpx.Response(200, json={})

    client = _client(handler)
    assert await client.health() is True


@pytest.mark.asyncio
async def test_health_false_on_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    assert await client.health() is False


def test_resolve_dimensions_scales_with_resolution_and_stays_multiple_of_8() -> None:
    w1, h1 = _resolve_dimensions("16:9", "1K")
    w2, h2 = _resolve_dimensions("16:9", "2K")
    assert (w1 % 8, h1 % 8) == (0, 0)
    assert w2 == w1 * 2 and h2 == h1 * 2

    # Unknown ratio/resolution falls back to sane defaults instead of raising.
    w, h = _resolve_dimensions("not-a-ratio", "not-a-res")
    assert (w, h) == _resolve_dimensions("1:1", "1K")


def test_resolve_dimensions_qwen_image_family_uses_its_own_table() -> None:
    # Qwen-Image's official 1:1 is 1328x1328, distinct from the generic checkpoint
    # table's 1024x1024 -- confirms the family switch actually changes which table is
    # consulted, not just that both happen to produce square output.
    checkpoint_dims = _resolve_dimensions("1:1", "1K", family="checkpoint")
    qwen_dims = _resolve_dimensions("1:1", "1K", family="qwen_image")
    assert checkpoint_dims == (1024, 1024)
    assert qwen_dims == (1328, 1328)
    assert checkpoint_dims != qwen_dims
