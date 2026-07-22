import hashlib
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.dependencies import get_report_service, get_storage
from app.errors import ReportPersistenceError
from app.main import create_app
from app.models import Report
from app.services.reports import ReportService
from tests.fakes import InMemoryStorage

pytestmark = pytest.mark.integration
TEST_BUCKET = "stock-reports-test"
TEST_LIMIT = 64


@pytest.fixture
def fake_storage() -> InMemoryStorage:
    return InMemoryStorage(chunk_size=4)


@pytest.fixture
def reports_app(
    fake_storage: InMemoryStorage,
    test_session_factory: sessionmaker[Session],
) -> FastAPI:
    application = create_app()
    service = ReportService(
        fake_storage,
        test_session_factory,
        TEST_BUCKET,
        max_upload_bytes=TEST_LIMIT,
    )
    application.dependency_overrides[get_storage] = lambda: fake_storage
    application.dependency_overrides[get_report_service] = lambda: service
    return application


@pytest.fixture
def reports_client(reports_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(reports_app, raise_server_exceptions=False) as client:
        yield client


def upload(
    client: TestClient,
    content: bytes,
    *,
    filename: str = "report.csv",
) -> dict[str, object]:
    response = client.post(
        "/api/v1/reports",
        files={"file": (filename, content, "text/csv")},
    )
    assert response.status_code == 202, response.text
    result: dict[str, object] = response.json()
    return result


def report_count(session_factory: Callable[[], Session]) -> int:
    with session_factory() as session, session.begin():
        count = session.scalar(select(func.count()).select_from(Report))
    return count or 0


def test_successful_upload_is_pending_and_uses_bounded_reads(
    reports_client: TestClient,
    fake_storage: InMemoryStorage,
) -> None:
    content = b"warehouse_code,sku\nWH-1,SKU-1\n"
    response = reports_client.post(
        "/api/v1/reports",
        files={"file": ("folder/../report.csv", content, "text/csv")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["original_filename"] == "report.csv"
    assert body["size_bytes"] == len(content)
    assert body["checksum_sha256"] == hashlib.sha256(content).hexdigest()
    assert all(0 < size <= fake_storage.chunk_size for size in fake_storage.read_sizes)
    assert "object_bucket" not in body
    assert "object_key" not in body


@pytest.mark.parametrize(
    "content",
    [b"", b"this is not a CSV document"],
    ids=["empty", "invalid-csv"],
)
def test_content_is_registered_without_csv_validation(
    reports_client: TestClient,
    content: bytes,
) -> None:
    body = upload(reports_client, content)
    assert body["status"] == "pending"
    assert body["size_bytes"] == len(content)


def test_exact_size_limit_is_accepted(reports_client: TestClient) -> None:
    body = upload(reports_client, b"x" * TEST_LIMIT)
    assert body["size_bytes"] == TEST_LIMIT


def test_limit_plus_one_returns_413_without_report_or_object(
    reports_client: TestClient,
    fake_storage: InMemoryStorage,
    test_session_factory: sessionmaker[Session],
) -> None:
    response = reports_client.post(
        "/api/v1/reports",
        files={"file": ("large.csv", b"x" * (TEST_LIMIT + 1), "text/csv")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert report_count(test_session_factory) == 0
    assert fake_storage.objects == {}


def test_storage_error_does_not_create_report(
    test_session_factory: sessionmaker[Session],
) -> None:
    storage = InMemoryStorage(fail_upload=True)
    application = create_app()
    application.dependency_overrides[get_storage] = lambda: storage
    application.dependency_overrides[get_report_service] = lambda: ReportService(
        storage, test_session_factory, TEST_BUCKET
    )

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/reports",
            files={"file": ("report.csv", b"data", "text/csv")},
        )

    assert response.status_code == 503
    assert report_count(test_session_factory) == 0


def test_database_error_after_upload_triggers_compensating_delete() -> None:
    storage = InMemoryStorage()

    def failing_session_factory() -> Session:
        raise RuntimeError("simulated database failure")

    service = ReportService(storage, failing_session_factory, TEST_BUCKET)

    with pytest.raises(ReportPersistenceError):
        service.register_original(source=ReadableBytes(b"data"), filename="report.csv")

    assert len(storage.deleted) == 1
    assert storage.objects == {}


def test_list_filter_and_pagination(
    reports_client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    first = upload(reports_client, b"first")
    second = upload(reports_client, b"second")
    third = upload(reports_client, b"third")
    second_id = second["id"]
    assert isinstance(second_id, int)
    with test_session_factory() as session, session.begin():
        session.execute(update(Report).where(Report.id == second_id).values(status="failed"))

    page = reports_client.get("/api/v1/reports?limit=2&offset=0")
    assert page.status_code == 200
    page_body = page.json()
    assert page_body["total"] == 3
    assert page_body["limit"] == 2
    assert page_body["offset"] == 0
    assert [item["id"] for item in page_body["items"]] == [third["id"], second["id"]]

    filtered = reports_client.get("/api/v1/reports?status=failed")
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["id"] == second["id"]
    assert first["id"] != second["id"]


def test_details_and_unknown_report(reports_client: TestClient) -> None:
    created = upload(reports_client, b"details")
    response = reports_client.get(f"/api/v1/reports/{created['id']}")

    assert response.status_code == 200
    details = response.json()
    assert details["row_count"] == 0
    assert details["error_count"] == 0
    assert details["stocks_created"] == 0
    assert "object_bucket" not in details
    assert "object_key" not in details

    missing = reports_client.get("/api/v1/reports/999999")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "report_not_found"


def test_streamed_download_and_unicode_content_disposition(reports_client: TestClient) -> None:
    content = b"streamed-content"
    created = upload(reports_client, content, filename="остатки июля.csv")

    response = reports_client.get(f"/api/v1/reports/{created['id']}/original")

    assert response.status_code == 200
    assert response.content == content
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "%D0%BE%D1%81%D1%82%D0%B0%D1%82%D0%BA%D0%B8" in disposition


def test_missing_registered_object_returns_safe_server_error(
    reports_client: TestClient,
    fake_storage: InMemoryStorage,
) -> None:
    created = upload(reports_client, b"will-disappear")
    fake_storage.objects.clear()

    response = reports_client.get(f"/api/v1/reports/{created['id']}/original")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "original_object_missing",
            "message": "The original report file is unavailable",
        }
    }


def test_missing_file_uses_safe_422_format(reports_client: TestClient) -> None:
    response = reports_client.post("/api/v1/reports")
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "validation_error", "message": "Request validation failed"}
    }


class ReadableBytes:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._position = 0

    def read(self, size: int, /) -> bytes:
        start = self._position
        self._position += size
        return self._content[start : self._position]
