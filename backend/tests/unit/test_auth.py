from datetime import datetime, timedelta, timezone

from app.domain.auth.passwords import hash_password, verify_password
from app.domain.auth.sessions import hash_token, is_session_valid, issue_session


def test_password_hash_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong password", h) is False


def test_password_hash_never_equals_plaintext():
    h = hash_password("secret123")
    assert h != "secret123"


def test_session_token_is_hashed_not_stored_raw():
    issued = issue_session(ttl_hours=12)
    assert issued.raw_token != issued.token_hash
    assert hash_token(issued.raw_token) == issued.token_hash


def test_session_valid_when_not_expired_or_revoked():
    issued = issue_session(ttl_hours=1)
    assert is_session_valid(issued.expires_at, None) is True


def test_session_invalid_when_revoked():
    issued = issue_session(ttl_hours=1)
    assert is_session_valid(issued.expires_at, revoked_at=datetime.now(timezone.utc)) is False


def test_session_invalid_when_expired():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert is_session_valid(past, None) is False
