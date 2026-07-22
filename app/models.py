from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint(
            "object_bucket",
            "object_key",
            name="uq_reports_object_bucket_object_key",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="status_allowed",
        ),
        CheckConstraint(
            "row_count >= 0 AND error_count >= 0 "
            "AND stocks_created >= 0 AND stocks_updated >= 0 AND stocks_zeroed >= 0",
            name="nonnegative_counters",
        ),
        CheckConstraint(
            "checksum_sha256 ~ '^[0-9a-f]{64}$'",
            name="checksum_sha256_format",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    original_filename: Mapped[str] = mapped_column(Text)
    object_bucket: Mapped[str] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    stocks_created: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    stocks_updated: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    stocks_zeroed: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    failure_kind: Mapped[str | None] = mapped_column(String(20))
    failure_message: Mapped[str | None] = mapped_column(Text)


Index(
    "ix_reports_queue",
    Report.created_at,
    Report.id,
    postgresql_where=Report.status.in_(("pending", "processing")),
)
Index("ix_reports_created_desc", Report.created_at.desc(), Report.id.desc())


class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (UniqueConstraint("code", name="uq_warehouses_code"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("sku", name="uq_products_sku"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sku: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StockBalance(Base):
    __tablename__ = "stock_balances"
    __table_args__ = (
        PrimaryKeyConstraint("warehouse_id", "product_id", name="pk_stock_balances"),
        CheckConstraint("quantity >= 0", name="quantity_nonnegative"),
        Index("ix_stock_balances_product_id_warehouse_id", "product_id", "warehouse_id"),
    )

    warehouse_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "warehouses.id",
            name="fk_stock_balances_warehouse_id_warehouses",
        ),
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "products.id",
            name="fk_stock_balances_product_id_products",
        ),
    )
    quantity: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReportStagingRow(Base):
    __tablename__ = "report_staging_rows"
    __table_args__ = (
        PrimaryKeyConstraint("report_id", "line_number", name="pk_report_staging_rows"),
        CheckConstraint("quantity >= 0", name="quantity_nonnegative"),
        Index(
            "ix_report_staging_rows_report_id_warehouse_code_sku",
            "report_id",
            "warehouse_code",
            "sku",
        ),
    )

    report_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "reports.id",
            ondelete="CASCADE",
            name="fk_report_staging_rows_report_id_reports",
        ),
    )
    line_number: Mapped[int] = mapped_column(BigInteger)
    warehouse_code: Mapped[str] = mapped_column(Text)
    warehouse_name: Mapped[str] = mapped_column(Text)
    sku: Mapped[str] = mapped_column(Text)
    product_name: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(BigInteger)


class ReportError(Base):
    __tablename__ = "report_errors"
    __table_args__ = (
        Index("ix_report_errors_report_id_line_number_id", "report_id", "line_number", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    report_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "reports.id",
            ondelete="CASCADE",
            name="fk_report_errors_report_id_reports",
        ),
    )
    line_number: Mapped[int | None] = mapped_column(BigInteger)
    field_name: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    raw_data: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
