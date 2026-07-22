import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Product, Report, ReportError, ReportStagingRow, StockBalance, Warehouse
from app.services.apply_report import ApplyStep
from app.services.report_processing import WorkerCycleOutcome, process_next_report
from tests.fakes import InMemoryStorage

pytestmark = pytest.mark.integration
LOCK_KEY = 9_204_331_447
StagingValue = tuple[int, str, str, str, str, int]


def create_validated_report(
    session_factory: sessionmaker[Session],
    rows: Sequence[StagingValue],
    *,
    status: str = "processing",
) -> int:
    with session_factory() as session, session.begin():
        report = Report(
            status=status,
            original_filename="apply.csv",
            object_bucket="apply-test",
            object_key=f"apply-tests/{uuid4()}/original.csv",
            size_bytes=0,
            checksum_sha256=hashlib.sha256(b"").hexdigest(),
            row_count=len(rows) if status == "processing" else 0,
            error_count=0,
        )
        session.add(report)
        session.flush()
        report_id = report.id
        if rows:
            session.execute(
                insert(ReportStagingRow),
                [
                    {
                        "report_id": report_id,
                        "line_number": line_number,
                        "warehouse_code": warehouse_code,
                        "warehouse_name": warehouse_name,
                        "sku": sku,
                        "product_name": product_name,
                        "quantity": quantity,
                    }
                    for (
                        line_number,
                        warehouse_code,
                        warehouse_name,
                        sku,
                        product_name,
                        quantity,
                    ) in rows
                ],
            )
    return report_id


def run_apply(
    engine: Engine,
    session_factory: sessionmaker[Session],
    *,
    storage: InMemoryStorage | None = None,
    hook: Any = None,
) -> WorkerCycleOutcome:
    return process_next_report(
        engine,
        storage or InMemoryStorage(),
        session_factory,
        advisory_lock_key=LOCK_KEY,
        batch_size=10,
        apply_step_hook=hook,
    )


def seed_inventory(
    session_factory: sessionmaker[Session],
    balances: Sequence[tuple[str, str, str, str, int]],
    *,
    timestamp: datetime | None = None,
) -> None:
    timestamp = timestamp or datetime(2020, 1, 1, tzinfo=UTC)
    with session_factory() as session, session.begin():
        warehouse_by_code: dict[str, Warehouse] = {}
        product_by_sku: dict[str, Product] = {}
        for warehouse_code, warehouse_name, sku, product_name, _quantity in balances:
            if warehouse_code not in warehouse_by_code:
                warehouse = Warehouse(
                    code=warehouse_code,
                    name=warehouse_name,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(warehouse)
                warehouse_by_code[warehouse_code] = warehouse
            if sku not in product_by_sku:
                product = Product(
                    sku=sku,
                    name=product_name,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(product)
                product_by_sku[sku] = product
        session.flush()
        for warehouse_code, _warehouse_name, sku, _product_name, quantity in balances:
            session.add(
                StockBalance(
                    warehouse_id=warehouse_by_code[warehouse_code].id,
                    product_id=product_by_sku[sku].id,
                    quantity=quantity,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )


def quantities(session: Session) -> dict[tuple[str, str], int]:
    rows = session.execute(
        select(Warehouse.code, Product.sku, StockBalance.quantity)
        .join(StockBalance, StockBalance.warehouse_id == Warehouse.id)
        .join(Product, Product.id == StockBalance.product_id)
    )
    return {(warehouse_code, sku): quantity for warehouse_code, sku, quantity in rows}


def test_basic_atomic_apply_creates_catalog_and_stock(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
) -> None:
    report_id = create_validated_report(
        test_session_factory,
        [(2, "WH-1", "Warehouse 1", "SKU-1", "Product 1", 5)],
    )

    assert run_apply(test_engine, test_session_factory) == WorkerCycleOutcome.COMPLETED

    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        assert report.status == "completed"
        assert report.finished_at is not None
        assert (report.stocks_created, report.stocks_updated, report.stocks_zeroed) == (1, 0, 0)
        assert quantities(session) == {("WH-1", "SKU-1"): 5}
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReportStagingRow)
                .where(ReportStagingRow.report_id == report_id)
            )
            == 0
        )


def test_latest_logical_rows_choose_names_and_real_changes_update_timestamps(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
) -> None:
    old_timestamp = datetime(2020, 1, 1, tzinfo=UTC)
    seed_inventory(
        test_session_factory,
        [("WH", "Warehouse old", "SKU-A", "Product old", 1)],
        timestamp=old_timestamp,
    )
    report_id = create_validated_report(
        test_session_factory,
        [
            (2, "WH", "Warehouse intermediate", "SKU-A", "Product intermediate", 2),
            (3, "WH", "Warehouse final", "SKU-B", "Product B", 3),
            (4, "OTHER", "Other", "SKU-A", "Product final", 4),
        ],
    )

    assert run_apply(test_engine, test_session_factory) == WorkerCycleOutcome.COMPLETED

    with test_session_factory() as session, session.begin():
        warehouse = session.scalar(select(Warehouse).where(Warehouse.code == "WH"))
        product = session.scalar(select(Product).where(Product.sku == "SKU-A"))
        report = session.get(Report, report_id)
        assert warehouse is not None and warehouse.name == "Warehouse final"
        assert product is not None and product.name == "Product final"
        assert warehouse.updated_at > old_timestamp
        assert product.updated_at > old_timestamp
        assert report is not None
        assert (report.stocks_created, report.stocks_updated, report.stocks_zeroed) == (2, 1, 0)


def test_name_only_changes_do_not_count_as_stock_updates(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
) -> None:
    seed_inventory(
        test_session_factory,
        [("WH", "Warehouse old", "SKU", "Product old", 5)],
    )
    report_id = create_validated_report(
        test_session_factory,
        [(2, "WH", "Warehouse new", "SKU", "Product new", 5)],
    )

    assert run_apply(test_engine, test_session_factory) == WorkerCycleOutcome.COMPLETED

    with test_session_factory() as session, session.begin():
        warehouse = session.scalar(select(Warehouse).where(Warehouse.code == "WH"))
        product = session.scalar(select(Product).where(Product.sku == "SKU"))
        report = session.get(Report, report_id)
        assert warehouse is not None and warehouse.name == "Warehouse new"
        assert product is not None and product.name == "Product new"
        assert report is not None
        assert (report.stocks_created, report.stocks_updated, report.stocks_zeroed) == (0, 0, 0)


def test_assignment_snapshot_example_zeroes_only_report_warehouse(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
) -> None:
    seed_inventory(
        test_session_factory,
        [
            ("MSK-1", "Moscow", "A-001", "A1", 12),
            ("MSK-1", "Moscow", "A-002", "A2", 25),
            ("MSK-1", "Moscow", "A-003", "A3", 4),
            ("SPB-1", "Saint Petersburg", "A-001", "A1", 5),
            ("SPB-1", "Saint Petersburg", "A-004", "A4", 9),
        ],
    )
    report_id = create_validated_report(
        test_session_factory,
        [
            (2, "MSK-1", "Moscow", "A-001", "A1", 10),
            (3, "MSK-1", "Moscow", "A-002", "A2", 20),
        ],
    )

    assert run_apply(test_engine, test_session_factory) == WorkerCycleOutcome.COMPLETED

    with test_session_factory() as session, session.begin():
        assert quantities(session) == {
            ("MSK-1", "A-001"): 10,
            ("MSK-1", "A-002"): 20,
            ("MSK-1", "A-003"): 0,
            ("SPB-1", "A-001"): 5,
            ("SPB-1", "A-004"): 9,
        }
        report = session.get(Report, report_id)
        assert report is not None
        assert (report.stocks_created, report.stocks_updated, report.stocks_zeroed) == (0, 2, 1)


def test_explicit_zero_and_identical_repeat_have_exact_counters(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
) -> None:
    seed_inventory(
        test_session_factory,
        [
            ("WH", "Warehouse", "A", "A", 5),
            ("WH", "Warehouse", "B", "B", 0),
            ("OUT", "Outside", "X", "X", 9),
        ],
    )
    rows = [
        (2, "WH", "Warehouse", "A", "A", 0),
        (3, "WH", "Warehouse", "C", "C", 0),
    ]
    first_id = create_validated_report(test_session_factory, rows)

    assert run_apply(test_engine, test_session_factory) == WorkerCycleOutcome.COMPLETED
    with test_session_factory() as session, session.begin():
        first = session.get(Report, first_id)
        assert first is not None
        assert (first.stocks_created, first.stocks_updated, first.stocks_zeroed) == (1, 1, 0)
        assert quantities(session) == {
            ("WH", "A"): 0,
            ("WH", "B"): 0,
            ("WH", "C"): 0,
            ("OUT", "X"): 9,
        }
        warehouse = session.scalar(select(Warehouse).where(Warehouse.code == "WH"))
        product = session.scalar(select(Product).where(Product.sku == "A"))
        balance = session.scalar(
            select(StockBalance)
            .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
            .join(Product, Product.id == StockBalance.product_id)
            .where(Warehouse.code == "WH", Product.sku == "A")
        )
        assert warehouse is not None and product is not None and balance is not None
        unchanged_timestamps = (warehouse.updated_at, product.updated_at, balance.updated_at)

    second_id = create_validated_report(test_session_factory, rows)
    assert run_apply(test_engine, test_session_factory) == WorkerCycleOutcome.COMPLETED
    with test_session_factory() as session, session.begin():
        second = session.get(Report, second_id)
        assert second is not None
        assert (second.stocks_created, second.stocks_updated, second.stocks_zeroed) == (0, 0, 0)
        warehouse = session.scalar(select(Warehouse).where(Warehouse.code == "WH"))
        product = session.scalar(select(Product).where(Product.sku == "A"))
        balance = session.scalar(
            select(StockBalance)
            .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
            .join(Product, Product.id == StockBalance.product_id)
            .where(Warehouse.code == "WH", Product.sku == "A")
        )
        assert warehouse is not None and product is not None and balance is not None
        assert (
            warehouse.updated_at,
            product.updated_at,
            balance.updated_at,
        ) == unchanged_timestamps


def test_two_ready_reports_apply_fifo_and_second_sees_first(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
) -> None:
    first_id = create_validated_report(
        test_session_factory,
        [(2, "WH", "Warehouse", "SKU", "Product", 1)],
    )
    second_id = create_validated_report(
        test_session_factory,
        [(2, "WH", "Warehouse", "SKU", "Product", 2)],
    )

    assert run_apply(test_engine, test_session_factory) == WorkerCycleOutcome.COMPLETED
    with test_session_factory() as session, session.begin():
        assert session.get(Report, first_id).status == "completed"  # type: ignore[union-attr]
        assert session.get(Report, second_id).status == "processing"  # type: ignore[union-attr]
        assert quantities(session) == {("WH", "SKU"): 1}

    assert run_apply(test_engine, test_session_factory) == WorkerCycleOutcome.COMPLETED
    with test_session_factory() as session, session.begin():
        second = session.get(Report, second_id)
        assert second is not None and second.status == "completed"
        assert (second.stocks_created, second.stocks_updated, second.stocks_zeroed) == (0, 1, 0)
        assert quantities(session) == {("WH", "SKU"): 2}


@pytest.mark.parametrize("failing_step", list(ApplyStep))
def test_failure_after_each_apply_phase_rolls_back_every_subject_change(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
    failing_step: ApplyStep,
) -> None:
    seed_inventory(
        test_session_factory,
        [
            ("WH", "Warehouse old", "SKU", "Product old", 5),
            ("WH", "Warehouse old", "MISSING", "Missing", 8),
        ],
    )
    report_id = create_validated_report(
        test_session_factory,
        [(2, "WH", "Warehouse new", "SKU", "Product new", 7)],
    )
    storage = InMemoryStorage()
    with test_session_factory() as session, session.begin():
        report = session.get(Report, report_id)
        assert report is not None
        storage.objects[(report.object_bucket, report.object_key)] = b"original"
        object_bucket = report.object_bucket
        object_key = report.object_key

    def fail_at(step: ApplyStep) -> None:
        if step == failing_step:
            raise RuntimeError(f"injected failure after {step}")

    assert (
        run_apply(test_engine, test_session_factory, storage=storage, hook=fail_at)
        == WorkerCycleOutcome.PROCESSING_FAILED
    )

    with test_session_factory() as session, session.begin():
        warehouse = session.scalar(select(Warehouse).where(Warehouse.code == "WH"))
        product = session.scalar(select(Product).where(Product.sku == "SKU"))
        report = session.get(Report, report_id)
        assert warehouse is not None and warehouse.name == "Warehouse old"
        assert product is not None and product.name == "Product old"
        assert quantities(session) == {("WH", "SKU"): 5, ("WH", "MISSING"): 8}
        assert report is not None
        assert report.status == "failed"
        assert report.finished_at is not None
        assert report.failure_kind == "processing"
        assert (report.stocks_created, report.stocks_updated, report.stocks_zeroed) == (0, 0, 0)
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReportStagingRow)
                .where(ReportStagingRow.report_id == report_id)
            )
            == 0
        )
        error = session.scalar(select(ReportError).where(ReportError.report_id == report_id))
        assert error is not None and error.code == "apply_error"

    assert b"".join(storage.download_stream(object_bucket, object_key)) == b"original"


def test_advisory_lock_is_held_during_apply_and_released_afterward(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
) -> None:
    create_validated_report(
        test_session_factory,
        [(2, "WH", "Warehouse", "SKU", "Product", 1)],
    )
    lock_attempts: list[bool] = []

    def observe_lock(step: ApplyStep) -> None:
        if step == ApplyStep.WAREHOUSES_UPSERTED:
            with test_engine.connect() as contender:
                lock_attempts.append(
                    bool(contender.scalar(select(func.pg_try_advisory_lock(LOCK_KEY))))
                )

    assert (
        run_apply(test_engine, test_session_factory, hook=observe_lock)
        == WorkerCycleOutcome.COMPLETED
    )
    assert lock_attempts == [False]
    with test_engine.connect() as contender:
        assert contender.scalar(select(func.pg_try_advisory_lock(LOCK_KEY))) is True
        assert contender.scalar(select(func.pg_advisory_unlock(LOCK_KEY))) is True


def test_terminal_reports_are_not_applied(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
) -> None:
    create_validated_report(
        test_session_factory,
        [(2, "DONE", "Done", "DONE", "Done", 1)],
        status="completed",
    )
    create_validated_report(
        test_session_factory,
        [(2, "FAILED", "Failed", "FAILED", "Failed", 1)],
        status="failed",
    )

    assert run_apply(test_engine, test_session_factory) == WorkerCycleOutcome.NO_REPORT
    with test_session_factory() as session, session.begin():
        assert quantities(session) == {}


def test_thousands_of_rows_use_fixed_number_of_apply_queries(
    test_engine: Engine,
    test_session_factory: sessionmaker[Session],
) -> None:
    row_count = 3_000
    with test_session_factory() as session, session.begin():
        report = Report(
            status="processing",
            original_filename="large.csv",
            object_bucket="apply-test",
            object_key=f"large/{uuid4()}.csv",
            size_bytes=0,
            checksum_sha256=hashlib.sha256(b"").hexdigest(),
            row_count=row_count,
            error_count=0,
        )
        session.add(report)
        session.flush()
        report_id = report.id
        for batch_start in range(0, row_count, 500):
            session.execute(
                insert(ReportStagingRow),
                [
                    {
                        "report_id": report_id,
                        "line_number": number + 2,
                        "warehouse_code": f"WH-{number}",
                        "warehouse_name": f"Warehouse {number}",
                        "sku": f"SKU-{number}",
                        "product_name": f"Product {number}",
                        "quantity": number,
                    }
                    for number in range(batch_start, min(batch_start + 500, row_count))
                ],
            )

    statements = 0

    def count_statements(*_args: object) -> None:
        nonlocal statements
        statements += 1

    event.listen(test_engine, "before_cursor_execute", count_statements)
    try:
        assert run_apply(test_engine, test_session_factory) == WorkerCycleOutcome.COMPLETED
    finally:
        event.remove(test_engine, "before_cursor_execute", count_statements)

    assert statements <= 25
    with test_session_factory() as session, session.begin():
        loaded_report = session.get(Report, report_id)
        assert loaded_report is not None
        assert loaded_report.stocks_created == row_count
        assert session.scalar(select(func.count()).select_from(StockBalance)) == row_count
