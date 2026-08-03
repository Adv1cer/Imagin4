from datetime import datetime, timedelta, timezone

from app.domain.jobs.fairness import effective_priority, weighted_round_robin_order


def test_aging_increases_effective_priority_over_time():
    now = datetime.now(timezone.utc)
    queued_at = now - timedelta(minutes=10)
    p = effective_priority(
        base_priority=10, queued_at=queued_at, now=now, aging_increment_per_minute=0.5
    )
    assert p == 10 + 10 * 0.5


def test_old_low_priority_job_can_overtake_new_high_priority():
    now = datetime.now(timezone.utc)
    old_low = effective_priority(
        0, now - timedelta(minutes=60), now, aging_increment_per_minute=0.5
    )
    new_high = effective_priority(20, now, now, aging_increment_per_minute=0.5)
    assert old_low > new_high


def test_weighted_round_robin_interleaves_users():
    order = weighted_round_robin_order({"a": 3, "b": 1})
    assert order[0] != order[1] or order.count("a") == 3  # interleaved, not fully drained first
    assert order.count("a") == 3
    assert order.count("b") == 1
    # b's single job should not be stuck at the very end behind all of a's jobs
    assert order.index("b") < 2
