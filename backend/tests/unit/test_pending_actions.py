"""Unit tests for app/domain/chat/pending_actions.py -- the pure confirm/cancel
eligibility rules. Covers invariant #5 (user-bound, action-bound, expiring, single-use)
and #6 (changed parameters invalidate) from the spec, without touching a database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.chat.pending_actions import (
    ConfirmOutcome,
    PendingActionSnapshot,
    evaluate_cancellation,
    evaluate_confirmation,
)

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
OWNER = "user-1"
OTHER_USER = "user-2"


def _snapshot(**overrides) -> PendingActionSnapshot:
    base = dict(
        id="pa-1",
        user_id=OWNER,
        conversation_id="conv-1",
        status="pending",
        expires_at=NOW + timedelta(minutes=5),
        params_fingerprint="fp-1",
        resulting_job_id=None,
    )
    base.update(overrides)
    return PendingActionSnapshot(**base)


def test_missing_snapshot_is_not_found():
    assert evaluate_confirmation(None, OWNER, NOW) == ConfirmOutcome.NOT_FOUND


def test_wrong_owner_cannot_confirm():
    """A confirm request bound to a different user must never succeed, even with the
    correct pending_action_id -- confirmation is user-bound."""
    snap = _snapshot(user_id=OWNER)
    assert evaluate_confirmation(snap, OTHER_USER, NOW) == ConfirmOutcome.WRONG_OWNER


def test_pending_and_unexpired_is_ok():
    snap = _snapshot(status="pending", expires_at=NOW + timedelta(minutes=1))
    assert evaluate_confirmation(snap, OWNER, NOW) == ConfirmOutcome.OK


def test_expired_by_timestamp_is_expired_even_if_status_still_pending():
    """expires_at in the past must be treated as expired regardless of the stored status
    string (a background sweeper marking rows "expired" is not required for correctness
    -- the timestamp itself is authoritative)."""
    snap = _snapshot(status="pending", expires_at=NOW - timedelta(seconds=1))
    assert evaluate_confirmation(snap, OWNER, NOW) == ConfirmOutcome.EXPIRED


def test_explicit_expired_status_is_expired():
    snap = _snapshot(status="expired")
    assert evaluate_confirmation(snap, OWNER, NOW) == ConfirmOutcome.EXPIRED


def test_cancelled_cannot_be_confirmed():
    snap = _snapshot(status="cancelled")
    assert evaluate_confirmation(snap, OWNER, NOW) == ConfirmOutcome.CANCELLED


def test_already_confirmed_is_not_ok_again():
    """Single-use: a second confirm attempt against an already-confirmed row must not
    be treated as OK (the endpoint handles idempotent replay separately by checking
    resulting_job_id, but the eligibility check itself must not say OK twice)."""
    snap = _snapshot(status="confirmed", resulting_job_id="job-123")
    assert evaluate_confirmation(snap, OWNER, NOW) == ConfirmOutcome.ALREADY_CONFIRMED


def test_changed_parameters_invalidate_confirmation():
    snap = _snapshot(params_fingerprint="fp-original")
    outcome = evaluate_confirmation(
        snap, OWNER, NOW, expected_params_fingerprint="fp-different"
    )
    assert outcome == ConfirmOutcome.PARAMS_CHANGED


def test_matching_parameters_do_not_invalidate_confirmation():
    snap = _snapshot(params_fingerprint="fp-same")
    outcome = evaluate_confirmation(snap, OWNER, NOW, expected_params_fingerprint="fp-same")
    assert outcome == ConfirmOutcome.OK


def test_fingerprint_check_is_opt_in():
    """When the caller doesn't pass expected_params_fingerprint, a fingerprint mismatch
    can't occur -- matches chat_router.py's confirm endpoint, which always confirms
    exactly the row's own stored parameters (no client-supplied override to compare
    against)."""
    snap = _snapshot(params_fingerprint="whatever")
    assert evaluate_confirmation(snap, OWNER, NOW) == ConfirmOutcome.OK


# --- Cancellation ---


def test_cancel_missing_snapshot_is_not_found():
    assert evaluate_cancellation(None, OWNER) == ConfirmOutcome.NOT_FOUND


def test_cancel_wrong_owner_rejected():
    snap = _snapshot(user_id=OWNER)
    assert evaluate_cancellation(snap, OTHER_USER) == ConfirmOutcome.WRONG_OWNER


def test_cancel_pending_is_ok():
    snap = _snapshot(status="pending")
    assert evaluate_cancellation(snap, OWNER) == ConfirmOutcome.OK


def test_cancel_already_cancelled_is_idempotent_ok():
    snap = _snapshot(status="cancelled")
    assert evaluate_cancellation(snap, OWNER) == ConfirmOutcome.OK


def test_cancel_confirmed_action_is_rejected():
    """Cannot cancel something that has already executed/is executing."""
    snap = _snapshot(status="confirmed", resulting_job_id="job-123")
    assert evaluate_cancellation(snap, OWNER) == ConfirmOutcome.ALREADY_CONFIRMED
