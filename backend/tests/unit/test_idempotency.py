from app.domain.jobs.idempotency import (
    IdempotencyOutcome,
    canonical_payload_hash,
    check_idempotency,
)


def test_new_when_no_existing_job():
    result = check_idempotency(None, None, {"prompt": "a cat"})
    assert result.outcome == IdempotencyOutcome.NEW


def test_replay_when_same_payload():
    payload = {"prompt": "a cat", "steps": 20}
    h = canonical_payload_hash(payload)
    result = check_idempotency("job-1", h, payload)
    assert result.outcome == IdempotencyOutcome.REPLAY
    assert result.existing_job_id == "job-1"


def test_conflict_when_different_payload():
    h = canonical_payload_hash({"prompt": "a cat"})
    result = check_idempotency("job-1", h, {"prompt": "a dog"})
    assert result.outcome == IdempotencyOutcome.CONFLICT


def test_hash_is_order_independent():
    a = canonical_payload_hash({"a": 1, "b": 2})
    b = canonical_payload_hash({"b": 2, "a": 1})
    assert a == b
