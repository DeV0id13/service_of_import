from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.errors import ProductNotFoundError, WarehouseNotFoundError
from app.models import Product, StockBalance, Warehouse


@dataclass(frozen=True, slots=True)
class WarehousePage:
    items: list[Warehouse]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ProductPage:
    items: list[Product]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class StockReadModel:
    warehouse_id: int
    warehouse_code: str
    warehouse_name: str
    product_id: int
    sku: str
    product_name: str
    quantity: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StockPage:
    items: list[StockReadModel]
    total: int
    limit: int
    offset: int


class ReadApiService:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def list_warehouses(
        self,
        *,
        code: str | None,
        query: str | None,
        limit: int,
        offset: int,
    ) -> WarehousePage:
        item_query = select(Warehouse)
        count_query = select(func.count()).select_from(Warehouse)
        if code is not None:
            item_query = item_query.where(Warehouse.code == code)
            count_query = count_query.where(Warehouse.code == code)
        if query is not None:
            search_filter = or_(
                Warehouse.code.icontains(query, autoescape=True),
                Warehouse.name.icontains(query, autoescape=True),
            )
            item_query = item_query.where(search_filter)
            count_query = count_query.where(search_filter)

        with self._session_factory() as session, session.begin():
            total = session.scalar(count_query) or 0
            items = list(
                session.scalars(
                    item_query.order_by(Warehouse.code, Warehouse.id).limit(limit).offset(offset)
                )
            )
        return WarehousePage(items=items, total=total, limit=limit, offset=offset)

    def get_warehouse(self, warehouse_id: int) -> Warehouse:
        with self._session_factory() as session, session.begin():
            warehouse = session.get(Warehouse, warehouse_id)
            if warehouse is None:
                raise WarehouseNotFoundError
        return warehouse

    def list_products(
        self,
        *,
        sku: str | None,
        query: str | None,
        limit: int,
        offset: int,
    ) -> ProductPage:
        item_query = select(Product)
        count_query = select(func.count()).select_from(Product)
        if sku is not None:
            item_query = item_query.where(Product.sku == sku)
            count_query = count_query.where(Product.sku == sku)
        if query is not None:
            search_filter = or_(
                Product.sku.icontains(query, autoescape=True),
                Product.name.icontains(query, autoescape=True),
            )
            item_query = item_query.where(search_filter)
            count_query = count_query.where(search_filter)

        with self._session_factory() as session, session.begin():
            total = session.scalar(count_query) or 0
            items = list(
                session.scalars(
                    item_query.order_by(Product.sku, Product.id).limit(limit).offset(offset)
                )
            )
        return ProductPage(items=items, total=total, limit=limit, offset=offset)

    def get_product(self, product_id: int) -> Product:
        with self._session_factory() as session, session.begin():
            product = session.get(Product, product_id)
            if product is None:
                raise ProductNotFoundError
        return product

    def list_stocks(
        self,
        *,
        warehouse_id: int | None,
        warehouse_code: str | None,
        product_id: int | None,
        sku: str | None,
        include_zero: bool,
        limit: int,
        offset: int,
    ) -> StockPage:
        item_query = select(
            Warehouse.id.label("warehouse_id"),
            Warehouse.code.label("warehouse_code"),
            Warehouse.name.label("warehouse_name"),
            Product.id.label("product_id"),
            Product.sku,
            Product.name.label("product_name"),
            StockBalance.quantity,
            StockBalance.created_at,
            StockBalance.updated_at,
        ).select_from(StockBalance)
        count_query = select(func.count()).select_from(StockBalance)

        item_query = item_query.join(Warehouse, Warehouse.id == StockBalance.warehouse_id).join(
            Product, Product.id == StockBalance.product_id
        )
        count_query = count_query.join(Warehouse, Warehouse.id == StockBalance.warehouse_id).join(
            Product, Product.id == StockBalance.product_id
        )

        filters = []
        if warehouse_id is not None:
            filters.append(StockBalance.warehouse_id == warehouse_id)
        if warehouse_code is not None:
            filters.append(Warehouse.code == warehouse_code)
        if product_id is not None:
            filters.append(StockBalance.product_id == product_id)
        if sku is not None:
            filters.append(Product.sku == sku)
        if not include_zero:
            filters.append(StockBalance.quantity != 0)
        if filters:
            item_query = item_query.where(*filters)
            count_query = count_query.where(*filters)

        with self._session_factory() as session, session.begin():
            total = session.scalar(count_query) or 0
            rows = session.execute(
                item_query.order_by(
                    Warehouse.code,
                    Product.sku,
                    Warehouse.id,
                    Product.id,
                )
                .limit(limit)
                .offset(offset)
            )
            items = [StockReadModel(**row._mapping) for row in rows]
        return StockPage(items=items, total=total, limit=limit, offset=offset)
