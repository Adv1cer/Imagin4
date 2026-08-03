import pytest

from app.domain.jobs.ownership import NotOwnerError, assert_owner


def test_owner_matches_passes():
    assert_owner("user-1", "user-1") is None


def test_owner_mismatch_raises():
    with pytest.raises(NotOwnerError):
        assert_owner("user-1", "user-2")
