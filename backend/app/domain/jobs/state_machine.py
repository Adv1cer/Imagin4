"""Central allowed-transitions map for generation_jobs.state and pure transition logic.

All actual DB transitions must be performed as:
    UPDATE generation_jobs SET state = :new WHERE id = :id AND state = :expected
and must append a job_event row in the same transaction. This module contains no I/O;
it is the single source of truth for "is transition X->Y legal" so both the API layer
and the scheduler/reconciler agree.
"""

from __future__ import annotations

from dataclasses import dataclass

QUEUED = "queued"
ADMITTED = "admitted"
DISPATCHED = "dispatched"
RUNNING = "running"
RETRY_WAIT = "retry_wait"
CANCELLING = "cancelling"
CANCELLED = "cancelled"
SUCCEEDED = "succeeded"
FAILED = "failed"

TERMINAL_STATES = frozenset({CANCELLED, SUCCEEDED, FAILED})

# from_state -> set of legal to_states
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({ADMITTED, CANCELLED, FAILED}),
    ADMITTED: frozenset({DISPATCHED, CANCELLED, FAILED}),
    DISPATCHED: frozenset({RUNNING, CANCELLING, FAILED, RETRY_WAIT}),
    RUNNING: frozenset({SUCCEEDED, RETRY_WAIT, CANCELLING, FAILED}),
    RETRY_WAIT: frozenset({QUEUED, FAILED, CANCELLED}),
    CANCELLING: frozenset({CANCELLED, FAILED}),
    CANCELLED: frozenset(),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
}


class IllegalTransitionError(Exception):
    def __init__(self, from_state: str, to_state: str):
        super().__init__(f"illegal transition: {from_state} -> {to_state}")
        self.from_state = from_state
        self.to_state = to_state


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def can_transition(from_state: str, to_state: str) -> bool:
    if is_terminal(from_state):
        return False
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def assert_transition(from_state: str, to_state: str) -> None:
    if not can_transition(from_state, to_state):
        raise IllegalTransitionError(from_state, to_state)


@dataclass(frozen=True)
class CancelResult:
    """Result of requesting cancellation; idempotent regardless of current state."""

    already_terminal: bool
    new_state: str | None  # None if no state change was needed/applied


def request_cancel(current_state: str) -> CancelResult:
    """Cancellation is always idempotent: terminal states are left alone, queued/admitted
    jobs cancel immediately, dispatched/running jobs move to `cancelling` (the reconciler
    or worker callback later finalizes to `cancelled`, honestly reflecting reality if the
    ComfyUI cancel could not be guaranteed)."""
    if is_terminal(current_state):
        return CancelResult(already_terminal=True, new_state=None)
    if current_state in (QUEUED, ADMITTED):
        return CancelResult(already_terminal=False, new_state=CANCELLED)
    if current_state in (DISPATCHED, RUNNING):
        return CancelResult(already_terminal=False, new_state=CANCELLING)
    if current_state == RETRY_WAIT:
        return CancelResult(already_terminal=False, new_state=CANCELLED)
    if current_state == CANCELLING:
        return CancelResult(already_terminal=False, new_state=None)
    raise IllegalTransitionError(current_state, "cancel")
