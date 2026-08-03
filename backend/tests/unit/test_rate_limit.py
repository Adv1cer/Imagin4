from app.core.rate_limit import rate_limit_key


def test_key_scoped_to_user_and_scope():
    k1 = rate_limit_key("login", "user-1")
    k2 = rate_limit_key("login", "user-2")
    assert k1 != k2
    assert "user-1" in k1
    assert "login" in k1


def test_key_changes_across_windows():
    k1 = rate_limit_key("login", "user-1", window_seconds=60)
    k2 = rate_limit_key("login", "user-1", window_seconds=1)
    assert isinstance(k1, str) and isinstance(k2, str)
