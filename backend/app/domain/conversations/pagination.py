"""Opaque cursor encode/decode for keyset pagination over (sequence_no) or
(updated_at, id) style ordering. Cursor is base64url(json) of the position, never a raw
offset, so pagination stays stable under concurrent inserts."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any


class InvalidCursorError(ValueError):
    pass


@dataclass(frozen=True)
class Cursor:
    sort_key: Any
    id: str

    def encode(self) -> str:
        payload = json.dumps({"k": self.sort_key, "id": self.id}, separators=(",", ":"))
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode(token: str) -> Cursor:
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii"))
            data = json.loads(raw)
            return Cursor(sort_key=data["k"], id=data["id"])
        except Exception as exc:
            raise InvalidCursorError(f"invalid cursor: {token!r}") from exc
