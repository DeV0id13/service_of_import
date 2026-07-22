"""Add the original file SHA-256 checksum.

Revision ID: 0002_add_report_checksum
Revises: 0001_initial_schema
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_add_report_checksum"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
    )
    op.create_check_constraint(
        "ck_reports_checksum_sha256_format",
        "reports",
        "checksum_sha256 ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_reports_checksum_sha256_format",
        "reports",
        type_="check",
    )
    op.drop_column("reports", "checksum_sha256")
