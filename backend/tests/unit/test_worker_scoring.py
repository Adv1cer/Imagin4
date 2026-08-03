from datetime import datetime, timezone

from app.domain.workers.scoring import WorkerSnapshot, is_eligible, select_worker


def make_worker(**overrides):
    base = dict(
        worker_id="w1",
        status="online",
        capabilities=frozenset({"sdxl"}),
        max_slots=2,
        reserved_slots=0,
        running_slots=0,
        local_queue_depth=0,
        last_heartbeat_at=datetime.now(timezone.utc),
        recent_failure_rate=0.0,
        current_model_loaded="sdxl",
    )
    base.update(overrides)
    return WorkerSnapshot(**base)


def test_offline_worker_ineligible():
    w = make_worker(status="offline")
    assert is_eligible(w, "sdxl", datetime.now(timezone.utc)) is False


def test_incapable_worker_ineligible():
    w = make_worker(capabilities=frozenset({"flux"}))
    assert is_eligible(w, "sdxl", datetime.now(timezone.utc)) is False


def test_full_worker_ineligible():
    w = make_worker(reserved_slots=2)
    assert is_eligible(w, "sdxl", datetime.now(timezone.utc)) is False


def test_lower_utilization_worker_wins():
    busy = make_worker(worker_id="busy", running_slots=1)
    idle = make_worker(worker_id="idle", running_slots=0)
    chosen = select_worker([busy, idle], "sdxl", "sdxl")
    assert chosen.worker_id == "idle"


def test_model_switch_penalty_prefers_matching_model_loaded():
    matching = make_worker(worker_id="match", current_model_loaded="sdxl")
    mismatched = make_worker(worker_id="mismatch", current_model_loaded="flux")
    chosen = select_worker([matching, mismatched], "sdxl", "sdxl")
    assert chosen.worker_id == "match"


def test_no_eligible_worker_returns_none():
    assert select_worker([make_worker(status="offline")], "sdxl", "sdxl") is None
