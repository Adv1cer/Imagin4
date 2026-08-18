"""Integration tests for S3ObjectStorage (app/adapters/storage/s3.py) against a REAL
MinIO container, using `testcontainers` (dev dependency -- see pyproject.toml; also
needs the `minio` pip package, added alongside `testcontainers` specifically for this).

NOT executed in the sandbox this file was authored in (no Docker daemon there -- same
caveat as tests/integration/test_postgres_job_queue.py before it was actually run for
real). Run `pytest tests/integration/test_s3_object_storage.py -v` (needs Docker) before
trusting this adapter in production.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("testcontainers")
pytest.importorskip("minio")  # the SDK testcontainers.community.minio uses internally

from testcontainers.community.minio import MinioContainer

from app.adapters.storage.s3 import S3ObjectStorage
from app.core.config import Settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def minio_settings():
    with MinioContainer() as minio:
        config = minio.get_config()
        settings = Settings(
            s3_endpoint_url=f"http://{config['endpoint']}",
            s3_access_key=config["access_key"],
            s3_secret_key=config["secret_key"],
            s3_bucket="imaginv-test",
            s3_region="us-east-1",
        )
        # boto3 doesn't auto-create the bucket -- do it via the same client
        # S3ObjectStorage will use, so this test doesn't depend on a second SDK.
        storage = S3ObjectStorage(settings)
        storage._client.create_bucket(Bucket=settings.s3_bucket)
        yield settings


@pytest.fixture()
def storage(minio_settings) -> S3ObjectStorage:
    return S3ObjectStorage(minio_settings)


@pytest.mark.asyncio
async def test_put_and_get_object_round_trip(storage: S3ObjectStorage):
    key = f"generated/{uuid.uuid4()}.png"
    data = b"\x89PNG fake image bytes for the round trip test"

    await storage.put_object(key, data, content_type="image/png")
    fetched = await storage.get_object(key)

    assert fetched == data


@pytest.mark.asyncio
async def test_get_object_missing_key_raises_keyerror(storage: S3ObjectStorage):
    with pytest.raises(KeyError):
        await storage.get_object(f"generated/{uuid.uuid4()}-does-not-exist.png")


@pytest.mark.asyncio
async def test_exists_true_after_put_false_before(storage: S3ObjectStorage):
    key = f"generated/{uuid.uuid4()}.png"
    assert await storage.exists(key) is False

    await storage.put_object(key, b"data", content_type="application/octet-stream")
    assert await storage.exists(key) is True


@pytest.mark.asyncio
async def test_delete_object_removes_it(storage: S3ObjectStorage):
    key = f"generated/{uuid.uuid4()}.png"
    await storage.put_object(key, b"data", content_type="application/octet-stream")
    assert await storage.exists(key) is True

    await storage.delete_object(key)
    assert await storage.exists(key) is False


@pytest.mark.asyncio
async def test_signed_get_url_is_fetchable(storage: S3ObjectStorage, minio_settings):
    """The real point of a signed URL: an unauthenticated client should be able to GET
    the object directly from it without going through our API/S3 credentials at all."""
    import httpx

    key = f"generated/{uuid.uuid4()}.png"
    data = b"signed url round trip"
    await storage.put_object(key, data, content_type="application/octet-stream")

    url = await storage.signed_get_url(key, ttl_s=60)
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)

    assert resp.status_code == 200
    assert resp.content == data


@pytest.mark.asyncio
async def test_cross_process_visibility_is_the_whole_point(minio_settings):
    """The exact gap this adapter closes (see its module docstring): TWO separate
    S3ObjectStorage instances -- simulating two different containers/processes -- must
    see each other's uploads, unlike InMemoryObjectStorage."""
    writer = S3ObjectStorage(minio_settings)
    reader = S3ObjectStorage(minio_settings)  # fresh instance, no shared Python state
    key = f"generated/{uuid.uuid4()}.png"
    data = b"written by the scheduler process, read by the api process"

    await writer.put_object(key, data, content_type="application/octet-stream")
    fetched = await reader.get_object(key)

    assert fetched == data
