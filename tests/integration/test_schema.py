from collections.abc import Callable

import pytest
from sqlalchemy import Engine, delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Product,
    Report,
    ReportError,
    ReportStagingRow,
    StockBalance,
    Warehouse,
)

pytestmark = pytest.mark.integration


def make_report(**overrides: object) -> Report:
    values: dict[str, object] = {
        "status": "pending",
        "original_filename": "report.csv",
        "object_bucket": "stock-reports",
        "object_key": "reports/default.csv",
        "size_bytes": 100,
        "checksum_sha256": "0" * 64,
    }
    values.update(overrides)
    return Report(**values)


def test_initial_migration_created_expected_tables(test_engine: Engine) -> None:
    table_names = set(inspect(test_engine).get_table_names())
    assert {
        "alembic_version",
        "products",
        "report_errors",
        "report_staging_rows",
        "reports",
        "stock_balances",
        "warehouses",
    } <= table_names


@pytest.mark.parametrize(
    ("factory", "duplicate_factory"),
    [
        (
            lambda: Warehouse(code="WH-1", name="Warehouse 1"),
            lambda: Warehouse(code="WH-1", name="Other name"),
        ),
        (
            lambda: Product(sku="SKU-1", name="Product 1"),
            lambda: Product(sku="SKU-1", name="Other name"),
        ),
    ],
    ids=["warehouse-code", "product-sku"],
)
def test_catalog_codes_are_unique(
    db_session: Session,
    factory: Callable[[], Warehouse | Product],
    duplicate_factory: Callable[[], Warehouse | Product],
) -> None:
    with pytest.raises(IntegrityError), db_session.begin():
        db_session.add_all([factory(), duplicate_factory()])
        db_session.flush()


def test_stock_balance_pair_is_unique(db_session: Session) -> None:
    with pytest.raises(IntegrityError), db_session.begin():
        warehouse = Warehouse(code="WH-PAIR", name="Warehouse")
        product = Product(sku="SKU-PAIR", name="Product")
        db_session.add_all([warehouse, product])
        db_session.flush()
        db_session.add_all(
            [
                StockBalance(warehouse_id=warehouse.id, product_id=product.id, quantity=1),
                StockBalance(warehouse_id=warehouse.id, product_id=product.id, quantity=2),
            ]
        )
        db_session.flush()


def test_stock_balance_rejects_negative_quantity(db_session: Session) -> None:
    with pytest.raises(IntegrityError), db_session.begin():
        warehouse = Warehouse(code="WH-NEG", name="Warehouse")
        product = Product(sku="SKU-NEG", name="Product")
        db_session.add_all([warehouse, product])
        db_session.flush()
        db_session.add(StockBalance(warehouse_id=warehouse.id, product_id=product.id, quantity=-1))
        db_session.flush()


def test_staging_rejects_negative_quantity(db_session: Session) -> None:
    with pytest.raises(IntegrityError), db_session.begin():
        report = make_report(object_key="reports/negative-staging.csv")
        db_session.add(report)
        db_session.flush()
        db_session.add(
            ReportStagingRow(
                report_id=report.id,
                line_number=2,
                warehouse_code="WH",
                warehouse_name="Warehouse",
                sku="SKU",
                product_name="Product",
                quantity=-1,
            )
        )
        db_session.flush()


def test_report_rejects_unknown_status(db_session: Session) -> None:
    with pytest.raises(IntegrityError), db_session.begin():
        db_session.add(make_report(status="unknown", object_key="reports/unknown-status.csv"))
        db_session.flush()


@pytest.mark.parametrize(
    "counter_name",
    ["row_count", "error_count", "stocks_created", "stocks_updated", "stocks_zeroed"],
)
def test_report_rejects_negative_counters(db_session: Session, counter_name: str) -> None:
    with pytest.raises(IntegrityError), db_session.begin():
        db_session.add(
            make_report(
                object_key=f"reports/negative-{counter_name}.csv",
                **{counter_name: -1},
            )
        )
        db_session.flush()


def test_staging_allows_duplicate_pair_on_different_lines(db_session: Session) -> None:
    with db_session.begin():
        report = make_report(object_key="reports/duplicate-pair.csv")
        db_session.add(report)
        db_session.flush()
        db_session.add_all(
            [
                ReportStagingRow(
                    report_id=report.id,
                    line_number=line_number,
                    warehouse_code="WH",
                    warehouse_name=f"Warehouse {line_number}",
                    sku="SKU",
                    product_name=f"Product {line_number}",
                    quantity=line_number,
                )
                for line_number in (2, 3)
            ]
        )

    count = db_session.scalar(
        select(func.count())
        .select_from(ReportStagingRow)
        .where(ReportStagingRow.report_id == report.id)
    )
    assert count == 2


def test_report_children_require_existing_report(db_session: Session) -> None:
    with pytest.raises(IntegrityError), db_session.begin():
        db_session.add(
            ReportStagingRow(
                report_id=999_999,
                line_number=2,
                warehouse_code="WH",
                warehouse_name="Warehouse",
                sku="SKU",
                product_name="Product",
                quantity=1,
            )
        )
        db_session.flush()


def test_deleting_report_cascades_staging_and_errors(db_session: Session) -> None:
    with db_session.begin():
        report = make_report(object_key="reports/cascade.csv")
        db_session.add(report)
        db_session.flush()
        db_session.add(
            ReportStagingRow(
                report_id=report.id,
                line_number=2,
                warehouse_code="WH",
                warehouse_name="Warehouse",
                sku="SKU",
                product_name="Product",
                quantity=1,
            )
        )
        db_session.add(
            ReportError(
                report_id=report.id,
                line_number=2,
                field_name="quantity",
                code="invalid_quantity",
                message="Invalid quantity",
                raw_data={"quantity": "bad"},
            )
        )
        db_session.flush()

        db_session.execute(delete(Report).where(Report.id == report.id))

        staging_count = db_session.scalar(
            select(func.count())
            .select_from(ReportStagingRow)
            .where(ReportStagingRow.report_id == report.id)
        )
        error_count = db_session.scalar(
            select(func.count()).select_from(ReportError).where(ReportError.report_id == report.id)
        )

    assert staging_count == 0
    assert error_count == 0


def test_stock_restricts_catalog_deletion(db_session: Session) -> None:
    with pytest.raises(IntegrityError), db_session.begin():
        warehouse = Warehouse(code="WH-RESTRICT", name="Warehouse")
        product = Product(sku="SKU-RESTRICT", name="Product")
        db_session.add_all([warehouse, product])
        db_session.flush()
        db_session.add(StockBalance(warehouse_id=warehouse.id, product_id=product.id, quantity=1))
        db_session.flush()
        db_session.delete(warehouse)
        db_session.flush()
