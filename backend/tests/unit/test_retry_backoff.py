import random

from app.domain.jobs.retry import BackoffConfig, compute_backoff_seconds, is_retryable


def test_retryable_error_within_attempt_budget():
    assert is_retryable("comfy_timeout", attempt_no=1, max_attempts=3) is True


def test_non_retryable_error_code():
    assert is_retryable("workflow_invalid", attempt_no=1, max_attempts=3) is False


def test_exhausted_attempts_not_retryable():
    assert is_retryable("comfy_timeout", attempt_no=3, max_attempts=3) is False


def test_backoff_grows_and_stays_within_jitter_bounds():
    cfg = BackoffConfig(base_seconds=2.0, factor=2.0, max_seconds=120.0, jitter_ratio=0.2)
    rng = random.Random(42)
    for attempt in range(1, 8):
        raw = min(cfg.base_seconds * (cfg.factor ** (attempt - 1)), cfg.max_seconds)
        lo, hi = raw * 0.8, raw * 1.2
        value = compute_backoff_seconds(attempt, cfg, rng=rng)
        assert lo - 1e-9 <= value <= hi + 1e-9


def test_backoff_capped_at_max():
    cfg = BackoffConfig(base_seconds=2.0, factor=2.0, max_seconds=10.0, jitter_ratio=0.0)
    value = compute_backoff_seconds(20, cfg, rng=random.Random(1))
    assert value == 10.0
