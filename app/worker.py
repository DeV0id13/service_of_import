import logging
import signal
from collections.abc import Callable
from threading import Event
from types import FrameType

from app.config import get_settings
from app.db import SessionFactory, engine
from app.dependencies import get_storage
from app.logging import configure_logging
from app.services.report_processing import process_next_report

logger = logging.getLogger(__name__)


def build_shutdown_handler(
    stop_event: Event,
    received_signals: list[int],
) -> Callable[[int, FrameType | None], None]:
    def request_shutdown(signal_number: int, _: FrameType | None) -> None:
        if not received_signals:
            received_signals.append(signal_number)
        stop_event.set()

    return request_shutdown


def run_worker_loop(
    stop_event: Event,
    process_cycle: Callable[[], object],
    poll_interval_seconds: float,
) -> None:
    while not stop_event.is_set():
        try:
            process_cycle()
        except Exception:
            logger.exception(
                "Worker cycle failed",
                extra={"event": "worker_cycle_failed", "stage": "worker"},
            )
        stop_event.wait(poll_interval_seconds)


def main() -> None:
    """Poll PostgreSQL while advisory locking serializes CSV validation."""

    settings = get_settings()
    configure_logging(settings.log_level)
    stop_event = Event()
    received_signals: list[int] = []
    shutdown_handler = build_shutdown_handler(stop_event, received_signals)
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    logger.info(
        "Worker process started",
        extra={"event": "worker_started", "stage": "worker"},
    )

    storage = get_storage()

    def process_cycle() -> object:
        return process_next_report(
            engine,
            storage,
            SessionFactory,
            advisory_lock_key=settings.worker_advisory_lock_key,
            batch_size=settings.validation_batch_size,
            csv_max_field_chars=settings.csv_max_field_chars,
            csv_max_record_chars=settings.csv_max_record_chars,
            csv_error_raw_value_chars=settings.csv_error_raw_value_chars,
            csv_error_raw_total_chars=settings.csv_error_raw_total_chars,
        )

    try:
        run_worker_loop(stop_event, process_cycle, settings.worker_poll_interval_seconds)
    finally:
        engine.dispose()
        if received_signals:
            logger.info(
                "Worker shutdown requested",
                extra={
                    "event": "worker_shutdown_requested",
                    "stage": "worker",
                    "signal": signal.Signals(received_signals[0]).name,
                },
            )
        logger.info(
            "Worker process stopped",
            extra={"event": "worker_stopped", "stage": "worker"},
        )


if __name__ == "__main__":
    main()
