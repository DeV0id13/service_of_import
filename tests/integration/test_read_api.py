import hashlib
from collections.abc import Iterator, Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.dependencies import get_read_api_service, get_report_service
from app.main import create_app
from app.models import Product, Report, ReportError, StockBalance, Warehouse
from app.services.read_api import ReadApiService
from app.services.reports import ReportService
from tests.fakes import InMemoryStorage

pytestmark = pytest.mark.integration
READ_API_BUCKET = "read-api-test"


@pytest.fixture
def read_app(test_session_factory: sessionmaker[Session]) -> FastAPI:
    application = create_app()
    storage = InMemoryStorage()
    application.dependency_overrides[get_read_api_service] = lambda: ReadApiService(
        test_session_factory
    )
    application.dependency_overrides[get_report_service] = lambda: ReportService(
        storage,
        test_session_factory,
        READ_API_BUCKET,
    )
    return application


@pytest.fixture
def read_client(read_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(read_app, raise_server_exceptions=False) as client:
        yield client


def seed_inventory(
    session_factory: sessionmaker[Session],
) -> tuple[dict[str, int], dict[str, int]]:
    with session_factory() as session, session.begin():
        warehouses = {
            "WH-B": Warehouse(code="WH-B", name="Beta Depot"),
            "WH-A": Warehouse(code="WH-A", name="Alpha Depot"),
        }
        products = {
            "SKU-B": Product(sku="SKU-B", name="Beta Product"),
            "SKU-A": Product(sku="SKU-A", name="Alpha Product"),
        }
        session.add_all([*warehouses.values(), *products.values()])
        session.flush()
        session.add_all(
            [
                StockBalance(
                    warehouse_id=warehouses["WH-A"].id,
                    product_id=products["SKU-A"].id,
                    quantity=5,
                ),
                StockBalance(
                    warehouse_id=warehouses["WH-A"].id,
                    product_id=products["SKU-B"].id,
                    quantity=0,
                ),
                StockBalance(
                    warehouse_id=warehouses["WH-B"].id,
                    product_id=products["SKU-A"].id,
                    quantity=7,
                ),
            ]
        )
        warehouse_ids = {code: warehouse.id for code, warehouse in warehouses.items()}
        product_ids = {sku: product.id for sku, product in products.items()}
    return warehouse_ids, product_ids


def create_report(
    session_factory: sessionmaker[Session],
    *,
    status: str = "failed",
    errors: Sequence[dict[str, object]] = (),
) -> int:
    with session_factory() as session, session.begin():
        report = Report(
            status=status,
            original_filename="errors.csv",
            object_bucket=READ_API_BUCKET,
            object_key=f"errors/{status}/{len(errors)}.csv",
            size_bytes=0,
            checksum_sha256=hashlib.sha256(b"").hexdigest(),
        )
        session.add(report)
        session.flush()
        for error in errors:
            session.add(ReportError(report_id=report.id, **error))
        report_id = report.id
    return report_id


def test_warehouses_empty_sort_paginate_filter_search_and_details(
    read_client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    empty = read_client.get("/api/v1/warehouses")
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "limit": 50, "offset": 0, "total": 0}

    warehouse_ids, _ = seed_inventory(test_session_factory)

    page = read_client.get("/api/v1/warehouses?limit=1&offset=1")
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert [item["code"] for item in page.json()["items"]] == ["WH-B"]

    exact = read_client.get("/api/v1/warehouses?code=WH-A")
    assert [item["code"] for item in exact.json()["items"]] == ["WH-A"]
    by_code = read_client.get("/api/v1/warehouses?query=wh-a")
    assert [item["code"] for item in by_code.json()["items"]] == ["WH-A"]
    by_name = read_client.get("/api/v1/warehouses?query=ALPHA")
    assert [item["code"] for item in by_name.json()["items"]] == ["WH-A"]

    detail = read_client.get(f"/api/v1/warehouses/{warehouse_ids['WH-A']}")
    assert detail.status_code == 200
    assert set(detail.json()) == {"id", "code", "name", "created_at", "updated_at"}
    missing = read_client.get("/api/v1/warehouses/999999")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "warehouse_not_found"


def test_products_empty_sort_paginate_filter_search_and_details(
    read_client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    empty = read_client.get("/api/v1/products")
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0

    _, product_ids = seed_inventory(test_session_factory)

    page = read_client.get("/api/v1/products?limit=1&offset=0")
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert [item["sku"] for item in page.json()["items"]] == ["SKU-A"]

    exact = read_client.get("/api/v1/products?sku=SKU-B")
    assert [item["sku"] for item in exact.json()["items"]] == ["SKU-B"]
    by_sku = read_client.get("/api/v1/products?query=sku-b")
    assert [item["sku"] for item in by_sku.json()["items"]] == ["SKU-B"]
    by_name = read_client.get("/api/v1/products?query=beta")
    assert [item["sku"] for item in by_name.json()["items"]] == ["SKU-B"]

    detail = read_client.get(f"/api/v1/products/{product_ids['SKU-A']}")
    assert detail.status_code == 200
    assert set(detail.json()) == {"id", "sku", "name", "created_at", "updated_at"}
    missing = read_client.get("/api/v1/products/999999")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "product_not_found"


def test_stocks_denormalization_filters_zeros_pagination_and_sorting(
    read_client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    empty = read_client.get("/api/v1/stocks")
    assert empty.status_code == 200
    assert empty.json()["items"] == []

    warehouse_ids, product_ids = seed_inventory(test_session_factory)

    response = read_client.get("/api/v1/stocks")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert [(item["warehouse_code"], item["sku"]) for item in body["items"]] == [
        ("WH-A", "SKU-A"),
        ("WH-A", "SKU-B"),
        ("WH-B", "SKU-A"),
    ]
    assert body["items"][1]["quantity"] == 0
    assert set(body["items"][0]) == {
        "warehouse_id",
        "warehouse_code",
        "warehouse_name",
        "product_id",
        "sku",
        "product_name",
        "quantity",
        "created_at",
        "updated_at",
    }

    filters = [
        (f"warehouse_id={warehouse_ids['WH-A']}", 2),
        ("warehouse_code=WH-B", 1),
        (f"product_id={product_ids['SKU-A']}", 2),
        ("sku=SKU-B", 1),
        ("warehouse_code=WH-A&sku=SKU-A", 1),
        ("warehouse_code=UNKNOWN", 0),
    ]
    for query, expected_total in filters:
        filtered = read_client.get(f"/api/v1/stocks?{query}")
        assert filtered.status_code == 200
        assert filtered.json()["total"] == expected_total

    nonzero = read_client.get("/api/v1/stocks?include_zero=false")
    assert nonzero.status_code == 200
    assert nonzero.json()["total"] == 2
    assert all(item["quantity"] > 0 for item in nonzero.json()["items"])

    page = read_client.get("/api/v1/stocks?limit=1&offset=1")
    assert page.json()["limit"] == 1
    assert page.json()["offset"] == 1
    assert page.json()["total"] == 3
    assert page.json()["items"][0]["sku"] == "SKU-B"


def test_stocks_use_two_queries_independent_of_page_size(
    read_client: TestClient,
    test_session_factory: sessionmaker[Session],
    test_engine: Engine,
) -> None:
    seed_inventory(test_session_factory)
    stock_selects: list[str] = []

    def capture_stock_queries(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.lower()
        if normalized.startswith("select") and "stock_balances" in normalized:
            stock_selects.append(statement)

    event.listen(test_engine, "before_cursor_execute", capture_stock_queries)
    try:
        assert read_client.get("/api/v1/stocks?limit=1").status_code == 200
        first_request_count = len(stock_selects)
        stock_selects.clear()
        assert read_client.get("/api/v1/stocks?limit=200").status_code == 200
        full_request_count = len(stock_selects)
    finally:
        event.remove(test_engine, "before_cursor_execute", capture_stock_queries)

    assert first_request_count == 2
    assert full_request_count == 2


def test_report_errors_not_found_empty_json_sort_and_pagination(
    read_client: TestClient,
    test_session_factory: sessionmaker[Session],
) -> None:
    missing = read_client.get("/api/v1/reports/999999/errors")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "report_not_found"

    empty_report_id = create_report(test_session_factory, status="completed")
    empty = read_client.get(f"/api/v1/reports/{empty_report_id}/errors")
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "limit": 50, "offset": 0, "total": 0}

    report_id = create_report(
        test_session_factory,
        errors=[
            {
                "line_number": 3,
                "field_name": "quantity",
                "code": "quantity_negative",
                "message": "Quantity must be non-negative",
                "raw_data": {"quantity": "-1"},
            },
            {
                "line_number": None,
                "field_name": None,
                "code": "apply_error",
                "message": "Atomic report apply failed",
                "raw_data": None,
            },
            {
                "line_number": 2,
                "field_name": "sku",
                "code": "required",
                "message": "SKU is required",
                "raw_data": {"sku": ""},
            },
        ],
    )

    response = read_client.get(f"/api/v1/reports/{report_id}/errors?limit=2&offset=0")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert [item["line_number"] for item in body["items"]] == [None, 2]
    assert body["items"][1]["raw_data"] == {"sku": ""}
    assert set(body["items"][0]) == {
        "id",
        "line_number",
        "field_name",
        "code",
        "message",
        "raw_data",
        "created_at",
    }
    assert "object_bucket" not in body
    assert "object_key" not in body

    second_page = read_client.get(f"/api/v1/reports/{report_id}/errors?limit=2&offset=2")
    assert [item["line_number"] for item in second_page.json()["items"]] == [3]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/warehouses?limit=0",
        "/api/v1/products?limit=201",
        "/api/v1/stocks?offset=-1",
        "/api/v1/stocks?warehouse_id=0",
        "/api/v1/reports/1/errors?limit=0",
    ],
)
def test_invalid_read_query_parameters_use_safe_422(
    read_client: TestClient,
    path: str,
) -> None:
    response = read_client.get(path)
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "validation_error", "message": "Request validation failed"}
    }


def test_openapi_documents_read_endpoints(read_client: TestClient) -> None:
    response = read_client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    expected_paths = {
        "/api/v1/warehouses",
        "/api/v1/warehouses/{warehouse_id}",
        "/api/v1/products",
        "/api/v1/products/{product_id}",
        "/api/v1/stocks",
        "/api/v1/reports/{report_id}/errors",
    }
    assert expected_paths <= paths.keys()
    for path in expected_paths:
        operation = paths[path]["get"]
        assert operation["summary"]
        assert operation["description"]
        assert operation["responses"]["422"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
