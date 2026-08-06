"""Pure eligibility rules for confirming/cancelling a PendingAction (see
app/db/models.py:PendingAction, app/api/v1/chat_router.py). Operates on a read-only
snapshot dataclass rather than the ORM row directly so these rules stay unit-testable
without a database -- see tests/unit/test_pending_actions.py.

The actual state TRANSITION (the thing that has to be atomic/race-free) is a conditional
`UPDATE ... WHERE status='pending' AND expires_at > now()` in chat_router.py, mirroring
the compare-and-set pattern already used for job state transitions elsewhere in this
codebase. This module only answers "would confirming/cancelling this row be legitimate
right now" -- used both to produce a precise error before attempting the transition, and
directly by unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ConfirmOutcome(str, Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    WRONG_OWNER = "wrong_owner"
    ALREADY_CONFIRMED = "already_confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PARAMS_CHANGED = "params_changed"


@dataclass(frozen=True)
class PendingActionSnapshot:
    """Read-only view of a PendingAction row, decoupled from the SQLAlchemy model."""

    id: str
    user_id: str
    conversation_id: str
    status: str
    expires_at: datetime
    params_fingerprint: str
    resulting_job_id: str | None


def evaluate_confirmation(
    snapshot: PendingActionSnapshot | None,
    requesting_user_id: str,
    now: datetime,
    expected_params_fingerprint: str | None = None,
) -> ConfirmOutcome:
    """Pure eligibility check -- does NOT mutate anything. `expected_params_fingerprint`
    is optional: pass it when the caller wants to detect "the normalized parameters this
    confirmation was issued for are not the ones being confirmed" (e.g. a future edit
    flow); the current chat_router.py confirm endpoint always confirms exactly the stored
    parameters, so it omits this and relies on status/ownership/expiry alone -- the field
    exists so that invariant is enforceable the moment such a flow is added, rather than
    requiring a schema change later."""
    if snapshot is None:
        return ConfirmOutcome.NOT_FOUND
    if snapshot.user_id != requesting_user_id:
        return ConfirmOutcome.WRONG_OWNER
    if snapshot.status == "cancelled":
        return ConfirmOutcome.CANCELLED
    if snapshot.status == "confirmed":
        return ConfirmOutcome.ALREADY_CONFIRMED
    if snapshot.status == "expired" or snapshot.expires_at <= now:
        return ConfirmOutcome.EXPIRED
    if (
        expected_params_fingerprint is not None
        and expected_params_fingerprint != snapshot.params_fingerprint
    ):
        return ConfirmOutcome.PARAMS_CHANGED
    return ConfirmOutcome.OK


def evaluate_cancellation(
    snapshot: PendingActionSnapshot | None, requesting_user_id: str
) -> ConfirmOutcome:
    """Same shape of outcome enum as confirmation (a subset applies): NOT_FOUND,
    WRONG_OWNER, ALREADY_CONFIRMED (can't cancel something already executed), CANCELLED
    (idempotent -- already cancelled is not an error), or OK."""
    if snapshot is None:
        return ConfirmOutcome.NOT_FOUND
    if snapshot.user_id != requesting_user_id:
        return ConfirmOutcome.WRONG_OWNER
    if snapshot.status == "confirmed":
        return ConfirmOutcome.ALREADY_CONFIRMED
    return ConfirmOutcome.OK
