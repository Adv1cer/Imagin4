from app.domain.auth.api_keys import (
    KEY_PREFIX,
    generate_raw_api_key,
    hash_api_key,
    is_api_key_active,
    issue_api_key,
)


def test_generated_key_has_the_expected_prefix():
    raw = generate_raw_api_key()
    assert raw.startswith(KEY_PREFIX)


def test_generated_keys_are_unique():
    assert generate_raw_api_key() != generate_raw_api_key()


def test_hash_is_deterministic_and_never_equals_the_raw_key():
    raw = generate_raw_api_key()
    h1 = hash_api_key(raw)
    h2 = hash_api_key(raw)
    assert h1 == h2
    assert h1 != raw


def test_different_keys_hash_differently():
    assert hash_api_key(generate_raw_api_key()) != hash_api_key(generate_raw_api_key())


def test_issue_api_key_hash_matches_hash_of_its_own_raw_key():
    issued = issue_api_key()
    assert issued.raw_key.startswith(KEY_PREFIX)
    assert hash_api_key(issued.raw_key) == issued.key_hash


def test_active_only_when_not_revoked():
    assert is_api_key_active(None) is True
    assert is_api_key_active("2026-08-11T00:00:00+00:00") is False
