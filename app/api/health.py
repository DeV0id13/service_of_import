from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@router.get("/live", response_model=HealthResponse)
def live() -> HealthResponse:
    """Report that the API process is alive."""

    return HealthResponse()


@router.get("/ready", response_model=HealthResponse)
def ready() -> HealthResponse:
    """Report that the scaffold process started with valid settings."""

    return HealthResponse()
