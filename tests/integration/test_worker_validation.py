import hashlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event, Lock, Thread
from typing import Any
from uuid import uuid4

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.client import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from sqlalchemy import Engine, event, func, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Product, Report, ReportError, ReportStagingRow, StockBalance, Warehouse
from app.services.report_processing import (
    LockConnectionLost,
    ReportProcessor,
    WorkerCycleOutcome,
    process_next_report,
)
from app.services.storage import S3ObjectStorage

pytestmark = pytest.mark.integration
WORKER_TEST_BUCKET = "stock-reports-worker-test"
LOCK_KEY = 8_104_221_337
HEADER = "warehouse_code,warehouse_name,sku,product_name,quantity\n"


class WorkerLogCollector:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process
        self._lines: list[str] = []
        self._events: dict[str, Event] = {}
        self._lock = Lock()
        self._thread = Thread(target=self._collect, daemon=True)
        self._thread.start()

    def _collect(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            with self._lock:
                self._lines.append(line)
            try:
                event_name = json.loads(line).get("event")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(event_name, str):
                with self._lock:
                    event = self._events.setdefault(event_name, Event())
                event.set()

    def wait_for(self, event_name: str, timeout: float = 10.0) -> bool:
        with self._lock:
            event = self._events.setdefault(event_name, Event())
        return event.wait(timeout)

    def finish(self) -> str:
        self._thread.join(timeout=2)
        with self._lock:
            return "".join(self._lines)


def start_worker_process(
    test_database_url: URL,
) -> tuple[subprocess.Popen[str], WorkerLogCollector]:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": test_database_url.render_as_string(hide_password=False),
            "S3_BUCKET": WORKER_TEST_BUCKET,
            "WORKER_ADVISORY_LOCK_KEY": str(LOCK_KEY + 1),
            "WORKER_POLL_INTERVAL_SECONDS": "30",
            "PYTHONUNBUFFERED": "1",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process, WorkerLogCollector(process)


def stop_worker_process(
    process: subprocess.Popen[str],
    collector: WorkerLogCollector,
) -> str:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        return_code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        pytest.fail("worker did not stop within five seconds after SIGTERM")
    output = collector.finish()
    assert return_code == 0, output
    assert "worker_shutdown_requested" in output
    assert "worker_stopped" in output
    return output


class BlockingDownloadStorage(S3ObjectStorage):
    def __init__(
        self,
        delegate: S3ObjectStorage,
        download_started: Event,
        release_download: Event,
    ) -> None:
        self._delegate = delegate
        self._download_started = download_started
        self._release_download = release_download

    def download_stream(self, bucket: str, key: str) -> Iterator[bytes]:
        self._download_started.set()
        if not self._release_download.wait(timeout=5):
            raise RuntimeError("concurrent worker test did not release the download")
        return self._delegate.download_stream(bucket, key)


def clean_bucket(client: Any) -> None:
    listed = client.list_objects_v2(Bucket=WORKER_TEST_BUCKET)
    objects = [{"Key": item["Key"]} for item in listed.get("Contents", [])]
    if objects:
        client.delete_objects(Bucket=WORKER_TEST_BUCKET, Delete={"Objects": objects})
    for upload in client.list_multipart_uploads(Bucket=WORKER_TEST_BUCKET).get("Uploads", []):
        client.abort_multipart_upload(
            Bucket=WORKER_TEST_BUCKET,
            Key=upload["Key"],
            UploadId=upload["UploadId"],
        )


@pytest.fixture
def worker_minio_client() -> Iterator[Any]:
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
        client.head_bucket(Bucket=WORKER_TEST_BUCKET)
    except ClientError:
        client.create_bucket(Bucket=WORKER_TEST_BUCKET)
    clean_bucket(client)
    try:
        yield client
    finally:
        clean_bucket(client)


@pytest.fixture
def worker_storage(worker_minio_client: Any) -> S3ObjectStorage:
    return S3ObjectStorage(get_settings(), client=worker_minio_client)


def create_report(
    session_factory: sessionmaker[Session],
    storage: S3ObjectStorage,
    content: bytes,
    *,
    status: str = "pending",
) -> int:
    key = f"worker-tests/{uuid4()}/original.csv"
    upload = storage.upload_stream(WORKER_TEST_BUCKET, key, BytesIO(content))
    with session_factory() as session, session.begin():
        report = Report(
            status=status,
            original_filename="report.csv",
            object_bucket=WORKER_TEST_BUCKET,
            object_key=key,
            size_bytes=upload.size_bytes,
            checksum_sha256=upload.checksum_sha256,
        )
        session.add(report)
        session.flush()
        report_id = report.id
    return report_id


def run_worker(
    engine: Engine,
    storage: S3ObjectStorage,
    session_factory: sessionmaker[Session],
    *,
    batch_size: int = 2,
    csv_max_field_chars: int = 1_048_576,
    csv_max_record_chars: int = 4_194_304,
    csv_error_raw_value_chars: int = 1_024,
    csv_error_raw_total_chars: int = 4_096,
) -> WorkerCycleOutcome:
    return process_next_report(
        engine,
        storage,
        session_factory,
        advisory_lock_key=LOCK_KEY,
        batch_size=batch_size,
        csv_max_field_chars=csv_max_field_chars,
        csv_max_record_chars=csv_max_record_chars,
        csv_error_raw_value_chars=csv_error_raw_value_chars,
        csv_error_raw_total_chars=csv_error_raw_total_chars,
    )


def subject_counts(session: Session) -> tuple[int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(Warehouse)) or 0,
        session.scalar(select(func.count()).select_from(Product)) or 0,
        session.scalar(select(func.count()).select_from(StockBalance)) or 0,
    )


def test_valid_report_is_staged_then_applied_on_next_cycle(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    content = (
        HEADER + "WH-1,Warehouse 1,SKU-1,Product 1,10\n" + "WH-2,Warehouse 2,SKU-2,Product 2,0\n"
    ).encode()
    report_id = create_report(test_session_factory, worker_storage, content)

    outcome = run_worker(test_engine, worker_storage, test_session_factory, batch_size=1)

    assert outcome == WorkerCycleOutcome.VALIDATED
    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "processing"
        assert report.processing_started_at is not None
        assert report.finished_at is None
        assert report.row_count == 2
        assert report.error_count == 0
        staging = list(
            session.scalars(
                select(ReportStagingRow)
                .where(ReportStagingRow.report_id == report_id)
                .order_by(ReportStagingRow.line_number)
            )
        )
        assert [(row.line_number, row.quantity) for row in staging] == [(2, 10), (3, 0)]
        assert subject_counts(session) == (0, 0, 0)

    assert (
        run_worker(test_engine, worker_storage, test_session_factory)
        == WorkerCycleOutcome.COMPLETED
    )
    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "completed"
        assert report.finished_at is not None
        assert report.stocks_created == 2
        assert report.stocks_updated == 0
        assert report.stocks_zeroed == 0
        assert subject_counts(session) == (2, 2, 2)
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReportStagingRow)
                .where(ReportStagingRow.report_id == report_id)
            )
            == 0
        )
        object_bucket = report.object_bucket
        object_key = report.object_key

    assert b"".join(worker_storage.download_stream(object_bucket, object_key)) == content


def test_invalid_report_fails_without_subject_changes_and_keeps_original(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    content = (HEADER + ",Warehouse,SKU,Product,-1\n").encode()
    report_id = create_report(test_session_factory, worker_storage, content)

    outcome = run_worker(test_engine, worker_storage, test_session_factory)

    assert outcome == WorkerCycleOutcome.VALIDATION_FAILED
    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "failed"
        assert report.finished_at is not None
        assert report.failure_kind == "validation"
        assert report.error_count == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReportStagingRow)
                .where(ReportStagingRow.report_id == report_id)
            )
            == 0
        )
        errors = list(
            session.scalars(
                select(ReportError)
                .where(ReportError.report_id == report_id)
                .order_by(ReportError.id)
            )
        )
        assert {error.code for error in errors} == {"required", "quantity_negative"}
        assert all(error.line_number == 2 for error in errors)
        assert all(error.field_name is not None for error in errors)
        assert all(error.message for error in errors)
        assert all(error.raw_data is not None for error in errors)
        assert subject_counts(session) == (0, 0, 0)
        object_bucket = report.object_bucket
        object_key = report.object_key

    assert b"".join(worker_storage.download_stream(object_bucket, object_key)) == content


@pytest.mark.parametrize(
    ("content", "field_limit", "record_limit", "expected_code"),
    [
        (
            (HEADER + f"WH,W,SKU,{'P' * 65},1\n").encode(),
            64,
            512,
            "csv_field_too_large",
        ),
        (
            (HEADER + f'WH,{'W' * 70},SKU,"line-one\n{'P' * 70}",1\n').encode(),
            128,
            128,
            "csv_record_too_large",
        ),
    ],
    ids=["field-limit", "multiline-record-limit"],
)
def test_oversized_csv_is_validation_failure_with_bounded_error_and_original(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
    content: bytes,
    field_limit: int,
    record_limit: int,
    expected_code: str,
) -> None:
    with test_session_factory() as session, session.begin():
        warehouse = Warehouse(code="EXISTING", name="Existing")
        product = Product(sku="EXISTING", name="Existing")
        session.add_all([warehouse, product])
        session.flush()
        session.add(
            StockBalance(
                warehouse_id=warehouse.id,
                product_id=product.id,
                quantity=7,
            )
        )
    report_id = create_report(test_session_factory, worker_storage, content)

    outcome = run_worker(
        test_engine,
        worker_storage,
        test_session_factory,
        csv_max_field_chars=field_limit,
        csv_max_record_chars=record_limit,
        csv_error_raw_value_chars=16,
        csv_error_raw_total_chars=64,
    )

    assert outcome == WorkerCycleOutcome.VALIDATION_FAILED
    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "failed"
        assert report.failure_kind == "validation"
        error = session.scalar(select(ReportError).where(ReportError.report_id == report_id))
        assert error is not None
        assert error.code == expected_code
        assert error.raw_data is not None
        assert error.raw_data["_truncated"] is True
        raw_strings = [value for value in error.raw_data.values() if isinstance(value, str)]
        assert all(len(value) <= 16 for value in raw_strings)
        assert sum(map(len, raw_strings)) <= 64
        assert subject_counts(session) == (1, 1, 1)
        balance = session.scalar(select(StockBalance))
        assert balance is not None
        assert balance.quantity == 7
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReportStagingRow)
                .where(ReportStagingRow.report_id == report_id)
            )
            == 0
        )
        object_bucket = report.object_bucket
        object_key = report.object_key

    assert b"".join(worker_storage.download_stream(object_bucket, object_key)) == content


def test_duplicate_pair_between_batches_marks_repeated_line(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    content = (HEADER + "WH,Name 1,SKU,Product 1,1\n" + "WH,Name 2,SKU,Product 2,2\n").encode()
    report_id = create_report(test_session_factory, worker_storage, content)

    assert run_worker(test_engine, worker_storage, test_session_factory, batch_size=1) == (
        WorkerCycleOutcome.VALIDATION_FAILED
    )

    with test_session_factory() as session, session.begin():
        duplicate = session.scalar(
            select(ReportError).where(
                ReportError.report_id == report_id,
                ReportError.code == "duplicate_warehouse_sku",
            )
        )
        assert duplicate is not None
        assert duplicate.line_number == 3
        assert duplicate.field_name == "warehouse_code+sku"
        assert duplicate.raw_data is not None
        assert duplicate.raw_data["warehouse_code"] == "WH"


def test_different_names_for_same_codes_are_valid(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    content = (
        HEADER
        + "WH,Warehouse old,SKU-1,Product 1,1\n"
        + "WH,Warehouse new,SKU-2,Product old,2\n"
        + "OTHER,Other,SKU-2,Product new,3\n"
    ).encode()
    report_id = create_report(test_session_factory, worker_storage, content)

    assert run_worker(test_engine, worker_storage, test_session_factory, batch_size=1) == (
        WorkerCycleOutcome.VALIDATED
    )
    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "processing"
        assert report.row_count == 3
        assert report.error_count == 0


def test_partial_processing_is_cleared_and_revalidated_from_start(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    content = (HEADER + "WH,Warehouse,SKU,Product,4\n").encode()
    report_id = create_report(
        test_session_factory,
        worker_storage,
        content,
        status="processing",
    )
    with test_session_factory() as session, session.begin():
        session.add(
            ReportStagingRow(
                report_id=report_id,
                line_number=999,
                warehouse_code="PARTIAL",
                warehouse_name="Partial",
                sku="PARTIAL",
                product_name="Partial",
                quantity=1,
            )
        )
        session.add(
            ReportError(
                report_id=report_id,
                line_number=999,
                field_name="quantity",
                code="partial",
                message="partial",
                raw_data=None,
            )
        )

    assert run_worker(test_engine, worker_storage, test_session_factory) == (
        WorkerCycleOutcome.VALIDATED
    )
    with test_session_factory() as session, session.begin():
        staging = list(
            session.scalars(select(ReportStagingRow).where(ReportStagingRow.report_id == report_id))
        )
        assert [(row.line_number, row.warehouse_code) for row in staging] == [(2, "WH")]
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReportError)
                .where(ReportError.report_id == report_id)
            )
            == 0
        )


def test_completed_validation_is_applied_before_newer_pending_report(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    older_id = create_report(
        test_session_factory,
        worker_storage,
        (HEADER + "WH,W,SKU,P,1\n").encode(),
        status="processing",
    )
    with test_session_factory() as session, session.begin():
        older = session.get(Report, older_id)
        assert older is not None
        older.row_count = 1
        older.error_count = 0
        session.add(
            ReportStagingRow(
                report_id=older_id,
                line_number=2,
                warehouse_code="WH",
                warehouse_name="W",
                sku="SKU",
                product_name="P",
                quantity=1,
            )
        )
    newer_id = create_report(
        test_session_factory,
        worker_storage,
        (HEADER + "NEW,New,SKU-2,P2,2\n").encode(),
    )

    assert (
        run_worker(test_engine, worker_storage, test_session_factory)
        == WorkerCycleOutcome.COMPLETED
    )
    with test_session_factory() as session, session.begin():
        older = session.get(Report, older_id)
        newer = session.get(Report, newer_id)
        assert older is not None
        assert older.status == "completed"
        assert newer is not None
        assert newer.status == "pending"
        assert newer.processing_started_at is None


def test_only_one_connection_holds_advisory_lock_and_lock_is_reusable(
    test_engine: Engine,
) -> None:
    with test_engine.connect() as first, test_engine.connect() as second:
        assert first.scalar(select(func.pg_try_advisory_lock(LOCK_KEY))) is True
        assert second.scalar(select(func.pg_try_advisory_lock(LOCK_KEY))) is False
        assert first.scalar(select(func.pg_advisory_unlock(LOCK_KEY))) is True
        assert second.scalar(select(func.pg_try_advisory_lock(LOCK_KEY))) is True
        assert second.scalar(select(func.pg_advisory_unlock(LOCK_KEY))) is True


def test_existing_lock_makes_worker_skip_processing(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    report_id = create_report(
        test_session_factory,
        worker_storage,
        (HEADER + "WH,W,SKU,P,1\n").encode(),
    )
    with test_engine.connect() as first:
        assert first.scalar(select(func.pg_try_advisory_lock(LOCK_KEY))) is True
        assert run_worker(test_engine, worker_storage, test_session_factory) == (
            WorkerCycleOutcome.LOCK_NOT_ACQUIRED
        )
        assert first.scalar(select(func.pg_advisory_unlock(LOCK_KEY))) is True

    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "pending"


def test_two_concurrent_worker_cycles_preserve_lock_and_fifo(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    older_id = create_report(
        test_session_factory,
        worker_storage,
        (HEADER + "OLDER,Older,OLDER-SKU,Older product,1\n").encode(),
    )
    newer_id = create_report(
        test_session_factory,
        worker_storage,
        (HEADER + "NEWER,Newer,NEWER-SKU,Newer product,2\n").encode(),
    )
    download_started = Event()
    release_download = Event()
    blocking_storage = BlockingDownloadStorage(
        worker_storage,
        download_started,
        release_download,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_cycle = executor.submit(
            run_worker,
            test_engine,
            blocking_storage,
            test_session_factory,
        )
        assert download_started.wait(timeout=5)
        second_cycle = executor.submit(
            run_worker,
            test_engine,
            worker_storage,
            test_session_factory,
        )
        try:
            assert second_cycle.result(timeout=5) == WorkerCycleOutcome.LOCK_NOT_ACQUIRED
        finally:
            release_download.set()
        assert first_cycle.result(timeout=5) == WorkerCycleOutcome.VALIDATED

    with test_session_factory() as session, session.begin():
        older = session.get(Report, older_id)
        newer = session.get(Report, newer_id)
        assert older is not None
        assert older.status == "processing"
        assert newer is not None
        assert newer.status == "pending"
        assert subject_counts(session) == (0, 0, 0)

    assert run_worker(test_engine, worker_storage, test_session_factory) == (
        WorkerCycleOutcome.COMPLETED
    )
    with test_session_factory() as session, session.begin():
        older = session.get(Report, older_id)
        newer = session.get(Report, newer_id)
        assert older is not None
        assert older.status == "completed"
        assert newer is not None
        assert newer.status == "pending"

    assert run_worker(test_engine, worker_storage, test_session_factory) == (
        WorkerCycleOutcome.VALIDATED
    )
    with test_session_factory() as session, session.begin():
        older = session.get(Report, older_id)
        newer = session.get(Report, newer_id)
        assert older is not None
        assert older.status == "completed"
        assert newer is not None
        assert newer.status == "processing"


def test_lost_lock_connection_stops_attempt_and_leaves_report_recoverable(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    report_id = create_report(
        test_session_factory,
        worker_storage,
        (HEADER + "WH,W,SKU,P,1\n").encode(),
    )
    processor = ReportProcessor(worker_storage, test_session_factory, batch_size=2)

    with test_engine.connect() as lost_connection:
        lost_connection.close()
        with pytest.raises(LockConnectionLost):
            processor.process(lost_connection)

    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "processing"
        assert report.row_count == 0
        assert subject_counts(session) == (0, 0, 0)
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReportStagingRow)
                .where(ReportStagingRow.report_id == report_id)
            )
            == 0
        )

    assert run_worker(test_engine, worker_storage, test_session_factory) == (
        WorkerCycleOutcome.VALIDATED
    )


def test_error_and_staging_writes_use_bounded_batches(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    batch_size = 3
    rows = [f"WH-{number},,SKU-{number},,-1\n" for number in range(7)]
    report_id = create_report(
        test_session_factory,
        worker_storage,
        (HEADER + "".join(rows)).encode(),
    )
    error_batch_sizes: list[int] = []

    def capture_batches(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        executemany: bool,
    ) -> None:
        if statement.startswith("INSERT INTO report_errors") and executemany:
            assert isinstance(parameters, list)
            error_batch_sizes.append(len(parameters))

    event.listen(test_engine, "before_cursor_execute", capture_batches)
    try:
        assert (
            run_worker(
                test_engine,
                worker_storage,
                test_session_factory,
                batch_size=batch_size,
            )
            == WorkerCycleOutcome.VALIDATION_FAILED
        )
    finally:
        event.remove(test_engine, "before_cursor_execute", capture_batches)

    assert len(error_batch_sizes) > 1
    assert max(error_batch_sizes) <= batch_size
    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.error_count == 21


def test_error_in_last_row_of_large_stream_fails_after_many_staging_batches(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    batch_size = 25
    valid_rows = [
        f"WH-{number},Warehouse {number},SKU-{number},Product {number},{number}\n"
        for number in range(250)
    ]
    content = (HEADER + "".join(valid_rows) + "LAST,Last,LAST-SKU,Last,-1\n").encode()
    report_id = create_report(test_session_factory, worker_storage, content)
    staging_batch_sizes: list[int] = []

    def capture_batches(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        executemany: bool,
    ) -> None:
        if statement.startswith("INSERT INTO report_staging_rows") and executemany:
            assert isinstance(parameters, list)
            staging_batch_sizes.append(len(parameters))

    event.listen(test_engine, "before_cursor_execute", capture_batches)
    try:
        assert (
            run_worker(
                test_engine,
                worker_storage,
                test_session_factory,
                batch_size=batch_size,
            )
            == WorkerCycleOutcome.VALIDATION_FAILED
        )
    finally:
        event.remove(test_engine, "before_cursor_execute", capture_batches)

    assert len(staging_batch_sizes) == 10
    assert max(staging_batch_sizes) <= batch_size
    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "failed"
        assert report.row_count == 251
        last_error = session.scalar(select(ReportError).where(ReportError.report_id == report_id))
        assert last_error is not None
        assert last_error.line_number == 252
        assert last_error.code == "quantity_negative"
        assert subject_counts(session) == (0, 0, 0)


def test_missing_original_becomes_processing_error(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    with test_session_factory() as session, session.begin():
        report = Report(
            status="pending",
            original_filename="missing.csv",
            object_bucket=WORKER_TEST_BUCKET,
            object_key=f"missing/{uuid4()}.csv",
            size_bytes=1,
            checksum_sha256=hashlib.sha256(b"x").hexdigest(),
        )
        session.add(report)
        session.flush()
        report_id = report.id

    assert run_worker(test_engine, worker_storage, test_session_factory) == (
        WorkerCycleOutcome.PROCESSING_FAILED
    )
    with test_session_factory() as session, session.begin():
        loaded_report = session.get(Report, report_id)
        assert loaded_report is not None
        assert loaded_report.status == "failed"
        assert loaded_report.failure_kind == "processing"
        error = session.scalar(select(ReportError).where(ReportError.report_id == report_id))
        assert error is not None
        assert error.code == "processing_error"
        assert error.raw_data is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM lifecycle test")
def test_worker_sigterm_is_graceful_and_restart_continues_queue(
    test_database_url: URL,
    test_session_factory: sessionmaker[Session],
    worker_storage: S3ObjectStorage,
) -> None:
    idle_worker, idle_logs = start_worker_process(test_database_url)
    try:
        assert idle_logs.wait_for("worker_started")
    finally:
        stop_worker_process(idle_worker, idle_logs)

    report_id = create_report(
        test_session_factory,
        worker_storage,
        (HEADER + "RESTART,Restart,RESTART-SKU,Restart product,5\n").encode(),
    )

    validating_worker, validating_logs = start_worker_process(test_database_url)
    try:
        assert validating_logs.wait_for("validation_completed")
    finally:
        stop_worker_process(validating_worker, validating_logs)

    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "processing"

    applying_worker, applying_logs = start_worker_process(test_database_url)
    try:
        assert applying_logs.wait_for("report_completed")
    finally:
        stop_worker_process(applying_worker, applying_logs)

    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "completed"
        assert report.stocks_created == 1
