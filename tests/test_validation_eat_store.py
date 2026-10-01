from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


def test_validation_mode_defaults_to_observe_only_and_persists(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    assert store.get_validation_mode() == "observe_only"
    assert store.set_validation_mode("manual") == "manual"
    assert PredictionArbitrageStore(tmp_path / "data").get_validation_mode() == "manual"
    try:
        store.set_validation_mode("nope")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid mode must raise")


def test_auto_eat_attempts_and_stats(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 9, 22, 15, 59, 59, tzinfo=UTC)
    monkeypatch.setattr("open_trader.prediction_arbitrage_store._utc_now", lambda: now.isoformat())
    store = PredictionArbitrageStore(tmp_path / "data")
    store.record_auto_eat_attempt(
        signal_id="s1", market_id="m1", decision="submitted",
        total_cost=Decimal("5.00"),
    )
    store.record_auto_eat_attempt(
        signal_id="s2", market_id="m1", decision="rejected", reason="cooldown"
    )
    stats = store.auto_eat_stats(now=now)
    assert stats["today_submitted"] == 1
    assert stats["today_cost"] == 5.0
    assert stats["realized_pnl"] == 0.0
    assert stats["rejected_by_reason"] == {"cooldown": 1}
    assert store.auto_eat_attempt_exists("s1", "submitted") is True
    assert store.last_submitted_auto_eat("m1") is not None

    tomorrow = store.auto_eat_stats(now=now + timedelta(seconds=1))
    assert tomorrow["today_submitted"] == 0
    assert tomorrow["today_cost"] == 0.0
    assert tomorrow["rejected_by_reason"] == {}
