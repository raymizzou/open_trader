"""Schedule the single LP admission path; durable intent belongs to that path."""

from datetime import UTC, datetime, timedelta
from collections.abc import Mapping
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
        self._api_retry_interval_seconds = 60
        self._retry_not_before: datetime | None = None
        try:
            self._restore_deadline()
        except Exception as exc:
            self._state_read_failed(exc)
        register = getattr(execution, "set_lp_auto_wakeup", None)
        if callable(register):
            register(self.request_check)

    def _core_state(self):
        read = getattr(self.execution, 'lp_auto_state', None)
        core = read(include_intents=False) if callable(read) else {}
        if not isinstance(core, Mapping):
            raise TypeError('lp_auto_state_shape')
        interval = core.get('api_retry_interval_seconds')
        if type(interval) is int and interval > 0:
            self._api_retry_interval_seconds = interval
        return core

    def _state_read_failed(self, exc, *, event='prediction_lp_auto_state_failed'):
        with self._state:
            self._checking = False
            self._error = type(exc).__name__
            self._retry_not_before = self.clock() + timedelta(seconds=self._api_retry_interval_seconds)
            self._next_check_at = self._retry_not_before
            self._wake.clear()
        logger.exception(event)

    def _restore_deadline(self, *, core=None):
        waiting = (self._core_state() if core is None else core).get('plan_wait')
        if waiting and waiting.get('kind') == 'dependencies':
            deadline = datetime.fromisoformat(waiting['deadline'])
            self._next_check_at = deadline
        elif waiting and waiting.get('kind') == 'events':
            self._next_check_at = None  # no runnable work until a real wakeup

    def snapshot(self) -> dict[str, object]:
        with self._state:
            return {
                "last_check_at": self._last_check_at.isoformat() if self._last_check_at else None,
                "next_check_at": None if self._checking or self._next_check_at is None else (
                    self._next_check_at
                ).isoformat(),
                "check_in_progress": self._checking,
                "last_check_error": self._error,
                "scheduler_running": self._thread is not None and self._thread.is_alive(),
                "check_interval_seconds": None,
                "cycle_mode": "continuous",
            }

    def request_check(self) -> None:
        with self._state:
            now = self.clock()
            self._next_check_at = max(now, self._retry_not_before or now)
            if self._next_check_at <= now:
                self._wake.set()

    def run_due(self) -> bool:
        if not self._cycle.acquire(blocking=False):
            return False
        try:
            with self._state:
                if self._stop.is_set() or self._next_check_at is None or self.clock() < self._next_check_at:
                    return False
                self._wake.clear()
                self._checking = True
                self._last_check_at = self.clock()
            try:
                # Unknown state cannot authorize another execution cycle.
                self._core_state()
            except Exception as exc:
                self._state_read_failed(exc)
                return True
            with self._state:
                self._retry_not_before = None
            try:
                # This always reconciles, including while manually paused.
                # Only the core may decide whether a new BUY can be sent.
                check = getattr(self.execution, "lp_auto_scheduled_check", self.execution.lp_auto_run_once)
                check()
            except Exception as exc:
                with self._state:
                    self._error = type(exc).__name__
                logger.exception("prediction_lp_auto_check_failed")
            else:
                with self._state:
                    self._error = None
            finally:
                try:
                    core = self._core_state()
                    with self._state:
                        self._checking = False
                        now = self.clock()
                        self._next_check_at = (now + timedelta(seconds=self._api_retry_interval_seconds)
                            if self._error is not None else now)
                        # Read waits do not own the configured check-error retry.
                        if self._error is None:
                            self._restore_deadline(core=core)
                        if self._wake.is_set():
                            self._next_check_at = now
                except Exception as exc:
                    self._state_read_failed(exc)
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
                try:
                    self.run_due()
                except Exception as exc:
                    self._state_read_failed(exc, event='prediction_lp_auto_monitor_failed')
                with self._state:
                    delay = (None if self._next_check_at is None else
                        max(0, (self._next_check_at - self.clock()).total_seconds()))
                if self._stop.is_set():
                    break
                self._wake.wait(None if delay is None else min(delay, 60))

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
