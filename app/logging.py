import json
import logging
import sys
from datetime import UTC, datetime
from typing import Final

_CONTEXT_FIELDS: Final = (
    "event",
    "report_id",
    "stage",
    "status",
    "size_bytes",
    "object_key",
    "row_count",
    "error_count",
    "batch_kind",
    "batch_size",
    "lock_released",
)


class JsonFormatter(logging.Formatter):
    """Small JSON formatter for machine-readable container logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level_name: str) -> None:
    """Configure application logging once at process startup."""

    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        level = logging.INFO

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)
