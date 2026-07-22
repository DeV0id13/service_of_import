import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import Report, ReportStagingRow

logger = logging.getLogger(__name__)


class ApplyStep(StrEnum):
    WAREHOUSES_UPSERTED = "warehouses_upserted"
    PRODUCTS_UPSERTED = "products_upserted"
    EXPLICIT_STOCKS_APPLIED = "explicit_stocks_applied"
    MISSING_STOCKS_ZEROED = "missing_stocks_zeroed"
    BEFORE_REPORT_COMPLETED = "before_report_completed"


CounterKind = Literal["created", "updated", "zeroed"]
ApplyStepHook = Callable[[ApplyStep], None]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    report_id: int
    row_count: int
    stocks_created: int
    stocks_updated: int
    stocks_zeroed: int
    duration_seconds: float


class ReportNotReadyForApply(RuntimeError):
    pass


def classify_stock_change(
    *,
    previous_quantity: int | None,
    explicit_quantity: int | None,
    warehouse_in_report: bool,
) -> CounterKind | None:
    """Express the mutually exclusive counter rules for small unit examples."""

    if explicit_quantity is not None:
        if previous_quantity is None:
            return "created"
        if previous_quantity != explicit_quantity:
            return "updated"
        return None
    if warehouse_in_report and previous_quantity not in (None, 0):
        return "zeroed"
    return None


class ApplyReportService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        after_step: ApplyStepHook | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._after_step = after_step

    def apply(
        self,
        report_id: int,
        assert_lock_alive: Callable[[], None],
    ) -> ApplyResult:
        started_at = time.monotonic()
        logger.info(
            "Atomic report apply started",
            extra={"event": "apply_started", "report_id": report_id, "stage": "apply"},
        )

        try:
            with self._session_factory() as session, session.begin():
                assert_lock_alive()
                report = session.scalar(
                    select(Report).where(Report.id == report_id).with_for_update()
                )
                if report is None:
                    raise ReportNotReadyForApply("report no longer exists")

                staging_count = session.scalar(
                    select(func.count())
                    .select_from(ReportStagingRow)
                    .where(ReportStagingRow.report_id == report_id)
                )
                if (
                    report.status != "processing"
                    or report.error_count != 0
                    or report.row_count <= 0
                    or staging_count != report.row_count
                ):
                    raise ReportNotReadyForApply("report validation is not complete")

                self._upsert_warehouses(session, report_id)
                self._log_step(ApplyStep.WAREHOUSES_UPSERTED, report_id)
                self._run_hook(ApplyStep.WAREHOUSES_UPSERTED)
                assert_lock_alive()

                self._upsert_products(session, report_id)
                self._log_step(ApplyStep.PRODUCTS_UPSERTED, report_id)
                self._run_hook(ApplyStep.PRODUCTS_UPSERTED)
                assert_lock_alive()

                stocks_created, stocks_updated, stocks_zeroed = self._calculate_counters(
                    session, report_id
                )

                self._apply_explicit_stocks(session, report_id)
                self._log_step(ApplyStep.EXPLICIT_STOCKS_APPLIED, report_id)
                self._run_hook(ApplyStep.EXPLICIT_STOCKS_APPLIED)
                assert_lock_alive()

                self._zero_missing_stocks(session, report_id)
                self._log_step(ApplyStep.MISSING_STOCKS_ZEROED, report_id)
                self._run_hook(ApplyStep.MISSING_STOCKS_ZEROED)
                assert_lock_alive()

                self._run_hook(ApplyStep.BEFORE_REPORT_COMPLETED)
                report.stocks_created = stocks_created
                report.stocks_updated = stocks_updated
                report.stocks_zeroed = stocks_zeroed
                report.status = "completed"
                report.finished_at = func.now()
                report.failure_kind = None
                report.failure_message = None
                session.execute(
                    text("DELETE FROM report_staging_rows WHERE report_id = :report_id"),
                    {"report_id": report_id},
                )
                session.flush()
                assert_lock_alive()
                row_count = report.row_count
        except Exception:
            duration = time.monotonic() - started_at
            logger.exception(
                "Atomic report apply rolled back",
                extra={
                    "event": "apply_rolled_back",
                    "report_id": report_id,
                    "stage": "apply",
                    "duration_seconds": duration,
                },
            )
            raise

        duration = time.monotonic() - started_at
        result = ApplyResult(
            report_id=report_id,
            row_count=row_count,
            stocks_created=stocks_created,
            stocks_updated=stocks_updated,
            stocks_zeroed=stocks_zeroed,
            duration_seconds=duration,
        )
        completion_context = {
            "report_id": report_id,
            "stage": "apply",
            "status": "completed",
            "row_count": row_count,
            "stocks_created": stocks_created,
            "stocks_updated": stocks_updated,
            "stocks_zeroed": stocks_zeroed,
            "duration_seconds": duration,
        }
        logger.info(
            "Atomic report apply completed",
            extra={"event": "apply_completed", **completion_context},
        )
        logger.info(
            "Report completed",
            extra={"event": "report_completed", **completion_context},
        )
        return result

    @staticmethod
    def _upsert_warehouses(session: Session, report_id: int) -> None:
        session.execute(
            text(
                """
                INSERT INTO warehouses (code, name, created_at, updated_at)
                SELECT source.code, source.name, now(), now()
                FROM (
                    SELECT DISTINCT ON (warehouse_code)
                        warehouse_code AS code,
                        warehouse_name AS name
                    FROM report_staging_rows
                    WHERE report_id = :report_id
                    ORDER BY warehouse_code, line_number DESC
                ) AS source
                ON CONFLICT (code) DO UPDATE
                SET name = EXCLUDED.name,
                    updated_at = now()
                WHERE warehouses.name IS DISTINCT FROM EXCLUDED.name
                """
            ),
            {"report_id": report_id},
        )

    @staticmethod
    def _upsert_products(session: Session, report_id: int) -> None:
        session.execute(
            text(
                """
                INSERT INTO products (sku, name, created_at, updated_at)
                SELECT source.sku, source.name, now(), now()
                FROM (
                    SELECT DISTINCT ON (sku)
                        sku,
                        product_name AS name
                    FROM report_staging_rows
                    WHERE report_id = :report_id
                    ORDER BY sku, line_number DESC
                ) AS source
                ON CONFLICT (sku) DO UPDATE
                SET name = EXCLUDED.name,
                    updated_at = now()
                WHERE products.name IS DISTINCT FROM EXCLUDED.name
                """
            ),
            {"report_id": report_id},
        )

    @staticmethod
    def _calculate_counters(session: Session, report_id: int) -> tuple[int, int, int]:
        row = session.execute(
            text(
                """
                WITH explicit_pairs AS (
                    SELECT
                        warehouse.id AS warehouse_id,
                        product.id AS product_id,
                        staging.quantity
                    FROM report_staging_rows AS staging
                    JOIN warehouses AS warehouse ON warehouse.code = staging.warehouse_code
                    JOIN products AS product ON product.sku = staging.sku
                    WHERE staging.report_id = :report_id
                ),
                affected_warehouses AS (
                    SELECT DISTINCT warehouse.id AS warehouse_id
                    FROM report_staging_rows AS staging
                    JOIN warehouses AS warehouse ON warehouse.code = staging.warehouse_code
                    WHERE staging.report_id = :report_id
                )
                SELECT
                    count(*) FILTER (WHERE balance.warehouse_id IS NULL) AS stocks_created,
                    count(*) FILTER (
                        WHERE balance.warehouse_id IS NOT NULL
                          AND balance.quantity IS DISTINCT FROM explicit.quantity
                    ) AS stocks_updated,
                    (
                        SELECT count(*)
                        FROM stock_balances AS missing_balance
                        JOIN affected_warehouses AS affected
                          ON affected.warehouse_id = missing_balance.warehouse_id
                        WHERE missing_balance.quantity <> 0
                          AND NOT EXISTS (
                              SELECT 1
                              FROM explicit_pairs AS present
                              WHERE present.warehouse_id = missing_balance.warehouse_id
                                AND present.product_id = missing_balance.product_id
                          )
                    ) AS stocks_zeroed
                FROM explicit_pairs AS explicit
                LEFT JOIN stock_balances AS balance
                  ON balance.warehouse_id = explicit.warehouse_id
                 AND balance.product_id = explicit.product_id
                """
            ),
            {"report_id": report_id},
        ).one()
        return int(row[0]), int(row[1]), int(row[2])

    @staticmethod
    def _apply_explicit_stocks(session: Session, report_id: int) -> None:
        session.execute(
            text(
                """
                INSERT INTO stock_balances (
                    warehouse_id,
                    product_id,
                    quantity,
                    created_at,
                    updated_at
                )
                SELECT
                    warehouse.id,
                    product.id,
                    staging.quantity,
                    now(),
                    now()
                FROM report_staging_rows AS staging
                JOIN warehouses AS warehouse ON warehouse.code = staging.warehouse_code
                JOIN products AS product ON product.sku = staging.sku
                WHERE staging.report_id = :report_id
                ON CONFLICT (warehouse_id, product_id) DO UPDATE
                SET quantity = EXCLUDED.quantity,
                    updated_at = now()
                WHERE stock_balances.quantity IS DISTINCT FROM EXCLUDED.quantity
                """
            ),
            {"report_id": report_id},
        )

    @staticmethod
    def _zero_missing_stocks(session: Session, report_id: int) -> None:
        session.execute(
            text(
                """
                WITH affected_warehouses AS (
                    SELECT DISTINCT warehouse.id AS warehouse_id
                    FROM report_staging_rows AS staging
                    JOIN warehouses AS warehouse ON warehouse.code = staging.warehouse_code
                    WHERE staging.report_id = :report_id
                ),
                explicit_pairs AS (
                    SELECT
                        warehouse.id AS warehouse_id,
                        product.id AS product_id
                    FROM report_staging_rows AS staging
                    JOIN warehouses AS warehouse ON warehouse.code = staging.warehouse_code
                    JOIN products AS product ON product.sku = staging.sku
                    WHERE staging.report_id = :report_id
                )
                UPDATE stock_balances AS balance
                SET quantity = 0,
                    updated_at = now()
                FROM affected_warehouses AS affected
                WHERE balance.warehouse_id = affected.warehouse_id
                  AND balance.quantity <> 0
                  AND NOT EXISTS (
                      SELECT 1
                      FROM explicit_pairs AS present
                      WHERE present.warehouse_id = balance.warehouse_id
                        AND present.product_id = balance.product_id
                  )
                """
            ),
            {"report_id": report_id},
        )

    def _run_hook(self, step: ApplyStep) -> None:
        if self._after_step is not None:
            self._after_step(step)

    @staticmethod
    def _log_step(step: ApplyStep, report_id: int) -> None:
        messages = {
            ApplyStep.WAREHOUSES_UPSERTED: "Warehouses upserted",
            ApplyStep.PRODUCTS_UPSERTED: "Products upserted",
            ApplyStep.EXPLICIT_STOCKS_APPLIED: "Explicit stocks applied",
            ApplyStep.MISSING_STOCKS_ZEROED: "Missing stocks zeroed",
            ApplyStep.BEFORE_REPORT_COMPLETED: "Report ready for completion",
        }
        logger.info(
            messages[step],
            extra={"event": step.value, "report_id": report_id, "stage": "apply"},
        )
