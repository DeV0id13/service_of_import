from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.config import get_settings
from app.dependencies import get_storage
from app.errors import StorageUnavailableError
from app.services.storage import ObjectStorage

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@router.get("/live", response_model=HealthResponse)
def live() -> HealthResponse:
    """Report that the API process is alive."""

    return HealthResponse()


@router.get("/ready", response_model=HealthResponse)
def ready(storage: Annotated[ObjectStorage, Depends(get_storage)]) -> HealthResponse:
    """Report that the API can reach its configured object bucket."""

    if not storage.is_available(get_settings().s3_bucket):
        raise StorageUnavailableError
    return HealthResponse()
