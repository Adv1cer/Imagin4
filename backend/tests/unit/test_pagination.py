import pytest

from app.domain.conversations.pagination import Cursor, InvalidCursorError


def test_cursor_roundtrip():
    c = Cursor(sort_key="2024-01-01T00:00:00Z", id="abc-123")
    token = c.encode()
    decoded = Cursor.decode(token)
    assert decoded == c


def test_invalid_cursor_raises():
    with pytest.raises(InvalidCursorError):
        Cursor.decode("not-valid-base64!!!")
