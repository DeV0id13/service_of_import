import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from alembic.config import Config
from app.models import Report
from app.schemas import ReportSummary
from app.services.reports import ReportService
from tests.fakes import InMemoryStorage
from tests.integration.conftest import _drop_database, _recreate_database

pytestmark = pytest.mark.integration


@contextmanager
def migration_database_url(database_url: URL) -> Iterator[None]:
    rendered_url = database_url.render_as_string(hide_password=False)
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = rendered_url
    try:
        yield
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url


def test_populated_0001_upgrades_to_nullable_legacy_checksum(
    test_database_url: URL,
) -> None:
    assert test_database_url.database is not None
    chain_url = test_database_url.set(
        database=f"{test_database_url.database.removesuffix('_test')}_migration_chain_test"
    )
    _recreate_database(chain_url)
    engine = create_engine(chain_url)
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option(
        "sqlalchemy.url",
        chain_url.render_as_string(hide_password=False),
    )

    try:
        with migration_database_url(chain_url):
            command.upgrade(alembic_config, "0001_initial_schema")
            with engine.begin() as connection:
                legacy_report_id = connection.execute(
                    text(
                        "INSERT INTO reports "
                        "(status, original_filename, object_bucket, object_key, size_bytes) "
                        "VALUES ('pending', 'legacy.csv', 'legacy', 'legacy/report.csv', 12) "
                        "RETURNING id"
                    )
                ).scalar_one()

            command.upgrade(alembic_config, "head")

            session_factory = sessionmaker(
                bind=engine,
                class_=Session,
                expire_on_commit=False,
            )
            with session_factory() as session, session.begin():
                legacy_report = session.get(Report, legacy_report_id)
                assert legacy_report is not None
                assert legacy_report.original_filename == "legacy.csv"
                assert legacy_report.checksum_sha256 is None
                assert ReportSummary.model_validate(legacy_report).checksum_sha256 is None

            content = b"new report content"
            new_report = ReportService(
                InMemoryStorage(),
                session_factory,
                "migration-chain",
            ).register_original(BytesIO(content), "new.csv")
            expected_checksum = hashlib.sha256(content).hexdigest()
            assert new_report.checksum_sha256 == expected_checksum
            assert len(expected_checksum) == 64
            with session_factory() as session, session.begin():
                stored_checksum = session.scalar(
                    select(Report.checksum_sha256).where(Report.id == new_report.id)
                )
                assert stored_checksum == expected_checksum

            command.check(alembic_config)
    finally:
        engine.dispose()
        _drop_database(chain_url)
