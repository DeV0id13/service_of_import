from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ReportStatus = Literal["pending", "processing", "completed", "failed"]


class ReportSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: ReportStatus
    original_filename: str
    size_bytes: int
    checksum_sha256: str | None
    created_at: datetime


class ReportDetails(ReportSummary):
    processing_started_at: datetime | None
    finished_at: datetime | None
    row_count: int
    error_count: int
    stocks_created: int
    stocks_updated: int
    stocks_zeroed: int
    failure_kind: str | None
    failure_message: str | None


class ReportListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[ReportSummary]
    limit: int
    offset: int
    total: int


class WarehouseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    created_at: datetime
    updated_at: datetime


class WarehouseListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[WarehouseResponse]
    limit: int
    offset: int
    total: int


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    created_at: datetime
    updated_at: datetime


class ProductListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[ProductResponse]
    limit: int
    offset: int
    total: int


class StockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    warehouse_id: int
    warehouse_code: str
    warehouse_name: str
    product_id: int
    sku: str
    product_name: str
    quantity: int
    created_at: datetime
    updated_at: datetime


class StockListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[StockResponse]
    limit: int
    offset: int
    total: int


class ReportErrorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    line_number: int | None
    field_name: str | None
    code: str
    message: str
    raw_data: dict[str, object] | None
    created_at: datetime


class ReportErrorListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[ReportErrorResponse]
    limit: int
    offset: int
    total: int


class ErrorDetails(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetails
