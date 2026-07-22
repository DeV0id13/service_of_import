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
    checksum_sha256: str
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


class ErrorDetails(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetails
