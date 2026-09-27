"""Schedule the single LP admission path; durable intent belongs to that path."""

from datetime import UTC, datetime, timedelta
import logging
import threading
from typing import Callable


logger = logging.getLogger(__name__)


class LPAutoScheduler:
    def __init__(self, execution: object, *, clock: Callable[[], datetime] | None = None):
        self.execution = execution
        self.clock = clock or (lambda: datetime.now(UTC))
        self._cycle = threading.Lock()
        self._state = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_check_at: datetime | None = None
        self._next_check_at = self.clock()
        self._checking = False
        self._error: str | None = None

    def snapshot(self) -> dict[str, object]:
        with self._state:
            return {
                "last_check_at": self._last_check_at.isoformat() if self._last_check_at else None,
                "next_check_at": None if self._checking else (
                    self.clock() if self._wake.is_set() else self._next_check_at
                ).isoformat(),
                "check_in_progress": self._checking,
                "last_check_error": self._error,
                "scheduler_running": self._thread is not None and self._thread.is_alive(),
                "check_interval_seconds": 60,
            }

    def request_check(self) -> None:
        self._wake.set()

    def run_due(self) -> bool:
        if not self._cycle.acquire(blocking=False):
            return False
        try:
            with self._state:
                if self._stop.is_set() or (
                    not self._wake.is_set() and self.clock() < self._next_check_at
                ):
                    return False
                self._wake.clear()
                self._checking = True
                self._last_check_at = self.clock()
            try:
                # This always reconciles, including while manually paused.
                # Only the core may decide whether a new BUY can be sent.
                self.execution.lp_auto_run_once()
            except Exception as exc:
                with self._state:
                    self._error = type(exc).__name__
                logger.exception("prediction_lp_auto_check_failed")
            else:
                with self._state:
                    self._error = None
            finally:
                with self._state:
                    self._checking = False
                    self._next_check_at = self.clock() + timedelta(seconds=60)
            return True
        finally:
            self._cycle.release()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self.request_check()

        def run() -> None:
            while not self._stop.is_set():
                self.run_due()
                with self._state:
                    delay = max(0, (self._next_check_at - self.clock()).total_seconds())
                self._wake.wait(min(delay, 60))

        self._thread = threading.Thread(target=run, name="prediction-lp-auto", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # Stopping the service never changes the operator's durable intent.
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("prediction LP auto monitor thread did not stop")
            self._thread = None
