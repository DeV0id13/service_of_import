import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import ERROR_HANDLERS
from app.api.health import router as health_router
from app.api.inventory import router as inventory_router
from app.api.reports import router as reports_router
from app.config import get_settings
from app.logging import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("API process started", extra={"event": "api_started"})
    yield
    logger.info("API process stopped", extra={"event": "api_stopped"})


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    for exception_type, handler in ERROR_HANDLERS.items():
        application.add_exception_handler(exception_type, handler)
    application.include_router(health_router)
    application.include_router(reports_router)
    application.include_router(inventory_router)
    return application


app = create_app()
