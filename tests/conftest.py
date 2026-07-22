from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_storage
from app.main import create_app
from tests.fakes import InMemoryStorage


@pytest.fixture
def client() -> Iterator[TestClient]:
    application = create_app()
    application.dependency_overrides[get_storage] = InMemoryStorage
    with TestClient(application) as test_client:
        yield test_client
