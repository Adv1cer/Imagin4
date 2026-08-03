"""Worker selection: filtering + scoring. Lowest score wins."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STALE_HEARTBEAT_SECONDS = 30


@dataclass(frozen=True)
class WorkerSnapshot:
    worker_id: str
    status: str  # online/draining/offline
    capabilities: frozenset[str]
    max_slots: int
    reserved_slots: int
    running_slots: int
    local_queue_depth: int
    last_heartbeat_at: datetime
    recent_failure_rate: float  # 0..1
    current_model_loaded: str | None


def is_eligible(w: WorkerSnapshot, required_capability: str, now: datetime) -> bool:
    if w.status != "online":
        return False
    if required_capability not in w.capabilities:
        return False
    if (now - w.last_heartbeat_at) > timedelta(seconds=STALE_HEARTBEAT_SECONDS):
        return False
    if w.reserved_slots + w.running_slots >= w.max_slots:
        return False
    return True


def score(w: WorkerSnapshot, target_model: str) -> float:
    slot_utilization = (w.reserved_slots + w.running_slots) / max(w.max_slots, 1)
    local_queue_penalty = 0.1 * w.local_queue_depth
    model_switch_penalty = 0.0 if w.current_model_loaded == target_model else 0.5
    recent_failure_penalty = 2.0 * w.recent_failure_rate
    return slot_utilization + local_queue_penalty + model_switch_penalty + recent_failure_penalty


def select_worker(
    workers: list[WorkerSnapshot],
    required_capability: str,
    target_model: str,
    now: datetime | None = None,
) -> WorkerSnapshot | None:
    now = now or datetime.now(timezone.utc)
    eligible = [w for w in workers if is_eligible(w, required_capability, now)]
    if not eligible:
        return None
    return min(eligible, key=lambda w: score(w, target_model))
