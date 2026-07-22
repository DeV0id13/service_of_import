from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.dependencies import get_read_api_service
from app.schemas import (
    ErrorResponse,
    ProductListResponse,
    ProductResponse,
    StockListResponse,
    WarehouseListResponse,
    WarehouseResponse,
)
from app.services.read_api import ReadApiService

router = APIRouter(prefix="/api/v1")
ReadServiceDependency = Annotated[ReadApiService, Depends(get_read_api_service)]


@router.get(
    "/warehouses",
    tags=["warehouses"],
    response_model=WarehouseListResponse,
    summary="List warehouses",
    description="Return warehouses with stable sorting and optional exact or text filters.",
    responses={422: {"model": ErrorResponse, "description": "Invalid query parameters."}},
)
def list_warehouses(
    service: ReadServiceDependency,
    code: Annotated[
        str | None,
        Query(description="Exact case-sensitive warehouse code."),
    ] = None,
    query: Annotated[
        str | None,
        Query(description="Case-insensitive substring search in warehouse code or name."),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=200, description="Maximum number of items to return."),
    ] = 50,
    offset: Annotated[
        int,
        Query(ge=0, description="Number of sorted items to skip."),
    ] = 0,
) -> WarehouseListResponse:
    page = service.list_warehouses(code=code, query=query, limit=limit, offset=offset)
    return WarehouseListResponse.model_validate(page)


@router.get(
    "/warehouses/{warehouse_id}",
    tags=["warehouses"],
    response_model=WarehouseResponse,
    summary="Get warehouse",
    description="Return one warehouse by its internal identifier.",
    responses={
        404: {"model": ErrorResponse, "description": "Warehouse not found."},
        422: {"model": ErrorResponse, "description": "Invalid path parameter."},
    },
)
def get_warehouse(
    service: ReadServiceDependency,
    warehouse_id: Annotated[int, Path(gt=0, description="Warehouse identifier.")],
) -> WarehouseResponse:
    return WarehouseResponse.model_validate(service.get_warehouse(warehouse_id))


@router.get(
    "/products",
    tags=["products"],
    response_model=ProductListResponse,
    summary="List products",
    description="Return products with stable sorting and optional exact or text filters.",
    responses={422: {"model": ErrorResponse, "description": "Invalid query parameters."}},
)
def list_products(
    service: ReadServiceDependency,
    sku: Annotated[
        str | None,
        Query(description="Exact case-sensitive product SKU."),
    ] = None,
    query: Annotated[
        str | None,
        Query(description="Case-insensitive substring search in SKU or product name."),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=200, description="Maximum number of items to return."),
    ] = 50,
    offset: Annotated[
        int,
        Query(ge=0, description="Number of sorted items to skip."),
    ] = 0,
) -> ProductListResponse:
    page = service.list_products(sku=sku, query=query, limit=limit, offset=offset)
    return ProductListResponse.model_validate(page)


@router.get(
    "/products/{product_id}",
    tags=["products"],
    response_model=ProductResponse,
    summary="Get product",
    description="Return one product by its internal identifier.",
    responses={
        404: {"model": ErrorResponse, "description": "Product not found."},
        422: {"model": ErrorResponse, "description": "Invalid path parameter."},
    },
)
def get_product(
    service: ReadServiceDependency,
    product_id: Annotated[int, Path(gt=0, description="Product identifier.")],
) -> ProductResponse:
    return ProductResponse.model_validate(service.get_product(product_id))


@router.get(
    "/stocks",
    tags=["stocks"],
    response_model=StockListResponse,
    summary="List stock balances",
    description=(
        "Return denormalized stock balances. Zero quantities are included unless explicitly "
        "disabled."
    ),
    responses={422: {"model": ErrorResponse, "description": "Invalid query parameters."}},
)
def list_stocks(
    service: ReadServiceDependency,
    warehouse_id: Annotated[
        int | None,
        Query(gt=0, description="Filter by warehouse identifier."),
    ] = None,
    warehouse_code: Annotated[
        str | None,
        Query(description="Filter by exact case-sensitive warehouse code."),
    ] = None,
    product_id: Annotated[
        int | None,
        Query(gt=0, description="Filter by product identifier."),
    ] = None,
    sku: Annotated[
        str | None,
        Query(description="Filter by exact case-sensitive product SKU."),
    ] = None,
    include_zero: Annotated[
        bool,
        Query(description="Include balances whose quantity is zero."),
    ] = True,
    limit: Annotated[
        int,
        Query(ge=1, le=200, description="Maximum number of items to return."),
    ] = 50,
    offset: Annotated[
        int,
        Query(ge=0, description="Number of sorted items to skip."),
    ] = 0,
) -> StockListResponse:
    page = service.list_stocks(
        warehouse_id=warehouse_id,
        warehouse_code=warehouse_code,
        product_id=product_id,
        sku=sku,
        include_zero=include_zero,
        limit=limit,
        offset=offset,
    )
    return StockListResponse.model_validate(page)
