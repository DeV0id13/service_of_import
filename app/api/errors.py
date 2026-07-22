import logging

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import ServiceError

logger = logging.getLogger(__name__)


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


async def service_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ServiceError):
        return error_response(500, "internal_error", "An internal error occurred")
    return error_response(exc.status_code, exc.code, exc.public_message)


async def validation_error_handler(_: Request, __: Exception) -> JSONResponse:
    return error_response(422, "validation_error", "Request validation failed")


async def http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        return error_response(500, "internal_error", "An internal error occurred")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return error_response(exc.status_code, "http_error", message)


async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled API error", exc_info=exc, extra={"event": "api_error"})
    return error_response(500, "internal_error", "An internal error occurred")


ERROR_HANDLERS = {
    ServiceError: service_error_handler,
    RequestValidationError: validation_error_handler,
    HTTPException: http_error_handler,
    Exception: unexpected_error_handler,
}
