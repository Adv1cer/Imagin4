"""Unit tests for app/adapters/openrouter.py:_sanitized_error's classification of
httpx.HTTPStatusError by status code -- mirrors
tests/unit/test_gemini_error_classification.py's structure/reasoning exactly, applied to
OpenRouter's documented HTTP status codes (429 rate limit, 402 insufficient credits,
401/403 auth, 500/502 upstream failure) instead of google-genai's .code/.status fields."""

from __future__ import annotations

import httpx
import pytest

from app.adapters.openrouter import _sanitized_error


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://openrouter.test/api/v1/images")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"{status_code} error", request=request, response=response)


@pytest.mark.parametrize(
    "status_code,expected",
    [
        (502, "openrouter_overloaded"),
        (429, "openrouter_rate_limited"),
        (402, "openrouter_insufficient_credits"),
        (401, "openrouter_auth_error"),
        (403, "openrouter_auth_error"),
        (500, "openrouter_upstream_error"),
    ],
)
def test_known_status_codes_map_to_specific_sanitized_codes(status_code: int, expected: str) -> None:
    assert _sanitized_error(_http_status_error(status_code)) == expected


def test_unmapped_status_code_falls_back_to_generic_class_name() -> None:
    assert _sanitized_error(_http_status_error(418)) == "openrouter_error:HTTPStatusError"


def test_plain_exception_without_a_response_falls_back_to_generic() -> None:
    assert _sanitized_error(RuntimeError("boom")) == "openrouter_error:RuntimeError"


def test_generic_fallback_never_includes_raw_exception_text() -> None:
    exc = RuntimeError("secret payload fragment: user email is chet@example.com")
    result = _sanitized_error(exc)
    assert "secret" not in result
    assert "chet@example.com" not in result
    assert result == "openrouter_error:RuntimeError"
