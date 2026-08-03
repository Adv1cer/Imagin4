"""Weighted-fair scheduling and priority-aging helpers (pure functions, DB-free).

The scheduler uses these to compute `effective_priority` for queued/retry_wait jobs.
Higher effective_priority is claimed first. Aging ensures old jobs eventually win even
against higher static-priority newcomers, preventing starvation.
"""

from __future__ import annotations

from datetime import datetime, timezone

PRIORITY_TIERS = {"low": 0, "normal": 10, "high": 20, "urgent": 30}


def effective_priority(
    base_priority: int, queued_at: datetime, now: datetime, aging_increment_per_minute: float = 0.5
) -> float:
    if queued_at.tzinfo is None:
        queued_at = queued_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_minutes = max(0.0, (now - queued_at).total_seconds() / 60.0)
    return base_priority + age_minutes * aging_increment_per_minute


def weighted_round_robin_order(user_queue_depths: dict[str, int]) -> list[str]:
    """Given {user_id: number_of_queued_jobs}, return a fair visiting order for admission,
    interleaving users (heavier users get proportionally fewer consecutive turns) rather
    than draining one user's whole backlog before moving to the next."""
    remaining = dict(user_queue_depths)
    order: list[str] = []
    while any(v > 0 for v in remaining.values()):
        for user_id in sorted(remaining):
            if remaining[user_id] > 0:
                order.append(user_id)
                remaining[user_id] -= 1
    return order
