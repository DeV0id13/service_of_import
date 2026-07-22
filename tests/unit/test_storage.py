import hashlib
from io import BytesIO
from typing import Any

import pytest

from app.config import Settings
from app.errors import FileTooLargeError, StorageUnavailableError
from app.services.storage import UPLOAD_PART_SIZE, S3ObjectStorage


class GuardedStream(BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.requested_sizes: list[int] = []

    def read(self, size: int | None = -1, /) -> bytes:
        if size is None or size < 0:
            raise AssertionError("unbounded read is forbidden")
        self.requested_sizes.append(size)
        return super().read(size)


class FakeBody(BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.requested_sizes: list[int] = []
        self.was_closed = False

    def read(self, size: int | None = -1, /) -> bytes:
        if size is None or size < 0:
            raise AssertionError("unbounded read is forbidden")
        self.requested_sizes.append(size)
        return super().read(size)

    def close(self) -> None:
        self.was_closed = True
        super().close()


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.parts: dict[int, bytes] = {}
        self.aborted_uploads: list[str] = []
        self.completed_uploads: list[str] = []
        self.fail_part = False
        self.last_body: FakeBody | None = None

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, object]:
        self.objects[(Bucket, Key)] = Body
        return {}

    def create_multipart_upload(self, *, Bucket: str, Key: str) -> dict[str, str]:
        return {"UploadId": "upload-1"}

    def upload_part(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumber: int,
        Body: bytes,
    ) -> dict[str, str]:
        if self.fail_part:
            raise RuntimeError("simulated upload failure")
        self.parts[PartNumber] = Body
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, Any],
    ) -> dict[str, object]:
        self.objects[(Bucket, Key)] = b"".join(self.parts[number] for number in sorted(self.parts))
        self.completed_uploads.append(UploadId)
        return {}

    def abort_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
    ) -> dict[str, object]:
        self.aborted_uploads.append(UploadId)
        self.parts.clear()
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.last_body = FakeBody(self.objects[(Bucket, Key)])
        return {"Body": self.last_body}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.objects.pop((Bucket, Key), None)
        return {}

    def head_bucket(self, *, Bucket: str) -> dict[str, object]:
        return {}


@pytest.fixture
def fake_client() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture
def storage(fake_client: FakeS3Client) -> S3ObjectStorage:
    return S3ObjectStorage(Settings(), client=fake_client)


def test_multipart_upload_uses_bounded_reads_and_sha256(
    storage: S3ObjectStorage,
    fake_client: FakeS3Client,
) -> None:
    content = b"a" * (UPLOAD_PART_SIZE + 123)
    source = GuardedStream(content)

    result = storage.upload_stream("bucket", "key", source)

    assert result.size_bytes == len(content)
    assert result.checksum_sha256 == hashlib.sha256(content).hexdigest()
    assert fake_client.objects[("bucket", "key")] == content
    assert [len(fake_client.parts[number]) for number in sorted(fake_client.parts)] == [
        UPLOAD_PART_SIZE,
        123,
    ]
    assert source.requested_sizes
    assert all(0 < size <= UPLOAD_PART_SIZE for size in source.requested_sizes)


def test_empty_upload_uses_no_multipart(
    storage: S3ObjectStorage,
    fake_client: FakeS3Client,
) -> None:
    result = storage.upload_stream("bucket", "empty", GuardedStream(b""))
    assert result.size_bytes == 0
    assert fake_client.objects[("bucket", "empty")] == b""
    assert fake_client.completed_uploads == []


def test_limit_violation_aborts_multipart(
    storage: S3ObjectStorage,
    fake_client: FakeS3Client,
) -> None:
    with pytest.raises(FileTooLargeError):
        storage.upload_stream("bucket", "large", GuardedStream(b"12345"), max_bytes=4)

    assert fake_client.aborted_uploads == ["upload-1"]
    assert ("bucket", "large") not in fake_client.objects


def test_upload_error_aborts_multipart(
    storage: S3ObjectStorage,
    fake_client: FakeS3Client,
) -> None:
    fake_client.fail_part = True
    with pytest.raises(StorageUnavailableError):
        storage.upload_stream("bucket", "failed", GuardedStream(b"data"))

    assert fake_client.aborted_uploads == ["upload-1"]
    assert ("bucket", "failed") not in fake_client.objects


def test_download_is_bounded_and_closes_body(
    storage: S3ObjectStorage,
    fake_client: FakeS3Client,
) -> None:
    fake_client.objects[("bucket", "key")] = b"download"
    assert b"".join(storage.download_stream("bucket", "key")) == b"download"
    assert fake_client.last_body is not None
    assert fake_client.last_body.was_closed
    assert all(size > 0 for size in fake_client.last_body.requested_sizes)
