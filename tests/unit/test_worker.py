import signal
from threading import Event
from unittest.mock import patch

import pytest

from app.worker import build_shutdown_handler, run_worker_loop

pytestmark = pytest.mark.unit


class RecordingEvent(Event):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(timeout)
        self.set()
        return True


def test_shutdown_handler_only_records_signal_and_sets_stop_event() -> None:
    stop_event = Event()
    received_signals: list[int] = []
    handler = build_shutdown_handler(stop_event, received_signals)

    with patch("app.worker.logger.info") as log_info:
        handler(signal.SIGTERM, None)
        handler(signal.SIGINT, None)

    assert stop_event.is_set()
    assert received_signals == [signal.SIGTERM]
    log_info.assert_not_called()


def test_worker_loop_uses_interruptible_wait_and_starts_no_second_cycle() -> None:
    stop_event = RecordingEvent()
    cycles = 0

    def process_cycle() -> None:
        nonlocal cycles
        cycles += 1

    run_worker_loop(stop_event, process_cycle, poll_interval_seconds=2.5)

    assert cycles == 1
    assert stop_event.wait_calls == [2.5]
    assert stop_event.is_set()
