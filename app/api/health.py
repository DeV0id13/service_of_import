from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.dependencies import get_readiness_engine, get_storage
from app.errors import DependencyUnavailableError
from app.services.storage import ObjectStorage

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@router.get("/live", response_model=HealthResponse)
def live() -> HealthResponse:
    """Report that the API process is alive."""

    return HealthResponse()


@router.get("/ready", response_model=HealthResponse)
def ready(
    storage: Annotated[ObjectStorage, Depends(get_storage)],
    database_engine: Annotated[Engine, Depends(get_readiness_engine)],
) -> HealthResponse:
    """Report that the API can reach PostgreSQL and its configured object bucket."""

    try:
        with database_engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
    except SQLAlchemyError as exc:
        raise DependencyUnavailableError from exc
    if not storage.is_available(get_settings().s3_bucket):
        raise DependencyUnavailableError
    return HealthResponse()
