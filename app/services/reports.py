import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import (
    FileTooLargeError,
    ObjectNotFoundError,
    OriginalObjectMissingError,
    ReportNotFoundError,
    ReportPersistenceError,
    StorageUnavailableError,
)
from app.models import Report
from app.services.storage import MAX_UPLOAD_BYTES, ObjectStorage, ReadableStream

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReportPage:
    items: list[Report]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ReportDownload:
    report: Report
    chunks: Iterator[bytes]


class ReportService:
    def __init__(
        self,
        storage: ObjectStorage,
        session_factory: Callable[[], Session],
        bucket: str,
        *,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
    ) -> None:
        self._storage = storage
        self._session_factory = session_factory
        self._bucket = bucket
        self._max_upload_bytes = max_upload_bytes

    def register_original(self, source: ReadableStream, filename: str | None) -> Report:
        original_filename = normalize_original_filename(filename)
        object_key = f"reports/{uuid4()}/original.csv"
        logger.info(
            "Original report upload started",
            extra={
                "event": "report_upload_started",
                "stage": "upload",
                "object_key": object_key,
            },
        )

        try:
            upload = self._storage.upload_stream(
                self._bucket,
                object_key,
                source,
                max_bytes=self._max_upload_bytes,
            )
        except (FileTooLargeError, StorageUnavailableError):
            logger.exception(
                "Original report upload failed",
                extra={
                    "event": "report_upload_failed",
                    "stage": "upload",
                    "object_key": object_key,
                },
            )
            raise

        try:
            with self._session_factory() as session, session.begin():
                report = Report(
                    status="pending",
                    original_filename=original_filename,
                    object_bucket=self._bucket,
                    object_key=object_key,
                    size_bytes=upload.size_bytes,
                    checksum_sha256=upload.checksum_sha256,
                )
                session.add(report)
                session.flush()
        except Exception as exc:
            logger.exception(
                "Report registration failed after upload",
                extra={
                    "event": "report_registration_failed",
                    "stage": "database",
                    "object_key": object_key,
                },
            )
            self._compensating_delete(object_key)
            raise ReportPersistenceError from exc

        logger.info(
            "Original report upload completed",
            extra={
                "event": "report_upload_completed",
                "report_id": report.id,
                "stage": "upload",
                "status": report.status,
                "size_bytes": report.size_bytes,
                "object_key": object_key,
            },
        )
        return report

    def list_reports(self, *, status: str | None, limit: int, offset: int) -> ReportPage:
        with self._session_factory() as session, session.begin():
            item_query = select(Report)
            count_query = select(func.count()).select_from(Report)
            if status is not None:
                item_query = item_query.where(Report.status == status)
                count_query = count_query.where(Report.status == status)

            items = list(
                session.scalars(
                    item_query.order_by(Report.created_at.desc(), Report.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            total = session.scalar(count_query)

        return ReportPage(items=items, total=total or 0, limit=limit, offset=offset)

    def get_report(self, report_id: int) -> Report:
        with self._session_factory() as session, session.begin():
            report = session.get(Report, report_id)
            if report is None:
                raise ReportNotFoundError
        return report

    def download_original(self, report_id: int) -> ReportDownload:
        report = self.get_report(report_id)
        try:
            chunks = self._storage.download_stream(report.object_bucket, report.object_key)
        except ObjectNotFoundError as exc:
            logger.exception(
                "Registered original object is missing",
                extra={
                    "event": "report_download_failed",
                    "report_id": report.id,
                    "stage": "download",
                    "object_key": report.object_key,
                },
            )
            raise OriginalObjectMissingError from exc
        except StorageUnavailableError:
            logger.exception(
                "Original report download failed",
                extra={
                    "event": "report_download_failed",
                    "report_id": report.id,
                    "stage": "download",
                    "object_key": report.object_key,
                },
            )
            raise

        return ReportDownload(report=report, chunks=self._logged_chunks(report, chunks))

    def _compensating_delete(self, object_key: str) -> None:
        try:
            self._storage.delete_object(self._bucket, object_key)
        except StorageUnavailableError:
            logger.exception(
                "Compensating object delete failed",
                extra={
                    "event": "compensating_delete_failed",
                    "stage": "cleanup",
                    "object_key": object_key,
                },
            )
        else:
            logger.info(
                "Compensating object delete completed",
                extra={
                    "event": "compensating_delete_completed",
                    "stage": "cleanup",
                    "object_key": object_key,
                },
            )

    @staticmethod
    def _logged_chunks(report: Report, chunks: Iterator[bytes]) -> Iterator[bytes]:
        try:
            yield from chunks
        except Exception:
            logger.exception(
                "Original report stream failed",
                extra={
                    "event": "report_download_failed",
                    "report_id": report.id,
                    "stage": "download",
                    "object_key": report.object_key,
                },
            )
            raise


def normalize_original_filename(filename: str | None) -> str:
    normalized = (filename or "").replace("\\", "/")
    basename = PurePosixPath(normalized).name
    safe_name = "".join(character for character in basename if " " <= character != "\x7f")
    return safe_name[:255] or "original.csv"
