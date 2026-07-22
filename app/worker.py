import logging
import time

from app.config import get_settings
from app.db import SessionFactory, engine
from app.dependencies import get_storage
from app.logging import configure_logging
from app.services.report_processing import process_next_report

logger = logging.getLogger(__name__)


def main() -> None:
    """Poll PostgreSQL while advisory locking serializes CSV validation."""

    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "Worker process started",
        extra={"event": "worker_started", "stage": "worker"},
    )

    try:
        while True:
            try:
                process_next_report(
                    engine,
                    get_storage(),
                    SessionFactory,
                    advisory_lock_key=settings.worker_advisory_lock_key,
                    batch_size=settings.validation_batch_size,
                )
            except Exception:
                logger.exception(
                    "Worker cycle failed",
                    extra={"event": "worker_cycle_failed", "stage": "worker"},
                )
            time.sleep(settings.worker_poll_interval_seconds)
    except KeyboardInterrupt:
        logger.info(
            "Worker process stopped",
            extra={"event": "worker_stopped", "stage": "worker"},
        )


if __name__ == "__main__":
    main()
