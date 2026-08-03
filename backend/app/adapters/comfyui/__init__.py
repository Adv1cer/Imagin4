"""ComfyUIClient port + a deterministic mock adapter for tests/local dev."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ComfySubmitResult:
    prompt_id: str


@dataclass
class ComfyStatus:
    prompt_id: str
    state: str  # "running" | "succeeded" | "failed"
    outputs: list[dict] | None = None
    error: str | None = None


class ComfyUIClient(Protocol):
    async def submit(self, workflow_payload: dict) -> ComfySubmitResult: ...

    async def get_status(self, prompt_id: str) -> ComfyStatus: ...

    async def cancel(self, prompt_id: str) -> None: ...

    async def health(self) -> bool: ...


class MockComfyUIClient:
    """Deterministic mock: every submit "completes" after a configurable number of polls
    and produces a fake output keyed by a hash of the input payload, so the same input
    always produces the same fake asset key -- useful for idempotency tests."""

    def __init__(self, polls_to_complete: int = 0, fail_keyword: str = "__force_fail__") -> None:
        self.polls_to_complete = polls_to_complete
        self.fail_keyword = fail_keyword
        self._poll_counts: dict[str, int] = {}
        self._payloads: dict[str, dict] = {}
        self._cancelled: set[str] = set()

    async def submit(self, workflow_payload: dict) -> ComfySubmitResult:
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
        return ComfyStatus(
            prompt_id=prompt_id,
            state="succeeded",
            outputs=[{"object_key": f"generated/{digest}.png", "mime_type": "image/png"}],
        )

    async def cancel(self, prompt_id: str) -> None:
        self._cancelled.add(prompt_id)

    async def health(self) -> bool:
        return True
