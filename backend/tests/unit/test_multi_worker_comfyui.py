"""Unit tests for MultiWorkerComfyUIClient -- round-robin dispatch across several
underlying ComfyUIClient instances, with per-prompt_id ownership tracking so
get_status()/cancel() always ask the SAME worker that accepted the submission (see the
module's docstring for why: ComfyUI's /history is per-process state, not shared)."""

from __future__ import annotations

import pytest

from app.adapters.comfyui import MockComfyUIClient
from app.adapters.comfyui.multi_worker import MultiWorkerComfyUIClient


@pytest.mark.asyncio
async def test_requires_at_least_one_worker():
    with pytest.raises(ValueError):
        MultiWorkerComfyUIClient([])


@pytest.mark.asyncio
async def test_submit_round_robins_across_workers():
    w1 = MockComfyUIClient(polls_to_complete=0)
    w2 = MockComfyUIClient(polls_to_complete=0)
    client = MultiWorkerComfyUIClient([w1, w2])

    r1 = await client.submit({"prompt": "a"})
    r2 = await client.submit({"prompt": "b"})
    r3 = await client.submit({"prompt": "c"})

    # Round-robin: 1st->w1, 2nd->w2, 3rd->w1 again.
    assert r1.prompt_id in w1._payloads
    assert r2.prompt_id in w2._payloads
    assert r3.prompt_id in w1._payloads


@pytest.mark.asyncio
async def test_get_status_routes_back_to_the_owning_worker():
    w1 = MockComfyUIClient(polls_to_complete=0)
    w2 = MockComfyUIClient(polls_to_complete=0)
    client = MultiWorkerComfyUIClient([w1, w2])

    r1 = await client.submit({"prompt": "a"})  # -> w1
    r2 = await client.submit({"prompt": "b"})  # -> w2

    status1 = await client.get_status(r1.prompt_id)
    status2 = await client.get_status(r2.prompt_id)
    assert status1.state == "succeeded"
    assert status2.state == "succeeded"


@pytest.mark.asyncio
async def test_get_status_unknown_prompt_id_fails_safely():
    client = MultiWorkerComfyUIClient([MockComfyUIClient()])
    status = await client.get_status("never-submitted")
    assert status.state == "failed"
    assert status.error == "unknown_prompt_id"


@pytest.mark.asyncio
async def test_cancel_routes_to_owning_worker_and_forgets_it():
    w1 = MockComfyUIClient()
    client = MultiWorkerComfyUIClient([w1])
    r1 = await client.submit({"prompt": "a"})

    await client.cancel(r1.prompt_id)

    status = await client.get_status(r1.prompt_id)
    assert status.state == "failed"
    assert status.error == "cancelled"


@pytest.mark.asyncio
async def test_health_true_if_any_worker_healthy():
    class _AlwaysDown:
        async def health(self):
            return False

    healthy = MockComfyUIClient()
    down = _AlwaysDown()
    client = MultiWorkerComfyUIClient([down, healthy])
    assert await client.health() is True


@pytest.mark.asyncio
async def test_health_false_if_all_workers_unhealthy():
    class _AlwaysDown:
        async def health(self):
            return False

    client = MultiWorkerComfyUIClient([_AlwaysDown(), _AlwaysDown()])
    assert await client.health() is False


@pytest.mark.asyncio
async def test_health_survives_a_worker_raising():
    class _Raises:
        async def health(self):
            raise RuntimeError("connection refused")

    healthy = MockComfyUIClient()
    client = MultiWorkerComfyUIClient([_Raises(), healthy])
    assert await client.health() is True
