import hashlib
from collections.abc import Iterator
from io import BytesIO
from typing import Any

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.client import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.dependencies import get_report_service, get_storage
from app.errors import ReportPersistenceError, StorageUnavailableError
from app.main import create_app
from app.models import Report
from app.services.reports import ReportService
from app.services.storage import UPLOAD_PART_SIZE, S3ObjectStorage

pytestmark = pytest.mark.integration
TEST_BUCKET = "stock-reports-test"


class GuardedBytesIO(BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.read_sizes: list[int] = []

    def read(self, size: int | None = -1, /) -> bytes:
        if size is None or size < 0:
            raise AssertionError("unbounded read is forbidden")
        self.read_sizes.append(size)
        return super().read(size)


class FailingAfterFirstPart:
    def __init__(self) -> None:
        self._first_read = True

    def read(self, size: int, /) -> bytes:
        if self._first_read:
            self._first_read = False
            return b"x" * size
        raise OSError("simulated stream failure")


def clean_bucket(client: Any, bucket: str) -> None:
    listed = client.list_objects_v2(Bucket=bucket)
    objects = [{"Key": item["Key"]} for item in listed.get("Contents", [])]
    if objects:
        client.delete_objects(Bucket=bucket, Delete={"Objects": objects})

    uploads = client.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
    for upload in uploads:
        client.abort_multipart_upload(
            Bucket=bucket,
            Key=upload["Key"],
            UploadId=upload["UploadId"],
        )


@pytest.fixture
def minio_client() -> Iterator[Any]:
    settings = get_settings()
    client: Any = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        client.head_bucket(Bucket=TEST_BUCKET)
    except ClientError:
        client.create_bucket(Bucket=TEST_BUCKET)
    clean_bucket(client, TEST_BUCKET)
    try:
        yield client
    finally:
        clean_bucket(client, TEST_BUCKET)


@pytest.fixture
def minio_storage(minio_client: Any) -> S3ObjectStorage:
    return S3ObjectStorage(Settings(), client=minio_client)


def test_real_multipart_upload_download_and_checksum(
    minio_storage: S3ObjectStorage,
    minio_client: Any,
) -> None:
    content = b"m" * (UPLOAD_PART_SIZE + 257)
    source = GuardedBytesIO(content)

    result = minio_storage.upload_stream(TEST_BUCKET, "integration/multipart.csv", source)
    downloaded = b"".join(minio_storage.download_stream(TEST_BUCKET, "integration/multipart.csv"))

    assert downloaded == content
    assert result.size_bytes == len(content)
    assert result.checksum_sha256 == hashlib.sha256(content).hexdigest()
    assert minio_client.head_object(Bucket=TEST_BUCKET, Key="integration/multipart.csv")
    assert all(0 < size <= UPLOAD_PART_SIZE for size in source.read_sizes)


def test_real_upload_failure_leaves_no_object_or_multipart(
    minio_storage: S3ObjectStorage,
    minio_client: Any,
) -> None:
    key = "integration/failed.csv"
    with pytest.raises(StorageUnavailableError):
        minio_storage.upload_stream(TEST_BUCKET, key, FailingAfterFirstPart())

    with pytest.raises(ClientError):
        minio_client.head_object(Bucket=TEST_BUCKET, Key=key)
    uploads = minio_client.list_multipart_uploads(Bucket=TEST_BUCKET).get("Uploads", [])
    assert all(upload["Key"] != key for upload in uploads)


def test_real_compensating_delete_removes_unregistered_object(
    minio_storage: S3ObjectStorage,
    minio_client: Any,
) -> None:
    def failing_session_factory() -> Session:
        raise RuntimeError("simulated database failure")

    service = ReportService(minio_storage, failing_session_factory, TEST_BUCKET)
    with pytest.raises(ReportPersistenceError):
        service.register_original(GuardedBytesIO(b"uploaded"), "report.csv")

    assert minio_client.list_objects_v2(Bucket=TEST_BUCKET).get("KeyCount") == 0


@pytest.mark.parametrize("report_status", ["pending", "completed", "failed"])
def test_original_download_for_terminal_and_pending_statuses(
    report_status: str,
    minio_storage: S3ObjectStorage,
    minio_client: Any,
    test_session_factory: sessionmaker[Session],
) -> None:
    content = f"content-{report_status}".encode()
    application: FastAPI = create_app()
    service = ReportService(minio_storage, test_session_factory, TEST_BUCKET)
    application.dependency_overrides[get_storage] = lambda: minio_storage
    application.dependency_overrides[get_report_service] = lambda: service

    with TestClient(application) as client:
        created_response = client.post(
            "/api/v1/reports",
            files={"file": ("оригинал.csv", content, "text/csv")},
        )
        assert created_response.status_code == 202
        created = created_response.json()
        report_id = created["id"]
        assert isinstance(report_id, int)

        with test_session_factory() as session, session.begin():
            report = session.get(Report, report_id)
            assert report is not None
            report.status = report_status
            object_key = report.object_key

        assert minio_client.head_object(Bucket=TEST_BUCKET, Key=object_key)
        assert created["checksum_sha256"] == hashlib.sha256(content).hexdigest()

        download = client.get(f"/api/v1/reports/{report_id}/original")

    assert download.status_code == 200
    assert download.content == content
