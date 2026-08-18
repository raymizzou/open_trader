from __future__ import annotations

import threading
import time

import pytest

from open_trader.prediction_predict_snapshot_refresher import (
    REFRESH_INTERVAL_SECONDS,
    PredictAccountSnapshotRefresher,
)


class FakeExecution:
    def __init__(self, *, snapshots: list[object] | None = None) -> None:
        self.refresh_calls = 0
        self._snapshots = snapshots if snapshots is not None else [{"wallet_address": "0xpredict"}]
        self._lock = threading.Lock()

    def _refresh_predict_account_snapshot(self) -> object:
        with self._lock:
            self.refresh_calls += 1
            index = min(self.refresh_calls - 1, len(self._snapshots) - 1)
            item = self._snapshots[index]
        if isinstance(item, Exception):
            raise item
        return item


def test_refresher_rejects_invalid_constructor_arguments() -> None:
    class NoSeam:
        pass

    with pytest.raises(ValueError, match="_refresh_predict_account_snapshot"):
        PredictAccountSnapshotRefresher(execution=NoSeam())
    with pytest.raises(ValueError, match="interval must be positive"):
        PredictAccountSnapshotRefresher(execution=FakeExecution(), interval=0)
    with pytest.raises(ValueError, match="interval must be positive"):
        PredictAccountSnapshotRefresher(execution=FakeExecution(), interval=True)


def test_default_interval_matches_approved_freshness_budget() -> None:
    assert REFRESH_INTERVAL_SECONDS == 30.0


def test_tick_calls_refresh_and_tolerates_none_result() -> None:
    execution = FakeExecution(snapshots=[None, {"wallet_address": "0xpredict"}])
    refresher = PredictAccountSnapshotRefresher(execution=execution)

    refresher._tick()
    refresher._tick()

    assert execution.refresh_calls == 2


def test_start_stop_is_idempotent_and_ticks_in_background() -> None:
    execution = FakeExecution()
    refresher = PredictAccountSnapshotRefresher(execution=execution, interval=0.01)

    refresher.start()
    refresher.start()
    assert refresher._thread is not None
    assert refresher._thread.is_alive()

    deadline = time.monotonic() + 5
    while execution.refresh_calls < 3 and time.monotonic() < deadline:
        time.sleep(0.01)

    refresher.stop()
    refresher.stop()
    assert refresher._thread is None or not refresher._thread.is_alive()
    # The loop ticks first, so the very first refresh starts with the thread.
    assert execution.refresh_calls >= 3


def test_refresh_exception_does_not_kill_the_loop() -> None:
    execution = FakeExecution(
        snapshots=[
            RuntimeError("predict fetch failed"),
            {"wallet_address": "0xpredict"},
        ]
    )
    refresher = PredictAccountSnapshotRefresher(execution=execution, interval=0.01)

    refresher.start()
    deadline = time.monotonic() + 5
    while execution.refresh_calls < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    refresher.stop()

    # First tick raised, later ticks recovered; the thread survived either way.
    assert execution.refresh_calls >= 4
