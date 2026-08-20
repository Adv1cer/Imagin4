"""Unit tests for the 2026-08-20 Retry-After additions:
- app/core/rate_limit.py: seconds_until_window_reset
- app/api/deps.py: check_admission_capacity's 503 and rate_limited()'s 429 now both
  carry a Retry-After header (previously neither did -- see each call site's own
  comment for why that gap mattered, same class of issue CapacityExceededError closed
  for the per-user/global queue caps)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request

from app.api.deps import check_admission_capacity, rate_limited
from app.core.config import Settings
from app.core.rate_limit import AdmissionGate, seconds_until_window_reset


# -- seconds_until_window_reset ---------------------------------------------------


def test_seconds_until_window_reset_is_within_the_window_bounds():
    # Can't pin exact wall-clock alignment in a unit test, but the result must always
    # be a small positive int bounded by the window size (+1 for the round-up).
    result = seconds_until_window_reset(60)
    assert 1 <= result <= 61


def test_seconds_until_window_reset_shrinks_as_time_advances(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 960.0)  # exactly at a 60s boundary
    first = seconds_until_window_reset(60)
    monkeypatch.setattr(time, "time", lambda: 990.0)  # 30s into the SAME window
    second = seconds_until_window_reset(60)
    assert second < first
    assert second == 31  # 60 - 30 + 1


# -- check_admission_capacity (503 + Retry-After) ---------------------------------


def _fake_request(admission_gate=None):
    request = MagicMock(spec=Request)
    request.app.state.admission_gate = admission_gate
    return request


@pytest.mark.asyncio
async def test_admission_capacity_503_carries_configured_retry_after(monkeypatch):
    import app.api.deps as deps_module

    monkeypatch.setattr(
        deps_module, "get_settings", lambda: Settings(admission_gate_retry_after_s=7)
    )
    gate = MagicMock(spec=AdmissionGate)
    gate.try_acquire = AsyncMock(return_value=False)
    request = _fake_request(admission_gate=gate)

    with pytest.raises(HTTPException) as exc_info:
        async for _ in check_admission_capacity(request):
            pass
    assert exc_info.value.status_code == 503
    assert exc_info.value.headers["Retry-After"] == "7"


@pytest.mark.asyncio
async def test_admission_capacity_no_gate_is_a_noop():
    request = _fake_request(admission_gate=None)
    ran = False
    async for _ in check_admission_capacity(request):
        ran = True
    assert ran


# -- rate_limited() (429 + Retry-After) -------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_429_carries_retry_after_header(monkeypatch):
    import app.api.deps as deps_module

    monkeypatch.setattr(deps_module, "check_rate_limit", AsyncMock(return_value=False))
    monkeypatch.setattr(deps_module, "seconds_until_window_reset", lambda window_seconds: 42)
    monkeypatch.setattr(deps_module, "get_settings", lambda: Settings(rl_generation_per_min=10))

    dependency = rate_limited("generation", "rl_generation_per_min")
    request = MagicMock(spec=Request)
    request.app.state.redis = None
    user = MagicMock()
    user.id = "u1"

    with pytest.raises(HTTPException) as exc_info:
        await dependency(request, user)
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["Retry-After"] == "42"


@pytest.mark.asyncio
async def test_rate_limited_allowed_request_raises_nothing(monkeypatch):
    import app.api.deps as deps_module

    monkeypatch.setattr(deps_module, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(deps_module, "get_settings", lambda: Settings(rl_generation_per_min=10))

    dependency = rate_limited("generation", "rl_generation_per_min")
    request = MagicMock(spec=Request)
    request.app.state.redis = None
    user = MagicMock()
    user.id = "u1"

    await dependency(request, user)  # must not raise
