import hashlib
from collections.abc import Iterator

from app.errors import FileTooLargeError, ObjectNotFoundError, StorageUnavailableError
from app.services.storage import MAX_UPLOAD_BYTES, ObjectStorage, ReadableStream, UploadResult


class InMemoryStorage(ObjectStorage):
    def __init__(self, *, chunk_size: int = 4, fail_upload: bool = False) -> None:
        self.chunk_size = chunk_size
        self.fail_upload = fail_upload
        self.objects: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []
        self.read_sizes: list[int] = []

    def upload_stream(
        self,
        bucket: str,
        key: str,
        source: ReadableStream,
        *,
        max_bytes: int = MAX_UPLOAD_BYTES,
    ) -> UploadResult:
        if self.fail_upload:
            raise StorageUnavailableError

        content = bytearray()
        checksum = hashlib.sha256()
        while True:
            self.read_sizes.append(self.chunk_size)
            chunk = source.read(self.chunk_size)
            if not chunk:
                break
            if len(content) + len(chunk) > max_bytes:
                raise FileTooLargeError
            content.extend(chunk)
            checksum.update(chunk)

        self.objects[(bucket, key)] = bytes(content)
        return UploadResult(size_bytes=len(content), checksum_sha256=checksum.hexdigest())

    def download_stream(self, bucket: str, key: str) -> Iterator[bytes]:
        try:
            content = self.objects[(bucket, key)]
        except KeyError as exc:
            raise ObjectNotFoundError from exc

        def chunks() -> Iterator[bytes]:
            for start in range(0, len(content), self.chunk_size):
                yield content[start : start + self.chunk_size]

        return chunks()

    def delete_object(self, bucket: str, key: str) -> None:
        self.deleted.append((bucket, key))
        self.objects.pop((bucket, key), None)

    def is_available(self, bucket: str) -> bool:
        return True
