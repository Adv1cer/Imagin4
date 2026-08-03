import pytest

from app.domain.jobs.state_machine import (
    ALLOWED_TRANSITIONS,
    CANCELLED,
    CANCELLING,
    DISPATCHED,
    FAILED,
    QUEUED,
    RETRY_WAIT,
    RUNNING,
    SUCCEEDED,
    IllegalTransitionError,
    assert_transition,
    can_transition,
    is_terminal,
    request_cancel,
)


def test_happy_path_transitions_allowed():
    assert can_transition(QUEUED, "admitted")
    assert can_transition("admitted", DISPATCHED)
    assert can_transition(DISPATCHED, RUNNING)
    assert can_transition(RUNNING, SUCCEEDED)


def test_retry_loop_allowed():
    assert can_transition(RUNNING, RETRY_WAIT)
    assert can_transition(RETRY_WAIT, QUEUED)


def test_terminal_states_immutable():
    for terminal in (CANCELLED, SUCCEEDED, FAILED):
        assert is_terminal(terminal)
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()
        assert not can_transition(terminal, QUEUED)


def test_illegal_transition_raises():
    with pytest.raises(IllegalTransitionError):
        assert_transition(SUCCEEDED, QUEUED)
    with pytest.raises(IllegalTransitionError):
        assert_transition(QUEUED, RUNNING)


def test_cancel_idempotent_from_terminal():
    result = request_cancel(SUCCEEDED)
    assert result.already_terminal is True
    assert result.new_state is None


def test_cancel_queued_goes_direct_to_cancelled():
    result = request_cancel(QUEUED)
    assert result.new_state == CANCELLED


def test_cancel_running_goes_to_cancelling():
    result = request_cancel(RUNNING)
    assert result.new_state == CANCELLING


def test_cancel_already_cancelling_is_noop():
    result = request_cancel(CANCELLING)
    assert result.new_state is None
    assert result.already_terminal is False
