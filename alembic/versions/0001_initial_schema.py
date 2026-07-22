"""Create the initial application schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("object_bucket", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("stocks_created", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("stocks_updated", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("stocks_zeroed", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("failure_kind", sa.String(length=20), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "row_count >= 0 AND error_count >= 0 "
            "AND stocks_created >= 0 AND stocks_updated >= 0 AND stocks_zeroed >= 0",
            name="ck_reports_nonnegative_counters",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_reports_status_allowed",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reports"),
        sa.UniqueConstraint(
            "object_bucket",
            "object_key",
            name="uq_reports_object_bucket_object_key",
        ),
    )
    op.create_index(
        "ix_reports_created_desc",
        "reports",
        [sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_reports_queue",
        "reports",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )

    op.create_table(
        "warehouses",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_warehouses"),
        sa.UniqueConstraint("code", name="uq_warehouses_code"),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("sku", name="uq_products_sku"),
    )
    op.create_table(
        "stock_balances",
        sa.Column("warehouse_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("quantity >= 0", name="ck_stock_balances_quantity_nonnegative"),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_stock_balances_product_id_products",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name="fk_stock_balances_warehouse_id_warehouses",
        ),
        sa.PrimaryKeyConstraint("warehouse_id", "product_id", name="pk_stock_balances"),
    )
    op.create_index(
        "ix_stock_balances_product_id_warehouse_id",
        "stock_balances",
        ["product_id", "warehouse_id"],
        unique=False,
    )
    op.create_table(
        "report_staging_rows",
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("line_number", sa.BigInteger(), nullable=False),
        sa.Column("warehouse_code", sa.Text(), nullable=False),
        sa.Column("warehouse_name", sa.Text(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("product_name", sa.Text(), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "quantity >= 0",
            name="ck_report_staging_rows_quantity_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name="fk_report_staging_rows_report_id_reports",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("report_id", "line_number", name="pk_report_staging_rows"),
    )
    op.create_index(
        "ix_report_staging_rows_report_id_warehouse_code_sku",
        "report_staging_rows",
        ["report_id", "warehouse_code", "sku"],
        unique=False,
    )
    op.create_table(
        "report_errors",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("line_number", sa.BigInteger(), nullable=True),
        sa.Column("field_name", sa.Text(), nullable=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name="fk_report_errors_report_id_reports",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_report_errors"),
    )
    op.create_index(
        "ix_report_errors_report_id_line_number_id",
        "report_errors",
        ["report_id", "line_number", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_report_errors_report_id_line_number_id", table_name="report_errors")
    op.drop_table("report_errors")
    op.drop_index(
        "ix_report_staging_rows_report_id_warehouse_code_sku",
        table_name="report_staging_rows",
    )
    op.drop_table("report_staging_rows")
    op.drop_index(
        "ix_stock_balances_product_id_warehouse_id",
        table_name="stock_balances",
    )
    op.drop_table("stock_balances")
    op.drop_table("products")
    op.drop_table("warehouses")
    op.drop_index("ix_reports_queue", table_name="reports")
    op.drop_index("ix_reports_created_desc", table_name="reports")
    op.drop_table("reports")
