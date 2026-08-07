from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.validation_eat_policy import (
    DAILY_COST_LIMIT,
    MIN_BALANCE_FLOOR,
    should_eat,
)


def _signal(market_id: str = "m1", signal_id: str = "s1") -> dict[str, object]:
    return {"signal_id": signal_id, "market_id": market_id}


def _intent(cost: Decimal = Decimal("5.00")) -> SimpleNamespace:
    return SimpleNamespace(total_max_cost=cost)


def test_should_eat_allows_first_order_in_auto_mode(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    store.set_validation_mode("auto")
    allowed, reason = should_eat(
        store=store, signal=_signal(), intent=_intent(),
        balance=Decimal("60.00"), now=datetime.now(UTC),
    )
    assert allowed is True
    assert reason == ""


def test_should_eat_rejects_when_mode_not_auto(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    allowed, reason = should_eat(
        store=store, signal=_signal(), intent=_intent(),
        balance=Decimal("60.00"), now=datetime.now(UTC),
    )
    assert (allowed, reason) == (False, "mode_not_auto")


def test_should_eat_rejects_duplicate_episode_and_cooldown(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    store.set_validation_mode("auto")
    now = datetime.now(UTC)
    store.record_auto_eat_attempt(
        signal_id="s1", market_id="m1", decision="submitted",
        total_cost=Decimal("5.00"),
    )
    assert should_eat(
        store=store, signal=_signal(), intent=_intent(),
        balance=Decimal("60.00"), now=now,
    ) == (False, "episode_duplicate")
    store.record_auto_eat_attempt(
        signal_id="s9", market_id="m1", decision="submitted",
        total_cost=Decimal("5.00"),
    )
    assert should_eat(
        store=store, signal=_signal(signal_id="s2"), intent=_intent(),
        balance=Decimal("60.00"), now=now,
    ) == (False, "cooldown")


def test_should_eat_rejects_balance_and_daily_caps(tmp_path: Path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    store.set_validation_mode("auto")
    now = datetime.now(UTC)
    assert should_eat(
        store=store, signal=_signal(), intent=_intent(),
        balance=MIN_BALANCE_FLOOR - Decimal("1"), now=now,
    ) == (False, "insufficient_balance")
    store.record_auto_eat_attempt(
        signal_id="x1", market_id="x1", decision="submitted",
        total_cost=DAILY_COST_LIMIT,
    )
    assert should_eat(
        store=store, signal=_signal(), intent=_intent(),
        balance=Decimal("60.00"), now=now,
    ) == (False, "daily_cost_cap")
