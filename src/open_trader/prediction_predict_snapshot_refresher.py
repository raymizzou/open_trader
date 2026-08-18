"""Issue #93: background-refresh the predict account snapshot cache.

``PredictAccountSnapshotRefresher`` is a daemon thread owned by
``PredictionRuntime``. Every ``interval`` seconds it calls
``PredictionExecutionService._refresh_predict_account_snapshot()``, which
performs the live on-chain/REST snapshot fetch off the HTTP request threads
and publishes the normalized snapshot to the in-memory read cache.

The HTTP read path (``/api/prediction-arbitrage/state``) only reads the cache
through ``_fresh_predict_account_snapshot()`` (no network I/O, <=60s age
gate); execution/trading paths keep calling the live
``_live_predict_account_snapshot()`` directly.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 30.0


class PredictAccountSnapshotRefresher:
    """Periodically refresh the execution service's predict snapshot cache."""

    def __init__(
        self,
        *,
        execution: object,
        interval: float = REFRESH_INTERVAL_SECONDS,
    ) -> None:
        if not callable(getattr(execution, "_refresh_predict_account_snapshot", None)):
            raise ValueError("execution must provide _refresh_predict_account_snapshot")
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or interval <= 0
        ):
            raise ValueError("interval must be positive")
        self._execution = execution
        self._interval = float(interval)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="prediction-predict-snapshot-refresher",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            # A refresh already in progress is synchronous and bounded by the
            # predict REST/RPC timeouts; let it finish.
            if thread is not None and not thread.is_alive():
                self._thread = None

    def _loop(self) -> None:
        # Tick first so the cache warms up right after process start instead
        # of leaving the predict venue unavailable for a full interval.
        while True:
            try:
                self._tick()
            except Exception:
                logger.exception("predict_account_snapshot refresh tick failed")
            if self._stop_event.wait(self._interval):
                return

    def _tick(self) -> None:
        refresh = self._execution._refresh_predict_account_snapshot  # type: ignore[attr-defined]
        snapshot = refresh()
        if snapshot is None:
            logger.warning(
                "predict_account_snapshot refresh returned no valid snapshot; "
                "state read path will serve no predict venue until a refresh succeeds"
            )
            return
        checked_at = snapshot.get("checked_at")
        age = (
            (datetime.now(UTC) - checked_at).total_seconds()
            if isinstance(checked_at, datetime)
            else None
        )
        logger.info(
            "predict_account_snapshot refreshed age=%s",
            f"{age:.1f}s" if age is not None else "unknown",
        )
