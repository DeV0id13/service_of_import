import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Connection, Engine, delete, func, insert, literal, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Report, ReportError, ReportStagingRow
from app.services.apply_report import ApplyReportService, ApplyStepHook
from app.services.csv_validation import (
    DEFAULT_ERROR_RAW_TOTAL_CHARS,
    DEFAULT_ERROR_RAW_VALUE_CHARS,
    DEFAULT_MAX_FIELD_CHARS,
    DEFAULT_MAX_RECORD_CHARS,
    ValidatedRow,
    ValidationIssue,
    validate_csv_stream,
)
from app.services.storage import ObjectStorage

logger = logging.getLogger(__name__)


class WorkerCycleOutcome(StrEnum):
    LOCK_NOT_ACQUIRED = "lock_not_acquired"
    NO_REPORT = "no_report"
    VALIDATED = "validated"
    VALIDATION_FAILED = "validation_failed"
    PROCESSING_FAILED = "processing_failed"
    WAITING_FOR_APPLY = "waiting_for_apply"
    COMPLETED = "completed"


class LockConnectionLost(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReportSource:
    id: int
    object_bucket: str
    object_key: str


class ValidationBatchWriter:
    def __init__(
        self,
        report_id: int,
        session_factory: Callable[[], Session],
        batch_size: int,
        assert_lock_alive: Callable[[], None],
    ) -> None:
        self._report_id = report_id
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._assert_lock_alive = assert_lock_alive
        self._staging_batch: list[dict[str, object]] = []
        self._error_batch: list[dict[str, object]] = []

    def add_valid_row(self, row: ValidatedRow) -> None:
        self._staging_batch.append(
            {
                "report_id": self._report_id,
                "line_number": row.line_number,
                "warehouse_code": row.warehouse_code,
                "warehouse_name": row.warehouse_name,
                "sku": row.sku,
                "product_name": row.product_name,
                "quantity": row.quantity,
            }
        )
        if len(self._staging_batch) >= self._batch_size:
            self.flush_staging()

    def add_issues(self, issues: list[ValidationIssue]) -> None:
        for issue in issues:
            self._error_batch.append(
                {
                    "report_id": self._report_id,
                    "line_number": issue.line_number,
                    "field_name": issue.field_name,
                    "code": issue.code,
                    "message": issue.message,
                    "raw_data": issue.raw_data,
                }
            )
            if len(self._error_batch) >= self._batch_size:
                self.flush_errors()

    def flush_all(self) -> None:
        self.flush_staging()
        self.flush_errors()

    def flush_staging(self) -> None:
        if not self._staging_batch:
            return
        batch = self._staging_batch
        self._staging_batch = []
        self._write_batch(ReportStagingRow, batch, "staging")

    def flush_errors(self) -> None:
        if not self._error_batch:
            return
        batch = self._error_batch
        self._error_batch = []
        self._write_batch(ReportError, batch, "errors")

    def _write_batch(
        self,
        model: type[ReportStagingRow] | type[ReportError],
        batch: list[dict[str, object]],
        batch_kind: str,
    ) -> None:
        self._assert_lock_alive()
        with self._session_factory() as session, session.begin():
            session.execute(insert(model), batch)
        logger.info(
            "Validation batch written",
            extra={
                "event": "validation_batch_written",
                "report_id": self._report_id,
                "stage": "validation",
                "batch_kind": batch_kind,
                "batch_size": len(batch),
            },
        )


class ReportValidator:
    def __init__(
        self,
        storage: ObjectStorage,
        session_factory: Callable[[], Session],
        batch_size: int,
        *,
        csv_max_field_chars: int,
        csv_max_record_chars: int,
        csv_error_raw_value_chars: int,
        csv_error_raw_total_chars: int,
    ) -> None:
        self._storage = storage
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._csv_max_field_chars = csv_max_field_chars
        self._csv_max_record_chars = csv_max_record_chars
        self._csv_error_raw_value_chars = csv_error_raw_value_chars
        self._csv_error_raw_total_chars = csv_error_raw_total_chars
        self._duplicate_raw_value_chars = max(
            1,
            min(csv_error_raw_value_chars, csv_error_raw_total_chars // 4),
        )

    def validate(
        self,
        report: ReportSource,
        assert_lock_alive: Callable[[], None],
    ) -> WorkerCycleOutcome:
        logger.info(
            "Report validation started",
            extra={
                "event": "validation_started",
                "report_id": report.id,
                "stage": "validation",
            },
        )
        assert_lock_alive()
        chunks = self._storage.download_stream(report.object_bucket, report.object_key)
        writer = ValidationBatchWriter(
            report.id,
            self._session_factory,
            self._batch_size,
            assert_lock_alive,
        )
        summary = validate_csv_stream(
            chunks,
            writer.add_valid_row,
            writer.add_issues,
            before_next_chunk=assert_lock_alive,
            max_field_chars=self._csv_max_field_chars,
            max_record_chars=self._csv_max_record_chars,
            error_raw_value_chars=self._csv_error_raw_value_chars,
            error_raw_total_chars=self._csv_error_raw_total_chars,
        )
        writer.flush_all()
        assert_lock_alive()
        self._insert_duplicate_errors(report.id)
        assert_lock_alive()
        error_count = self._error_count(report.id)

        if error_count:
            assert_lock_alive()
            self._finalize_validation_failure(report.id, summary.row_count, error_count)
            logger.info(
                "Report validation failed",
                extra={
                    "event": "validation_failed",
                    "report_id": report.id,
                    "stage": "validation",
                    "status": "failed",
                    "error_count": error_count,
                },
            )
            return WorkerCycleOutcome.VALIDATION_FAILED

        if not summary.reached_eof or summary.row_count == 0:
            raise RuntimeError("validation ended without EOF or a validation error")

        assert_lock_alive()
        with self._session_factory() as session, session.begin():
            session.execute(
                update(Report)
                .where(Report.id == report.id, Report.status == "processing")
                .values(row_count=summary.row_count, error_count=0)
            )
        logger.info(
            "Report validation completed",
            extra={
                "event": "validation_completed",
                "report_id": report.id,
                "stage": "validation",
                "status": "processing",
                "row_count": summary.row_count,
            },
        )
        return WorkerCycleOutcome.VALIDATED

    def _insert_duplicate_errors(self, report_id: int) -> None:
        ranked = (
            select(
                ReportStagingRow.line_number,
                ReportStagingRow.warehouse_code,
                ReportStagingRow.warehouse_name,
                ReportStagingRow.sku,
                ReportStagingRow.product_name,
                ReportStagingRow.quantity,
                func.row_number()
                .over(
                    partition_by=(
                        ReportStagingRow.warehouse_code,
                        ReportStagingRow.sku,
                    ),
                    order_by=ReportStagingRow.line_number,
                )
                .label("duplicate_rank"),
            )
            .where(ReportStagingRow.report_id == report_id)
            .subquery()
        )
        duplicate_rows = select(
            literal(report_id),
            ranked.c.line_number,
            literal("warehouse_code+sku"),
            literal("duplicate_warehouse_sku"),
            literal("The warehouse_code and sku pair is duplicated in this report"),
            func.jsonb_build_object(
                "warehouse_code",
                func.left(ranked.c.warehouse_code, self._duplicate_raw_value_chars),
                "warehouse_name",
                func.left(ranked.c.warehouse_name, self._duplicate_raw_value_chars),
                "sku",
                func.left(ranked.c.sku, self._duplicate_raw_value_chars),
                "product_name",
                func.left(ranked.c.product_name, self._duplicate_raw_value_chars),
                "quantity",
                ranked.c.quantity,
                "_truncated",
                or_(
                    func.char_length(ranked.c.warehouse_code) > self._duplicate_raw_value_chars,
                    func.char_length(ranked.c.warehouse_name) > self._duplicate_raw_value_chars,
                    func.char_length(ranked.c.sku) > self._duplicate_raw_value_chars,
                    func.char_length(ranked.c.product_name) > self._duplicate_raw_value_chars,
                ),
            ),
            func.now(),
        ).where(ranked.c.duplicate_rank > 1)

        with self._session_factory() as session, session.begin():
            session.execute(
                insert(ReportError).from_select(
                    [
                        "report_id",
                        "line_number",
                        "field_name",
                        "code",
                        "message",
                        "raw_data",
                        "created_at",
                    ],
                    duplicate_rows,
                )
            )

    def _error_count(self, report_id: int) -> int:
        with self._session_factory() as session, session.begin():
            count = session.scalar(
                select(func.count())
                .select_from(ReportError)
                .where(ReportError.report_id == report_id)
            )
        return count or 0

    def _finalize_validation_failure(
        self,
        report_id: int,
        row_count: int,
        error_count: int,
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(delete(ReportStagingRow).where(ReportStagingRow.report_id == report_id))
            session.execute(
                update(Report)
                .where(Report.id == report_id, Report.status == "processing")
                .values(
                    status="failed",
                    finished_at=func.now(),
                    row_count=row_count,
                    error_count=error_count,
                    failure_kind="validation",
                    failure_message="Report contains validation errors",
                )
            )


class ReportProcessor:
    def __init__(
        self,
        storage: ObjectStorage,
        session_factory: Callable[[], Session],
        batch_size: int,
        apply_step_hook: ApplyStepHook | None = None,
        *,
        csv_max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
        csv_max_record_chars: int = DEFAULT_MAX_RECORD_CHARS,
        csv_error_raw_value_chars: int = DEFAULT_ERROR_RAW_VALUE_CHARS,
        csv_error_raw_total_chars: int = DEFAULT_ERROR_RAW_TOTAL_CHARS,
    ) -> None:
        self._session_factory = session_factory
        self._validator = ReportValidator(
            storage,
            session_factory,
            batch_size,
            csv_max_field_chars=csv_max_field_chars,
            csv_max_record_chars=csv_max_record_chars,
            csv_error_raw_value_chars=csv_error_raw_value_chars,
            csv_error_raw_total_chars=csv_error_raw_total_chars,
        )
        self._applier = ApplyReportService(session_factory, after_step=apply_step_hook)

    def process(self, lock_connection: Connection) -> WorkerCycleOutcome:
        report, ready_for_apply = self._claim_or_recover_next_report()
        if report is None:
            return WorkerCycleOutcome.NO_REPORT

        def assert_lock_alive() -> None:
            ensure_lock_connection_alive(lock_connection)

        if ready_for_apply:
            try:
                self._applier.apply(report.id, assert_lock_alive)
            except LockConnectionLost:
                raise
            except Exception as original_error:
                logger.exception(
                    "Report apply failed",
                    extra={
                        "event": "report_apply_failed",
                        "report_id": report.id,
                        "stage": "apply",
                        "status": "failed",
                    },
                )
                try:
                    assert_lock_alive()
                    self._mark_apply_failed(report.id)
                except Exception as persistence_error:
                    logger.exception(
                        "Failed to persist report apply failure",
                        extra={
                            "event": "report_apply_failure_persist_failed",
                            "report_id": report.id,
                            "stage": "apply",
                        },
                    )
                    raise original_error from persistence_error
                return WorkerCycleOutcome.PROCESSING_FAILED
            return WorkerCycleOutcome.COMPLETED

        try:
            return self._validator.validate(report, assert_lock_alive)
        except LockConnectionLost:
            raise
        except Exception:
            logger.exception(
                "Report processing failed",
                extra={
                    "event": "processing_failed",
                    "report_id": report.id,
                    "stage": "validation",
                    "status": "failed",
                },
            )
            assert_lock_alive()
            self._mark_processing_failed(report.id)
            return WorkerCycleOutcome.PROCESSING_FAILED

    def _claim_or_recover_next_report(self) -> tuple[ReportSource | None, bool]:
        with self._session_factory() as session, session.begin():
            report = session.scalar(
                select(Report)
                .where(Report.status.in_(("pending", "processing")))
                .order_by(Report.created_at, Report.id)
                .limit(1)
                .with_for_update()
            )
            if report is None:
                return None, False

            if report.status == "processing" and self._validation_is_complete(session, report):
                return _report_source(report), True

            session.execute(delete(ReportStagingRow).where(ReportStagingRow.report_id == report.id))
            session.execute(delete(ReportError).where(ReportError.report_id == report.id))
            report.status = "processing"
            report.processing_started_at = func.now()
            report.finished_at = None
            report.row_count = 0
            report.error_count = 0
            report.failure_kind = None
            report.failure_message = None
            source = _report_source(report)

        logger.info(
            "Report claimed for validation",
            extra={
                "event": "report_claimed",
                "report_id": source.id,
                "stage": "validation",
                "status": "processing",
            },
        )
        return source, False

    @staticmethod
    def _validation_is_complete(session: Session, report: Report) -> bool:
        if report.row_count <= 0 or report.error_count != 0:
            return False
        staging_count = session.scalar(
            select(func.count())
            .select_from(ReportStagingRow)
            .where(ReportStagingRow.report_id == report.id)
        )
        stored_error_count = session.scalar(
            select(func.count()).select_from(ReportError).where(ReportError.report_id == report.id)
        )
        return staging_count == report.row_count and stored_error_count == 0

    def _mark_processing_failed(self, report_id: int) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(delete(ReportStagingRow).where(ReportStagingRow.report_id == report_id))
            session.execute(
                insert(ReportError),
                [
                    {
                        "report_id": report_id,
                        "line_number": None,
                        "field_name": None,
                        "code": "processing_error",
                        "message": "Report processing failed",
                        "raw_data": None,
                    }
                ],
            )
            error_count = session.scalar(
                select(func.count())
                .select_from(ReportError)
                .where(ReportError.report_id == report_id)
            )
            session.execute(
                update(Report)
                .where(Report.id == report_id, Report.status == "processing")
                .values(
                    status="failed",
                    finished_at=func.now(),
                    error_count=error_count or 1,
                    failure_kind="processing",
                    failure_message="Report processing failed",
                )
            )

    def _mark_apply_failed(self, report_id: int) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(delete(ReportStagingRow).where(ReportStagingRow.report_id == report_id))
            session.execute(
                insert(ReportError),
                [
                    {
                        "report_id": report_id,
                        "line_number": None,
                        "field_name": None,
                        "code": "apply_error",
                        "message": "Atomic report apply failed",
                        "raw_data": None,
                    }
                ],
            )
            error_count = session.scalar(
                select(func.count())
                .select_from(ReportError)
                .where(ReportError.report_id == report_id)
            )
            session.execute(
                update(Report)
                .where(Report.id == report_id, Report.status == "processing")
                .values(
                    status="failed",
                    finished_at=func.now(),
                    error_count=error_count or 1,
                    stocks_created=0,
                    stocks_updated=0,
                    stocks_zeroed=0,
                    failure_kind="processing",
                    failure_message="Atomic report apply failed",
                )
            )


def process_next_report(
    engine: Engine,
    storage: ObjectStorage,
    session_factory: Callable[[], Session],
    *,
    advisory_lock_key: int,
    batch_size: int,
    apply_step_hook: ApplyStepHook | None = None,
    csv_max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
    csv_max_record_chars: int = DEFAULT_MAX_RECORD_CHARS,
    csv_error_raw_value_chars: int = DEFAULT_ERROR_RAW_VALUE_CHARS,
    csv_error_raw_total_chars: int = DEFAULT_ERROR_RAW_TOTAL_CHARS,
) -> WorkerCycleOutcome:
    with engine.connect() as lock_connection:
        acquired = bool(
            lock_connection.scalar(select(func.pg_try_advisory_lock(advisory_lock_key)))
        )
        if not acquired:
            logger.info(
                "Worker advisory lock not acquired",
                extra={"event": "worker_lock_not_acquired", "stage": "worker"},
            )
            return WorkerCycleOutcome.LOCK_NOT_ACQUIRED

        logger.info(
            "Worker advisory lock acquired",
            extra={"event": "worker_lock_acquired", "stage": "worker"},
        )
        try:
            processor = ReportProcessor(
                storage,
                session_factory,
                batch_size,
                apply_step_hook=apply_step_hook,
                csv_max_field_chars=csv_max_field_chars,
                csv_max_record_chars=csv_max_record_chars,
                csv_error_raw_value_chars=csv_error_raw_value_chars,
                csv_error_raw_total_chars=csv_error_raw_total_chars,
            )
            return processor.process(lock_connection)
        finally:
            try:
                released = bool(
                    lock_connection.scalar(select(func.pg_advisory_unlock(advisory_lock_key)))
                )
            except SQLAlchemyError:
                logger.exception(
                    "Worker advisory lock connection was lost",
                    extra={"event": "worker_lock_released", "stage": "worker"},
                )
            else:
                logger.info(
                    "Worker advisory lock released",
                    extra={
                        "event": "worker_lock_released",
                        "stage": "worker",
                        "lock_released": released,
                    },
                )


def ensure_lock_connection_alive(connection: Connection) -> None:
    if connection.closed or connection.invalidated:
        raise LockConnectionLost
    try:
        connection.execute(text("SELECT 1")).scalar_one()
    except SQLAlchemyError as exc:
        raise LockConnectionLost from exc


def _report_source(report: Report) -> ReportSource:
    return ReportSource(
        id=report.id,
        object_bucket=report.object_bucket,
        object_key=report.object_key,
    )
