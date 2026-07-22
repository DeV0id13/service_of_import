from collections.abc import Iterator
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from app.config import get_settings
from app.dependencies import get_readiness_engine, get_storage
from app.main import create_app
from app.services.storage import S3ObjectStorage
from tests.fakes import InMemoryStorage

pytestmark = pytest.mark.integration


class UnavailableStorage(InMemoryStorage):
    def is_available(self, bucket: str) -> bool:
        return False


@pytest.fixture
def health_client(test_engine: Engine) -> Iterator[TestClient]:
    application = create_app()
    application.dependency_overrides[get_readiness_engine] = lambda: test_engine
    application.dependency_overrides[get_storage] = lambda: S3ObjectStorage(get_settings())
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def assert_live(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_tracks_database_and_minio_and_recovers(
    health_client: TestClient,
    test_engine: Engine,
) -> None:
    application = cast(FastAPI, health_client.app)

    ready = health_client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok"}
    assert_live(health_client)

    unavailable_database = create_engine(
        "postgresql+psycopg://unavailable:unavailable@127.0.0.1:1/unavailable",
        connect_args={"connect_timeout": 1},
    )
    application.dependency_overrides[get_readiness_engine] = lambda: unavailable_database
    try:
        not_ready = health_client.get("/health/ready")
        assert not_ready.status_code == 503
        assert not_ready.json() == {
            "error": {
                "code": "dependency_unavailable",
                "message": "A required service is temporarily unavailable",
            }
        }
        assert "postgresql" not in not_ready.text.lower()
        assert_live(health_client)
    finally:
        unavailable_database.dispose()

    application.dependency_overrides[get_readiness_engine] = lambda: test_engine
    assert health_client.get("/health/ready").status_code == 200

    application.dependency_overrides[get_storage] = UnavailableStorage
    not_ready = health_client.get("/health/ready")
    assert not_ready.status_code == 503
    assert not_ready.json()["error"]["code"] == "dependency_unavailable"
    assert_live(health_client)

    application.dependency_overrides[get_storage] = lambda: S3ObjectStorage(get_settings())
    assert health_client.get("/health/ready").status_code == 200
