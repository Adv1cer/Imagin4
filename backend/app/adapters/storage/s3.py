"""S3/MinIO-backed ObjectStorage (see app/adapters/storage/__init__.py's `ObjectStorage`
Protocol). Closes a gap parallel to the old InMemoryJobQueue one (see
app/adapters/queue/postgres.py): `InMemoryObjectStorage` is a plain in-process dict, so
whichever process actually uploaded a generated image (the scheduler/reconciler, once
they're wired to a real ComfyUI/Gemini client -- see app/adapters/comfyui/factory.py) is
the ONLY process that can serve it back. `docker-compose.yml` already runs a `minio`
service and `Settings` already has `s3_*` config (see app/core/config.py), but nothing
implemented this adapter until now -- `app/main.py::_build_state` (and, once wired, the
standalone scheduler/reconciler entrypoints) must construct this instead of
`InMemoryObjectStorage` for `queue_backend=postgres` to actually be safe across
containers.

Uses plain `boto3` (not `aioboto3`/`aiobotocore`) wrapped in `asyncio.to_thread` per
call, deliberately -- aiobotocore's boto3-version pinning is notoriously fragile to keep
in sync, and this adapter's call volume (one or two S3 calls per generation job, not a
hot per-request path) doesn't need a fully async S3 client to avoid blocking the event
loop meaningfully. If per-request S3 latency ever shows up as a real bottleneck in
metrics, revisit with `aioboto3` then -- not speculatively now.

NOT executed against a real MinIO in the sandbox this file was authored in (same caveat
as app/adapters/queue/postgres.py before its integration tests were actually run) --
`tests/integration/test_s3_object_storage.py` uses `testcontainers`'s MinIO container so
it can be verified for real; run it (needs Docker) before trusting this in production.
"""

from __future__ import annotations

import asyncio
import hashlib
from functools import lru_cache

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import Settings


class S3ObjectStorage:
    """Implements the `ObjectStorage` Protocol against any S3-compatible endpoint
    (MinIO in dev/on-prem, real S3 in cloud deployments) via `settings.s3_*`."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        self._default_ttl_s = settings.signed_url_ttl_s
        # path-style addressing (not virtual-hosted-style) is required for MinIO and
        # most non-AWS S3-compatible endpoints -- AWS S3 itself also still accepts it.
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=BotoConfig(s3={"addressing_style": "path"}, signature_version="s3v4"),
        )

    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            # Defense in depth alongside the sha256 already recorded in the `assets`
            # table (see app/db/models.py) -- lets S3/MinIO itself reject a corrupted
            # upload rather than silently storing bad bytes.
            ContentMD5=_content_md5_b64(data),
        )

    async def get_object(self, key: str) -> bytes:
        try:
            response = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise KeyError(f"object not found: {key}") from exc
            raise
        # response["Body"] is a botocore StreamingBody -- .read() is itself blocking I/O,
        # so it also needs to run off the event loop thread, not just the initial call.
        return await asyncio.to_thread(response["Body"].read)

    async def delete_object(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def signed_get_url(self, key: str, ttl_s: int = 300) -> str:
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl_s or self._default_ttl_s,
        )

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise


def _content_md5_b64(data: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.md5(data).digest()).decode("ascii")


@lru_cache
def get_s3_object_storage() -> S3ObjectStorage:
    """Process-wide singleton, mirroring app/db/base.py's `get_engine()`/
    `get_session_factory()` pattern -- avoids re-creating a boto3 client (and its own
    connection pool) per request. Reads Settings via `get_settings()` internally so
    every caller (app/main.py, the standalone scheduler/reconciler entrypoints) gets the
    identical, cached instance without each having to thread a `Settings` object
    through by hand."""
    from app.core.config import get_settings

    return S3ObjectStorage(get_settings())
