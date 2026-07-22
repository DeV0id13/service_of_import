import logging
import time

from app.config import get_settings
from app.logging import configure_logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Run an idle worker process until report processing is implemented."""

    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("Worker scaffold started", extra={"event": "worker_started"})

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Worker scaffold stopped", extra={"event": "worker_stopped"})


if __name__ == "__main__":
    main()
