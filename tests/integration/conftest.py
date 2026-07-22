import os
import re
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from alembic.config import Config

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://import_service:import_service@localhost:5432/import_service_test"
)
SAFE_DATABASE_NAME = re.compile(r"^[a-zA-Z0-9_]+_test$")


def _recreate_database(database_url: URL) -> None:
    database_name = database_url.database
    if database_name is None or SAFE_DATABASE_NAME.fullmatch(database_name) is None:
        raise RuntimeError("Integration test database name must end with '_test'")

    admin_url = database_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        admin_engine.dispose()


def _drop_database(database_url: URL) -> None:
    database_name = database_url.database
    if database_name is None or SAFE_DATABASE_NAME.fullmatch(database_name) is None:
        raise RuntimeError("Integration test database name must end with '_test'")

    admin_engine = create_engine(
        database_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> URL:
    database_url = make_url(os.getenv("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL))
    if database_url.get_backend_name() != "postgresql":
        raise RuntimeError("Integration tests require PostgreSQL")
    return database_url


@pytest.fixture(scope="session", autouse=True)
def migrated_database(test_database_url: URL) -> Iterator[None]:
    _recreate_database(test_database_url)

    alembic_config = Config("alembic.ini")
    migration_url = test_database_url.render_as_string(hide_password=False)
    alembic_config.set_main_option("sqlalchemy.url", migration_url)
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = migration_url
    try:
        command.upgrade(alembic_config, "head")
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url

    try:
        yield
    finally:
        _drop_database(test_database_url)


@pytest.fixture(scope="session")
def test_engine(test_database_url: URL, migrated_database: None) -> Iterator[Engine]:
    engine = create_engine(test_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_test_database(test_engine: Engine) -> None:
    with test_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE report_errors, report_staging_rows, stock_balances, "
                "products, warehouses, reports RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture(scope="session")
def test_session_factory(test_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=test_engine, class_=Session, expire_on_commit=False)


@pytest.fixture
def db_session(test_engine: Engine) -> Iterator[Session]:
    with test_engine.connect() as connection:
        outer_transaction = connection.begin()
        session = Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            session.close()
            outer_transaction.rollback()
