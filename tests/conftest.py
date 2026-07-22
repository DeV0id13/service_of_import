from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.dependencies import get_readiness_engine, get_storage
from app.main import create_app
from tests.fakes import InMemoryStorage


@pytest.fixture
def client() -> Iterator[TestClient]:
    application = create_app()
    readiness_engine = create_engine("sqlite+pysqlite:///:memory:")
    application.dependency_overrides[get_storage] = InMemoryStorage
    application.dependency_overrides[get_readiness_engine] = lambda: readiness_engine
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        readiness_engine.dispose()
