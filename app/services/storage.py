import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

import boto3  # type: ignore[import-untyped]
from botocore.client import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from app.config import Settings
from app.errors import FileTooLargeError, ObjectNotFoundError, StorageUnavailableError

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 1_073_741_824
UPLOAD_PART_SIZE = 8 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class ReadableStream(Protocol):
    def read(self, size: int, /) -> bytes: ...


@dataclass(frozen=True, slots=True)
class UploadResult:
    size_bytes: int
    checksum_sha256: str


class ObjectStorage(Protocol):
    def upload_stream(
        self,
        bucket: str,
        key: str,
        source: ReadableStream,
        *,
        max_bytes: int = MAX_UPLOAD_BYTES,
    ) -> UploadResult: ...

    def download_stream(self, bucket: str, key: str) -> Iterator[bytes]: ...

    def delete_object(self, bucket: str, key: str) -> None: ...

    def is_available(self, bucket: str) -> bool: ...


class S3ObjectStorage:
    """Small boto3 adapter for bounded multipart upload and streamed download."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._client: Any = client or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=60,
                retries={"max_attempts": 2, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )

    def upload_stream(
        self,
        bucket: str,
        key: str,
        source: ReadableStream,
        *,
        max_bytes: int = MAX_UPLOAD_BYTES,
    ) -> UploadResult:
        checksum = hashlib.sha256()
        first_part = self._read_part(source)

        if not first_part:
            try:
                self._client.put_object(Bucket=bucket, Key=key, Body=b"")
            except (BotoCoreError, ClientError) as exc:
                raise StorageUnavailableError from exc
            return UploadResult(size_bytes=0, checksum_sha256=checksum.hexdigest())

        upload_id: str | None = None
        try:
            response = self._client.create_multipart_upload(Bucket=bucket, Key=key)
            upload_id = str(response["UploadId"])
            parts: list[dict[str, object]] = []
            total_size = 0
            part_number = 1
            part = first_part

            while part:
                total_size += len(part)
                if total_size > max_bytes:
                    raise FileTooLargeError

                checksum.update(part)
                uploaded = self._client.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=part,
                )
                parts.append({"ETag": str(uploaded["ETag"]), "PartNumber": part_number})
                part_number += 1
                part = self._read_part(source)

            self._client.complete_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            return UploadResult(size_bytes=total_size, checksum_sha256=checksum.hexdigest())
        except FileTooLargeError:
            self._abort_multipart_upload(bucket, key, upload_id)
            raise
        except Exception as exc:
            self._abort_multipart_upload(bucket, key, upload_id)
            if isinstance(exc, StorageUnavailableError):
                raise
            raise StorageUnavailableError from exc

    def download_stream(self, bucket: str, key: str) -> Iterator[bytes]:
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in {"404", "NoSuchBucket", "NoSuchKey", "NotFound"}:
                raise ObjectNotFoundError from exc
            raise StorageUnavailableError from exc
        except BotoCoreError as exc:
            raise StorageUnavailableError from exc

        body: Any = response["Body"]

        def chunks() -> Iterator[bytes]:
            try:
                while chunk := body.read(DOWNLOAD_CHUNK_SIZE):
                    yield bytes(chunk)
            except Exception as exc:
                raise StorageUnavailableError from exc
            finally:
                body.close()

        return chunks()

    def delete_object(self, bucket: str, key: str) -> None:
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageUnavailableError from exc

    def is_available(self, bucket: str) -> bool:
        try:
            self._client.head_bucket(Bucket=bucket)
        except (BotoCoreError, ClientError):
            return False
        return True

    @staticmethod
    def _read_part(source: ReadableStream) -> bytes:
        part = bytearray()
        while len(part) < UPLOAD_PART_SIZE:
            requested_size = UPLOAD_PART_SIZE - len(part)
            chunk = source.read(requested_size)
            if not chunk:
                break
            if len(chunk) > requested_size:
                raise StorageUnavailableError
            part.extend(chunk)
        return bytes(part)

    def _abort_multipart_upload(self, bucket: str, key: str, upload_id: str | None) -> None:
        if upload_id is None:
            return
        try:
            self._client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        except Exception:
            logger.exception(
                "Failed to abort multipart upload",
                extra={"event": "multipart_abort_failed", "object_key": key},
            )
