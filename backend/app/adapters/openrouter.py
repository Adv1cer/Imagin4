"""OpenRouter (https://openrouter.ai/) image-generation adapter.

`OpenRouterImageComfyUIClient` implements the same `ComfyUIClient` port as
`app.adapters.gemini.GeminiImageComfyUIClient` -- it's the second, switchable backend for
"Poster / Infographic" generation (see Settings.image_provider, app/core/config.py, and
CompositeComfyUIClient in app/adapters/routing_comfyui.py). Deliberately mirrors
GeminiImageComfyUIClient's shape closely (same submit()/get_status()/cancel()/health()
behavior, same prompt_designer hook, same "submit does the whole synchronous
generate-and-store sequence, get_status just looks up the cached terminal ComfyStatus"
pattern) so swapping image_provider doesn't change scheduler/reconciler behavior at all.

Unlike Gemini (google-genai SDK, sync, offloaded via asyncio.to_thread), OpenRouter's
Image API (POST {base_url}/images) is a plain REST/JSON endpoint, so this uses httpx.AsyncClient
directly -- no thread offload needed. Response images come back as base64
(`data[].b64_json`), not URLs (confirmed via
https://openrouter.ai/docs/guides/overview/multimodal/image-generation, 2026-08).

Billing is documented as all-or-nothing: a failed generation errors out (commonly 502
Bad Gateway per OpenRouter's docs) and is not billed -- there's no "partially generated,
partially billed" state to reconcile here.
"""

from __future__ import annotations

import base64
import logging
import uuid

import httpx

from app.adapters.comfyui import ComfyStatus, ComfySubmitResult
from app.adapters.gemini import _exact_text_block
from app.adapters.storage import ObjectStorage

logger = logging.getLogger("imaginv.openrouter")


def _sanitized_error(exc: Exception) -> str:
    """Mirrors app.adapters.gemini._sanitized_error's reasoning exactly: never echo raw
    exception/response text back to clients or job_events, but do surface a few specific,
    high-value classifications from OpenRouter's own controlled HTTP status codes (not
    raw body text) so the customer-facing message attributes the failure correctly
    instead of reading like a bug in our own system.

    OpenRouter's Image API docs (2026-08) document, among others: 429 rate limited, 402
    insufficient credits, 401/403 auth problems, and 500/502 upstream failures -- 502
    specifically for a failed/unbillable generation, the closest OpenRouter equivalent of
    Gemini's 503 UNAVAILABLE "high demand" case that prompted this whole classification
    pattern in the first place (see app/adapters/gemini.py's _sanitized_error docstring)."""
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code == 502:
        return "openrouter_overloaded"
    if status_code == 429:
        return "openrouter_rate_limited"
    if status_code == 402:
        return "openrouter_insufficient_credits"
    if status_code in (401, 403):
        return "openrouter_auth_error"
    if status_code == 500:
        return "openrouter_upstream_error"
    return f"openrouter_error:{type(exc).__name__}"


class OpenRouterImageComfyUIClient:
    """Drop-in alternative to GeminiImageComfyUIClient: implements the `ComfyUIClient`
    port but generates the image via OpenRouter's unified Image API instead of Gemini
    directly. See Settings.image_provider / app/main.py for how this gets wired in
    instead of (never alongside) the Gemini image client."""

    def __init__(
        self,
        api_key: str,
        model: str,
        storage: ObjectStorage,
        timeout_s: float = 90.0,
        base_url: str = "https://openrouter.ai/api/v1",
        prompt_designer=None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._storage = storage
        self._timeout_s = timeout_s
        self._base_url = base_url.rstrip("/")
        self._results: dict[str, ComfyStatus] = {}
        # Only ever set in tests (httpx.MockTransport) -- see
        # app/adapters/comfyui/live.py's identical pattern. None in production, which
        # means httpx.AsyncClient uses its normal real-network transport.
        self._transport = transport
        # Optional async callable (prompt: str, exact_text: list[str], kind: str) -> str
        # -- normally GeminiTextClient.design_image_prompt, wired from app/main.py, same
        # as GeminiImageComfyUIClient's prompt_designer. Text/chat/routing always stays on
        # Gemini regardless of image_provider (see Settings.image_provider's comment), so
        # this best-effort "design a better prompt first" step is available here too.
        self._prompt_designer = prompt_designer

    async def submit(self, workflow_payload: dict, kind: str | None = None) -> ComfySubmitResult:
        prompt_id = str(uuid.uuid4())
        base_prompt = str(workflow_payload.get("prompt") or "").strip()
        exact_text = [
            t.strip()
            for t in (workflow_payload.get("exact_text") or [])
            if isinstance(t, str) and t.strip()
        ]

        if not base_prompt and not exact_text:
            self._results[prompt_id] = ComfyStatus(
                prompt_id=prompt_id, state="failed", error="empty_prompt"
            )
            return ComfySubmitResult(prompt_id=prompt_id)

        # Same reasoning as GeminiImageComfyUIClient.submit(): "kind" is the workflow
        # identifier (e.g. "poster_infographic"), prefer the job's own action_type
        # ("poster"/"infographic") for the design instruction's phrasing.
        design_kind = str(workflow_payload.get("action_type") or "poster")

        prompt_text = base_prompt
        if self._prompt_designer is not None:
            try:
                designed = await self._prompt_designer(base_prompt, exact_text, design_kind)
                if designed and designed.strip():
                    prompt_text = designed.strip()
            except Exception as exc:
                logger.info(
                    "openrouter image: prompt-design step failed (%s), using baseline "
                    "prompt prompt_id=%s",
                    type(exc).__name__,
                    prompt_id,
                )
        # Applied unconditionally, same belt-and-suspenders reasoning as
        # GeminiImageComfyUIClient -- guarantees exact_text survives even if the design
        # step's own instruction-following slips.
        prompt_text = prompt_text + _exact_text_block(exact_text)

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/images",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._model, "prompt": prompt_text},
                )
                response.raise_for_status()
                payload = response.json()

            images = payload.get("data") or []
            outputs = []
            for image in images:
                b64_data = image.get("b64_json")
                if not b64_data:
                    continue
                mime = image.get("media_type") or image.get("mime_type") or "image/png"
                ext = (mime.split("/")[-1] or "png").split("+")[0]
                raw_bytes = base64.b64decode(b64_data)
                object_key = f"generated/{prompt_id}.{ext}"
                await self._storage.put_object(object_key, raw_bytes, mime)
                outputs.append({"object_key": object_key, "mime_type": mime})

            if not outputs:
                self._results[prompt_id] = ComfyStatus(
                    prompt_id=prompt_id, state="failed", error="openrouter_no_image_in_response"
                )
            else:
                self._results[prompt_id] = ComfyStatus(
                    prompt_id=prompt_id, state="succeeded", outputs=outputs
                )
                logger.info(
                    "openrouter: image generated prompt_id=%s object_key=%s",
                    prompt_id,
                    outputs[0]["object_key"],
                )
        except Exception as exc:
            logger.exception("openrouter image generation failed prompt_id=%s", prompt_id)
            self._results[prompt_id] = ComfyStatus(
                prompt_id=prompt_id, state="failed", error=_sanitized_error(exc)
            )

        return ComfySubmitResult(prompt_id=prompt_id)

    async def get_status(self, prompt_id: str) -> ComfyStatus:
        cached = self._results.get(prompt_id)
        if cached is not None:
            return cached
        # submit() always populates a result (success or failure) before returning, so
        # reaching here means an unknown prompt_id -- same reasoning as
        # GeminiImageComfyUIClient.get_status().
        return ComfyStatus(prompt_id=prompt_id, state="failed", error="unknown_prompt_id")

    async def cancel(self, prompt_id: str) -> None:
        # OpenRouter's image call has already completed synchronously by the time any
        # client could observe the job as cancellable -- nothing in-flight to cancel.
        self._results.pop(prompt_id, None)

    async def health(self) -> bool:
        # Deliberately does not call the OpenRouter API (readiness checks must be cheap
        # and shouldn't burn billed image-generation quota) -- just confirms the client
        # was constructed with a key, which app/main.py only does when one is configured.
        return True
