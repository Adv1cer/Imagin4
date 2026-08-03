"""Object storage port + adapters."""

from __future__ import annotations

from typing import Protocol


class ObjectStorage(Protocol):
    """Port for object storage (S3/MinIO in production)."""

    async def put_object(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get_object(self, key: str) -> bytes: ...

    async def delete_object(self, key: str) -> None: ...

    async def signed_get_url(self, key: str, ttl_s: int = 300) -> str: ...

    async def exists(self, key: str) -> bool: ...


class InMemoryObjectStorage:
    """Deterministic in-memory ObjectStorage used by tests and local dev without MinIO."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        self._objects[key] = (data, content_type)

    async def get_object(self, key: str) -> bytes:
        if key not in self._objects:
            raise KeyError(f"object not found: {key}")
        return self._objects[key][0]

    async def delete_object(self, key: str) -> None:
        self._objects.pop(key, None)

    async def signed_get_url(self, key: str, ttl_s: int = 300) -> str:
        return f"https://fake-storage.local/{key}?ttl={ttl_s}"

    async def exists(self, key: str) -> bool:
        return key in self._objects
