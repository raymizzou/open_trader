from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from open_trader.decision_plan_watch import (
    evaluate_plan_snapshot,
    run_decision_plan_watch,
)
from open_trader.futu_watch import QuoteSnapshot
from open_trader.plan_events import load_plan_events


def at(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-07-13T{value}:00-04:00")


def price_plan() -> dict[str, object]:
    return {
        "plan_id": "US.DRAM:2026-07-13:v1",
        "run_date": "2026-07-13",
        "market": "US",
        "symbol": "DRAM",
        "mode": "validated_plan",
        "current_quantity": "400",
        "expires_at": "2026-07-13T16:00:00-04:00",
        "conditions": [{
            "condition_id": "trim-at-resistance",
            "priority": "ordinary",
            "operator": ">=",
            "calculated_value": "65",
            "suggested_action": "减仓",
            "target_quantity": "300",
            "target_weight": "0.06",
            "formula": "SMA20 + 2 * ATR14",
            "inputs": {"sma20": "61", "atr14": "2"},
            "source_date": "2026-07-10",
        }],
    }


def test_same_condition_can_trigger_reset_and_trigger_again() -> None:
    truth: dict[str, bool] = {}

    first, truth = evaluate_plan_snapshot(plan=price_plan(), previous_truth=truth, last_price=Decimal("66"), as_of=at("10:00"))
    held, truth = evaluate_plan_snapshot(plan=price_plan(), previous_truth=truth, last_price=Decimal("67"), as_of=at("10:01"))
    reset, truth = evaluate_plan_snapshot(plan=price_plan(), previous_truth=truth, last_price=Decimal("64"), as_of=at("10:02"))
    second, truth = evaluate_plan_snapshot(plan=price_plan(), previous_truth=truth, last_price=Decimal("66"), as_of=at("10:03"))

    assert [event.event_type for event in first] == ["condition_triggered"]
    assert held == []
    assert [event.event_type for event in reset] == ["condition_reset"]
    assert [event.event_type for event in second] == ["condition_triggered"]


class QuoteClient:
    def __init__(self, price: str) -> None:
        self.price = Decimal(price)
        self.closed = False

    def get_snapshots(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        return {symbol: QuoteSnapshot(symbol, self.price) for symbol in symbols}

    def close(self) -> None:
        self.closed = True


class FailingNotifier:
    def notify(self, title: str, message: str) -> None:
        assert "US.DRAM" in title
        assert "目标总仓位：300" in message
        assert "decision_tab=final" in message
        raise RuntimeError("offline")


def test_runner_records_notification_failure_without_losing_trigger(tmp_path: Path) -> None:
    client = QuoteClient("66")
    events_path = tmp_path / "plan_events.jsonl"

    result = run_decision_plan_watch(
        plans=[price_plan()], events_path=events_path, quote_client=client,
        notifier=FailingNotifier(), poll_seconds=1, once=True,
        now_fn=lambda: at("10:00"), sleep_fn=lambda _: None,
    )

    events = load_plan_events(events_path)
    assert [event.event_type for event in events] == [
        "condition_triggered", "notification_failed",
    ]
    assert result.trigger_count == 1
    assert result.notification_failed_count == 1
    assert client.closed is True


def test_fallback_plan_never_enters_quote_watcher(tmp_path: Path) -> None:
    client = QuoteClient("66")
    plan = {**price_plan(), "mode": "fallback_advice", "conditions": []}

    result = run_decision_plan_watch(
        plans=[plan], events_path=tmp_path / "events.jsonl", quote_client=client,
        notifier=FailingNotifier(), poll_seconds=1, once=True,
        now_fn=lambda: at("10:00"), sleep_fn=lambda _: None,
    )

    assert result.watched_plan_count == 0
    assert result.trigger_count == 0


def test_expired_plan_appends_one_expiry_event_across_restarts(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    plan = {**price_plan(), "expires_at": "2026-07-13T09:59:00-04:00"}

    for _ in range(2):
        run_decision_plan_watch(
            plans=[plan], events_path=events_path, quote_client=QuoteClient("66"),
            notifier=FailingNotifier(), poll_seconds=1, once=True,
            now_fn=lambda: at("10:00"), sleep_fn=lambda _: None,
        )

    assert [event.event_type for event in load_plan_events(events_path)] == [
        "plan_expired",
    ]
