"""Unit tests for app/adapters/gemini.py:_sanitized_error's classification of
google-genai APIError subclasses -- added 2026-08 after a production poster job failed
with the generic `gemini_error:ServerError` detail, which reads to a customer like a bug
in our own system when the real cause was Gemini's own 503 "high demand" overload. See
_sanitized_error's docstring for why surfacing `.code`/`.status` (structured, not raw
exception text) is safe under the "no raw exception text" rule."""

from __future__ import annotations

from app.adapters.gemini import _sanitized_error


class _FakeAPIError(Exception):
    """Stands in for google.genai.errors.APIError/ServerError/ClientError without
    depending on the real SDK's constructor -- only .code/.status matter here."""

    def __init__(self, code: int | None = None, status: str | None = None) -> None:
        super().__init__(f"{code} {status}")
        self.code = code
        self.status = status


def test_503_unavailable_maps_to_gemini_overloaded():
    exc = _FakeAPIError(code=503, status="UNAVAILABLE")
    assert _sanitized_error(exc) == "gemini_overloaded"


def test_status_unavailable_without_matching_code_still_maps_to_overloaded():
    # Defensive: classify on whichever signal is present, not just the numeric code.
    exc = _FakeAPIError(code=None, status="UNAVAILABLE")
    assert _sanitized_error(exc) == "gemini_overloaded"


def test_429_resource_exhausted_maps_to_gemini_rate_limited():
    exc = _FakeAPIError(code=429, status="RESOURCE_EXHAUSTED")
    assert _sanitized_error(exc) == "gemini_rate_limited"


def test_bare_timeout_error_maps_to_gemini_timeout():
    # Regression (2026-08, Chet's live logs): route_intent's asyncio.wait_for gave up
    # after gemini_request_timeout_s and raised a bare TimeoutError (asyncio.TimeoutError
    # IS builtins.TimeoutError as of Python 3.11+) -- previously fell through to the
    # generic gemini_error:TimeoutError bucket, which chat_router.py's fallback question
    # doesn't treat as an overload, so a slow-Gemini timeout showed the same "I couldn't
    # understand your request" text as a genuinely malformed message.
    assert _sanitized_error(TimeoutError()) == "gemini_timeout"


def test_unrelated_api_error_falls_back_to_generic_class_name():
    exc = _FakeAPIError(code=400, status="INVALID_ARGUMENT")
    assert _sanitized_error(exc) == "gemini_error:_FakeAPIError"


def test_plain_exception_without_code_or_status_falls_back_to_generic():
    assert _sanitized_error(RuntimeError("boom")) == "gemini_error:RuntimeError"


def test_generic_fallback_never_includes_raw_exception_text():
    exc = RuntimeError("secret payload fragment: user email is chet@example.com")
    result = _sanitized_error(exc)
    assert "secret" not in result
    assert "chet@example.com" not in result
    assert result == "gemini_error:RuntimeError"
