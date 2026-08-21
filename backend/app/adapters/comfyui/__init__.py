"""ComfyUIClient port + a deterministic mock adapter for tests/local dev."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.adapters.storage import ObjectStorage

# Smallest possible valid PNG: a 1x1 transparent pixel. Used so MockComfyUIClient's
# fabricated outputs are actually fetchable via GET /v1/jobs/{id}/asset in local/dev mode
# (no live ComfyUI, no GPU) instead of pointing at an object_key that was never written to
# storage -- previously any job routed through the mock adapter would "succeed" but the
# asset endpoint would 404, since the mock only ever invented a plausible-looking key.
_PLACEHOLDER_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c6360000200000500017a5eab3f0000000049454e44ae426082"
)


@dataclass
class ComfySubmitResult:
    prompt_id: str


@dataclass
class ComfyStatus:
    prompt_id: str
    state: str  # "running" | "succeeded" | "failed" | "unknown"
    outputs: list[dict] | None = None
    error: str | None = None


class ComfyUIClient(Protocol):
    async def submit(
        self, workflow_payload: dict, kind: str | None = None
    ) -> ComfySubmitResult: ...

    async def get_status(self, prompt_id: str) -> ComfyStatus: ...

    async def cancel(self, prompt_id: str) -> None: ...

    async def health(self) -> bool: ...


class MockComfyUIClient:
    """Deterministic mock: every submit "completes" after a configurable number of polls
    and produces a fake output keyed by a hash of the input payload, so the same input
    always produces the same fake asset key -- useful for idempotency tests."""

    def __init__(
        self,
        polls_to_complete: int = 0,
        fail_keyword: str = "__force_fail__",
        storage: "ObjectStorage | None" = None,
    ) -> None:
        self.polls_to_complete = polls_to_complete
        self.fail_keyword = fail_keyword
        # Optional: when provided, get_status() actually writes a tiny placeholder PNG to
        # storage under the fabricated object_key the first time a job "succeeds", so GET
        # /v1/jobs/{id}/asset returns real (if trivial) image bytes instead of 404ing on a
        # key nothing ever wrote to. Left None (as before) for tests/entrypoints that only
        # care about job state transitions, not asset retrieval.
        self._storage = storage
        self._poll_counts: dict[str, int] = {}
        self._payloads: dict[str, dict] = {}
        self._cancelled: set[str] = set()
        self._stored_keys: set[str] = set()

    async def submit(self, workflow_payload: dict, kind: str | None = None) -> ComfySubmitResult:
        prompt_id = str(uuid.uuid4())
        self._payloads[prompt_id] = workflow_payload
        self._poll_counts[prompt_id] = 0
        return ComfySubmitResult(prompt_id=prompt_id)

    async def get_status(self, prompt_id: str) -> ComfyStatus:
        if prompt_id in self._cancelled:
            return ComfyStatus(prompt_id=prompt_id, state="failed", error="cancelled")
        payload = self._payloads.get(prompt_id, {})
        if self.fail_keyword in str(payload):
            return ComfyStatus(prompt_id=prompt_id, state="failed", error="forced failure")

        self._poll_counts[prompt_id] = self._poll_counts.get(prompt_id, 0) + 1
        if self._poll_counts[prompt_id] <= self.polls_to_complete:
            return ComfyStatus(prompt_id=prompt_id, state="running")

        digest = hashlib.sha256(str(payload).encode("utf-8")).hexdigest()[:16]
        object_key = f"generated/{digest}.png"
        if self._storage is not None and object_key not in self._stored_keys:
            await self._storage.put_object(object_key, _PLACEHOLDER_PNG_1X1, "image/png")
            self._stored_keys.add(object_key)
        return ComfyStatus(
            prompt_id=prompt_id,
            state="succeeded",
            outputs=[{"object_key": object_key, "mime_type": "image/png"}],
        )

    async def cancel(self, prompt_id: str) -> None:
        self._cancelled.add(prompt_id)

    async def health(self) -> bool:
        return True
