from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionFactory
from app.services.read_api import ReadApiService
from app.services.reports import ReportService
from app.services.storage import ObjectStorage, S3ObjectStorage


@lru_cache
def get_storage() -> ObjectStorage:
    return S3ObjectStorage(get_settings())


def get_session_factory() -> Callable[[], Session]:
    return SessionFactory


def get_report_service(
    storage: Annotated[ObjectStorage, Depends(get_storage)],
    session_factory: Annotated[Callable[[], Session], Depends(get_session_factory)],
) -> ReportService:
    settings = get_settings()
    return ReportService(
        storage=storage,
        session_factory=session_factory,
        bucket=settings.s3_bucket,
    )


def get_read_api_service(
    session_factory: Annotated[Callable[[], Session], Depends(get_session_factory)],
) -> ReadApiService:
    return ReadApiService(session_factory)
