from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import threading
import time

import pytest
from polymarket.models.clob.account import ClobTrade, MakerOrder, OpenOrder
from polymarket.models.clob.order_book import OrderBook, OrderBookLevel
from polymarket.models.gamma.market import (
    FeeSchedule,
    Market,
    MarketMetrics,
    MarketOutcome,
    MarketOutcomes,
    MarketPrices,
    MarketResolution,
    MarketRewards,
    MarketSportsMetadata,
    MarketState,
    MarketTrading,
)

from open_trader import polymarket_lp
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from open_trader.prediction_arbitrage_store import (
    LP_RESERVED_MANUAL_SESSION_ID,
    PredictionArbitrageStore,
)


class _Exchange:
    def __init__(self) -> None:
        self.snapshot_value: dict[str, object] | None = None
        self.snapshots: list[dict[str, object]] = []
        self.snapshot_calls = 0
        self.limit_orders: list[dict[str, object]] = []
        self.posts: list[dict[str, object]] = []
        self.cancels: list[str] = []
        self.cancel_responses: list[object] = []
        self.protected_sells: list[dict[str, object]] = []
        self.protected_responses: list[object] = []
        self.post_failures: list[BaseException] = []
        self.scoring_responses: list[object] = []
        self.scoring_calls: list[str] = []
        self.reject_entry = False

    def lp_snapshot(self, request: dict[str, object]) -> dict[str, object]:
        del request
        self.snapshot_calls += 1
        if self.snapshots:
            return self.snapshots[min(self.snapshot_calls - 1, len(self.snapshots) - 1)]
        if self.snapshot_value is None:
            raise RuntimeError("snapshot unavailable")
        return self.snapshot_value

    def create_limit_order(self, **kwargs: object) -> dict[str, object]:
        self.limit_orders.append(dict(kwargs))
        return {**kwargs, "order_type": "GTD" if kwargs.get("expiration") else "GTC"}

    def post_order(self, signed: dict[str, object]) -> dict[str, object]:
        if self.reject_entry:
            response = {"order_id": "", "status": "REJECTED", **signed}
            self.posts.append(response)
            return response
        response = {
            "order_id": f"order-{len(self.posts) + 1}",
            "status": "LIVE",
            **signed,
        }
        self.posts.append(response)
        if self.post_failures:
            raise self.post_failures.pop(0)
        return response

    def cancel_order(self, order_id: str) -> object:
        self.cancels.append(order_id)
        if self.cancel_responses:
            return self.cancel_responses.pop(0)
        return {"canceled": [order_id], "status": "CANCELED"}

    def cancel_orders(self, order_ids: tuple[str, ...]) -> object:
        self.cancels.extend(order_ids)
        if self.cancel_responses:
            return self.cancel_responses.pop(0)
        return tuple(order_ids)

    def submit_protected_sell(self, **kwargs: object) -> dict[str, object]:
        self.protected_sells.append(dict(kwargs))
        if self.protected_responses:
            response = self.protected_responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response  # type: ignore[return-value]
        return {"order_id": f"stop-{len(self.protected_sells)}", "status": "FILLED"}

    def get_order_scoring(self, order_id: str) -> bool:
        self.scoring_calls.append(order_id)
        if self.scoring_responses:
            response = self.scoring_responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response is True
        return bool(self.snapshot_value and self.snapshot_value.get("scoring") is True)


def _snapshot(now: datetime) -> dict[str, object]:
    return {
        "account": {
            "authenticated": True,
            "balance": Decimal("100"),
            "allowance": Decimal("100"),
            "positions": [],
            "open_orders": [],
        },
        "market": {
            "market_id": "market-1",
            "condition_id": "0x" + "c" * 64,
            "token_id": "0x" + "1" * 64,
            "outcome": "YES",
            "accepting_orders": True,
            "exchange_type": "CLOB",
            "tick_size": Decimal("0.01"),
            "minimum_order_size": Decimal("1"),
            "fee": Decimal("0"),
            "taker_fee_rate": Decimal("0"),
            "reward_min_size": Decimal("1"),
            "reward_max_spread": Decimal("0.10"),
        },
        "book": {
            "timestamp": now,
            "received_at": now,
            "asks": [{"price": Decimal("0.31"), "size": Decimal("100")}],
            "bids": [{"price": Decimal("0.29"), "size": Decimal("100")}],
        },
        "trades": [],
        "orders": [],
        "orders_terminal": True,
    }


def _request(now: datetime) -> dict[str, object]:
    return {
        "market_id": "market-1",
        "condition_id": "0x" + "c" * 64,
        "token_id": "0x" + "1" * 64,
        "outcome": "YES",
        "question": "Will it happen?",
        "price": Decimal("0.30"),
        "quantity": Decimal("10"),
        "review_at": now + timedelta(minutes=10),
    }


def test_reward_threshold_uses_current_unrounded_daily_amount(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    amounts = iter(("0.99999", "1.00000", "1.10", "0.97"))

    class RewardExchange:
        def lp_reward_snapshot(self, reward_date: str, condition_id: str) -> dict[str, object]:
            assert reward_date == "2026-09-14"
            assert condition_id == "condition-1"
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "account_amount": Decimal(next(amounts)),
                "market_amount": Decimal("0.62"),
            }

    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        "reward-threshold-session",
        "reward-threshold-idempotency",
        state="entry_open",
        payload={
            "condition_id": "condition-1",
            "reward_date": "2026-09-14",
            "paid_rewards": Decimal("0"),
            "trade_pnl": Decimal("0"),
        },
    )
    service = PolymarketLPService(
        store, RewardExchange(), clock=lambda: now
    )

    expectations = (
        ("below", Decimal("0.00001")),
        ("met", Decimal("0")),
        ("met", Decimal("0")),
        ("below", Decimal("0.03")),
    )
    for expected_status, expected_gap in expectations:
        result = service.refresh_rewards()
        observation = result["reward_observation"]
        assert observation["status"] == expected_status
        assert observation["gap"] == expected_gap
        assert result["paid_rewards"] == Decimal("0")
        assert result["trade_pnl"] == Decimal("0")


def test_reward_error_and_staleness_preserve_unknown_and_trade_state(tmp_path) -> None:
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    current = [base]

    class RewardExchange:
        def __init__(self) -> None:
            self.fail = False

        def lp_reward_snapshot(self, reward_date: str, condition_id: str) -> dict[str, object]:
            assert reward_date == "2026-09-14"
            assert condition_id == "condition-1"
            if self.fail:
                raise TimeoutError("rewards_timeout")
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "account_amount": Decimal("0.80"),
                "market_amount": Decimal("0.62"),
            }

    exchange = RewardExchange()
    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        "reward-unknown-session",
        "reward-unknown-idempotency",
        state="stop_loss_exit",
        payload={
            "condition_id": "condition-1",
            "reward_date": "2026-09-14",
            "entry_order_id": "entry-1",
            "buy_filled_quantity": Decimal("10"),
            "residual_quantity": Decimal("10"),
            "stop_loss_latched": True,
            "owned_order_ids": ["entry-1", "exit-1"],
            "order_history": {"entry-1": {"status": "FILLED"}},
            "trade_pnl": Decimal("-1.25"),
            "paid_rewards": Decimal("0"),
        },
    )
    service = PolymarketLPService(
        store, exchange, clock=lambda: current[0]
    )

    known = service.refresh_rewards()
    assert known["reward_observation"]["status"] == "below"
    assert known["reward_observation"]["account_amount"] == Decimal("0.80")

    current[0] = base + timedelta(seconds=181)
    stale = service.status()
    assert stale["reward_observation"]["status"] == "unknown"
    assert stale["reward_observation"]["account_amount"] == Decimal("0.80")
    assert stale["reward_observation"]["checked_at"] == known["reward_observation"]["checked_at"]

    exchange.fail = True
    current[0] = base + timedelta(seconds=182)
    failed = service.refresh_rewards()
    observation = failed["reward_observation"]
    assert observation["status"] == "unknown"
    assert observation["account_amount"] == Decimal("0.80")
    assert observation["checked_at"] == known["reward_observation"]["checked_at"]
    assert observation["error"] == "TimeoutError"
    assert failed["entry_order_id"] == "entry-1"
    assert failed["buy_filled_quantity"] == Decimal("10")
    assert failed["residual_quantity"] == Decimal("10")
    assert failed["stop_loss_latched"] is True
    assert failed["trade_pnl"] == Decimal("-1.25")
    assert failed["paid_rewards"] == Decimal("0")
    assert failed["owned_order_ids"] == ["entry-1", "exit-1"]


def test_reward_day_remains_bound_through_review_and_restart(tmp_path) -> None:
    start = datetime(2026, 9, 14, 23, 59, tzinfo=UTC)
    current = [start]

    class RewardExchange:
        def __init__(self) -> None:
            self.requested_dates: list[str] = []

        def lp_reward_snapshot(self, reward_date: str, condition_id: str) -> dict[str, object]:
            assert condition_id == "condition-1"
            self.requested_dates.append(reward_date)
            return {
                "state": "known",
                "reward_date": reward_date,
                "condition_id": condition_id,
                "account_amount": Decimal("0.80")
                if reward_date == "2026-09-14"
                else Decimal("0.40"),
                "market_amount": Decimal("0.80")
                if reward_date == "2026-09-14"
                else Decimal("0.40"),
            }

    exchange = RewardExchange()
    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        "reward-bound-session",
        "reward-bound-idempotency",
        state="entry_open",
        payload={
            "condition_id": "condition-1",
            "reward_date": "2026-09-14",
            "review_at": "2026-09-15T01:00:00Z",
            "entry_order_id": "entry-1",
            "owned_order_ids": ["entry-1"],
            "trade_pnl": Decimal("0.10"),
            "paid_rewards": Decimal("0"),
        },
    )
    first = PolymarketLPService(store, exchange, clock=lambda: current[0])
    initial = first.refresh_rewards()
    assert initial["reward_observation"]["reward_date"] == "2026-09-14"
    assert initial["reward_observation"]["account_amount"] == Decimal("0.80")

    # Early completion is a review boundary; retaining the observation must
    # not create a new day or reopen the order lifecycle.
    store.lp_update_session(
        "reward-bound-session",
        state="complete",
        patch={"review_status": "closed"},
    )
    current[0] = start + timedelta(seconds=181)
    restarted = PolymarketLPService(store, exchange, clock=lambda: current[0])
    recovered = restarted.status()
    assert recovered["state"] == "complete"
    assert recovered["reward_observation"]["status"] == "unknown"
    assert recovered["reward_observation"]["reward_date"] == "2026-09-14"
    assert recovered["reward_observation"]["account_amount"] == Decimal("0.80")
    assert recovered["trade_pnl"] == Decimal("0.10")
    assert recovered["owned_order_ids"] == ["entry-1"]

    refreshed = restarted.refresh_rewards("reward-bound-session")
    assert refreshed["state"] == "complete"
    assert refreshed["reward_observation"]["reward_date"] == "2026-09-14"
    assert refreshed["reward_observation"]["account_amount"] == Decimal("0.80")
    assert refreshed["reward_observation"]["status"] == "below"
    assert refreshed["trade_pnl"] == Decimal("0.10")
    assert exchange.requested_dates == ["2026-09-14", "2026-09-14"]

    current[0] = datetime(2026, 9, 15, 1, 1, tzinfo=UTC)
    final = restarted.refresh_rewards("reward-bound-session")
    assert final["state"] == "complete"
    assert final["reward_observation"]["reward_date"] == "2026-09-14"
    assert final["reward_observation"]["account_amount"] == Decimal("0.80")
    assert final["reward_observation"]["status"] == "below"
    assert exchange.requested_dates == [
        "2026-09-14",
        "2026-09-14",
        "2026-09-14",
    ]

    current[0] = datetime(2026, 9, 15, 1, 2, tzinfo=UTC)
    retained = restarted.refresh_rewards("reward-bound-session")
    assert retained["state"] == "complete"
    assert retained["reward_observation"]["reward_date"] == "2026-09-14"
    assert retained["reward_observation"]["account_amount"] == Decimal("0.80")
    assert exchange.requested_dates == [
        "2026-09-14",
        "2026-09-14",
        "2026-09-14",
    ]


def test_preview_rejects_invalid_or_unknown_inputs(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    invalid_snapshot = _snapshot(now)
    invalid_snapshot["market"] = {
        **invalid_snapshot["market"],  # type: ignore[dict-item]
        "tick_size": None,
    }
    exchange.snapshot_value = invalid_snapshot
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )

    invalid = service.preview(_request(now))
    assert invalid["state"] == "rejected"
    assert invalid["reason"] == "tick_size_unknown"

    exchange.snapshot_value = _snapshot(now)
    valid = service.preview(_request(now))
    assert valid["state"] == "previewed"
    assert valid["preview_id"]
    assert exchange.posts == []


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("account", "authenticated"), None, "account_unknown"),
        (("account", "balance"), None, "balance_unknown"),
        (("account", "allowance"), Decimal("1"), "balance_insufficient"),
        (("market", "fee"), None, "fee_unknown"),
        (("market", "accepting_orders"), False, "market_not_accepting"),
        (("market", "exchange_type"), "UNKNOWN", "exchange_unknown"),
        (("market", "reward_min_size"), None, "reward_min_size_unknown"),
        (("book", "asks"), [], "book_invalid"),
        (("book", "received_at"), None, "book_freshness_unknown"),
    ],
)
def test_preview_rejects_missing_exchange_truth(tmp_path, path, value, reason) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    facts = _snapshot(now)
    section = dict(facts[path[0]])  # type: ignore[index]
    section[path[1]] = value
    facts[path[0]] = section
    exchange = _Exchange()
    exchange.snapshot_value = facts
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )

    result = service.preview(_request(now))

    assert result["state"] == "rejected"
    assert result["reason"] == reason
    assert exchange.posts == []


def test_stop_loss_is_per_opening_and_includes_partial_exits(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    base = _snapshot(now)
    base["market"] = {
        **base["market"],  # type: ignore[dict-item]
        "tick_size": Decimal("0.001"),
        "taker_fee_rate": Decimal("0.009558"),
    }
    request = {**_request(now), "quantity": Decimal("100")}
    exchange.snapshot_value = base
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    preview = service.preview(request)
    started = service.start(str(preview["preview_id"]), "lp-stop-loss-1")
    assert started["state"] in {"entry_open", "entry_submit_pending"}

    buy_trade = {
        "trade_id": "buy-1",
        "token_id": "0x" + "1" * 64,
        "side": "BUY",
        "status": "CONFIRMED",
        "maker_orders": [
            {
                "order_id": "order-1",
                "token_id": "0x" + "1" * 64,
                "side": "BUY",
                "matched_amount": Decimal("100"),
                "price": Decimal("0.30"),
                "fee": Decimal("0"),
            }
        ],
    }
    sell_trade = {
        "trade_id": "sell-1",
        "token_id": "0x" + "1" * 64,
        "side": "SELL",
        "status": "CONFIRMED",
        "maker_orders": [
            {
                "order_id": "order-2",
                "token_id": "0x" + "1" * 64,
                "side": "SELL",
                "matched_amount": Decimal("40"),
                "price": Decimal("0.29"),
                "fee": Decimal("0"),
            }
        ],
    }
    entry_filled = {
        **base,
        "account": {
            **base["account"],  # type: ignore[dict-item]
            "positions": [{"token_id": "0x" + "1" * 64, "size": Decimal("100")}],
        },
        "book": {
            "timestamp": now,
            "received_at": now,
            "asks": [
                    {"price": Decimal("0.29"), "size": Decimal("100")}
            ],
            "bids": [{"price": Decimal("0.28"), "size": Decimal("100")}],
        },
        "trades": [buy_trade],
        "orders": [
                {
                    "order_id": "order-1", "token_id": "0x" + "1" * 64, "side": "BUY",
                    "status": "FILLED", "price": Decimal("0.30"),
                    "original_size": Decimal("100"), "size_matched": Decimal("100"),
                }
        ],
        "scoring": True,
    }
    partial = {
        **entry_filled,
        "account": {
            **entry_filled["account"],  # type: ignore[dict-item]
            "positions": [{"token_id": "0x" + "1" * 64, "size": Decimal("60")}],
        },
        "book": {
            "timestamp": now,
            "received_at": now,
            "asks": [{"price": Decimal("0.29"), "size": Decimal("100")}],
            "bids": [{"price": Decimal("0.226"), "size": Decimal("60")}],
        },
        "trades": [buy_trade, sell_trade],
        "orders": [
            {
                "order_id": "order-1", "token_id": "0x" + "1" * 64, "side": "BUY",
                "status": "FILLED", "price": Decimal("0.30"),
                "original_size": Decimal("100"), "size_matched": Decimal("100"),
            },
            {
                "order_id": "order-2", "token_id": "0x" + "1" * 64, "side": "SELL",
                "status": "LIVE", "price": Decimal("0.29"),
                "original_size": Decimal("60"), "size_matched": Decimal("0"),
                "remaining_size": Decimal("60"),
            },
        ],
    }
    trigger = {
        **partial,
        "book": {
            "timestamp": now,
            "received_at": now,
            "asks": [{"price": Decimal("0.29"), "size": Decimal("100")}],
            "bids": [{"price": Decimal("0.225"), "size": Decimal("60")}],
        },
    }
    cancelled = {
        **trigger,
        "orders": [
            {
                "order_id": "order-1", "token_id": "0x" + "1" * 64, "side": "BUY",
                "status": "FILLED", "price": Decimal("0.30"),
                "original_size": Decimal("100"), "size_matched": Decimal("100"),
            },
            {
                "order_id": "order-2", "token_id": "0x" + "1" * 64, "side": "SELL",
                "status": "CANCELED", "price": Decimal("0.29"),
                "original_size": Decimal("60"), "size_matched": Decimal("0"),
            },
        ],
    }
    later = {
        **cancelled,
        "book": {
            "timestamp": now,
            "received_at": now,
            "asks": [{"price": Decimal("0.29"), "size": Decimal("100")}],
            "bids": [{"price": Decimal("0.226"), "size": Decimal("60")}],
        },
        "orders": [
            {
                "order_id": "order-1", "token_id": "0x" + "1" * 64, "side": "BUY",
                "status": "FILLED", "price": Decimal("0.30"),
                "original_size": Decimal("100"), "size_matched": Decimal("100"),
            },
            {
                "order_id": "order-2", "token_id": "0x" + "1" * 64, "side": "SELL",
                "status": "CANCELED", "price": Decimal("0.29"),
                "original_size": Decimal("60"), "size_matched": Decimal("0"),
            },
            {
                "order_id": "stop-1", "token_id": "0x" + "1" * 64, "side": "SELL",
                "status": "LIVE", "price": Decimal("0.225"),
                "original_size": Decimal("60"), "size_matched": Decimal("0"),
            },
        ],
    }
    exchange.snapshots = [
        base,
        base,
        entry_filled,
        partial,
        trigger,
        cancelled,
        later,
    ]

    first = service.tick()
    assert first["opening_loss"] < Decimal("5")
    assert first["stop_loss_latched"] is False

    second = service.tick()
    assert second["opening_loss"] == Decimal("4.94032")
    assert second["stop_loss_latched"] is False
    assert exchange.cancels == []

    third = service.tick()
    assert third["opening_loss"] == Decimal("5.00")
    assert third["stop_loss_latched"] is True
    assert third["state"] == "stop_loss_exit"
    assert exchange.cancels == ["order-2"]
    assert exchange.protected_sells == []

    fourth = service.tick()
    assert fourth["opening_loss"] == Decimal("5.00")
    assert fourth["stop_loss_latched"] is True
    assert exchange.protected_sells == [
        {
            "token_id": "0x" + "1" * 64,
            "quantity": Decimal("60"),
            "min_price": Decimal("0.225"),
        }
    ]

    fifth = service.tick()
    assert fifth["residual_quantity"] == Decimal("60")
    assert fifth["stop_loss_latched"] is True
    assert fifth["state"] == "stop_loss_exit"
    passive_orders = [
        item for item in exchange.limit_orders if item["side"] == "SELL"
    ]
    assert len(passive_orders) == 1
    assert passive_orders[0]["price"] == Decimal("0.29")
    assert len(exchange.protected_sells) == 1


def test_stop_trigger_evidence_is_latched_across_restart(tmp_path) -> None:
    now = datetime(2026, 9, 14, 23, 40, tzinfo=UTC)
    review_at = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    current = [now]
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])
    preview = service.preview(
        {**_request(now), "quantity": Decimal("100"), "review_at": review_at}
    )
    started = service.start(str(preview["preview_id"]), "lp-trigger-evidence-1")
    session_id = str(started["session_id"])
    store.lp_update_session(
        session_id, patch={"paid_rewards": Decimal("100")}
    )

    exchange.snapshot_value = _inventory_snapshot(
        current[0],
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("25.01"),
    )
    below_trigger = service.tick()
    assert below_trigger["opening_loss"] == Decimal("4.99")
    assert below_trigger["stop_loss_latched"] is False
    assert below_trigger["stop_loss_triggered_at"] is None
    assert below_trigger["stop_loss_triggered_loss"] is None

    current[0] = now + timedelta(minutes=1)
    exchange.snapshot_value = _inventory_snapshot(
        current[0],
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("24.99"),
        passive_status="LIVE",
    )
    triggered = service.tick()
    triggered_at = current[0].isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert triggered["opening_loss"] == Decimal("5.01")
    assert triggered["stop_loss_latched"] is True
    assert triggered["stop_loss_triggered_at"] == triggered_at
    assert triggered["stop_loss_triggered_loss"] == Decimal("5.01")

    current[0] = now + timedelta(minutes=2)
    exchange.snapshot_value = _inventory_snapshot(
        current[0],
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("25.20"),
        passive_status="LIVE",
    )
    later = service.tick()
    assert later["opening_loss"] == Decimal("4.80")
    assert later["stop_loss_triggered_at"] == triggered_at
    assert later["stop_loss_triggered_loss"] == Decimal("5.01")

    restarted = PolymarketLPService(store, exchange, clock=lambda: current[0])
    recovered = restarted.status(session_id)
    assert recovered["stop_loss_triggered_at"] == triggered_at
    assert recovered["stop_loss_triggered_loss"] == Decimal("5.01")
    assert recovered["paid_rewards"] == Decimal("100")
    assert recovered["opening_loss"] == Decimal("4.80")

    current[0] = review_at
    exchange.snapshot_value = _inventory_snapshot(
        current[0],
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("25.20"),
        passive_status="LIVE",
    )
    reviewed = restarted.tick()
    assert reviewed["state"] == "review"
    assert reviewed["stop_loss_triggered_at"] == triggered_at
    assert reviewed["stop_loss_triggered_loss"] == Decimal("5.01")


def test_entry_is_fixed_post_only_gtd_and_idempotent(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )

    request = _request(now)
    preview = service.preview(request)
    first = service.start(str(preview["preview_id"]), "lp-entry-1")
    second = service.start(str(preview["preview_id"]), "lp-entry-1")

    assert first["session_id"] == second["session_id"]
    assert len(exchange.limit_orders) == 1
    order = exchange.limit_orders[0]
    assert order["side"] == "BUY"
    assert order["price"] == Decimal("0.30")
    assert order["quantity"] == Decimal("10")
    assert order["post_only"] is True
    assert order["expiration"] == int((now + timedelta(minutes=10, seconds=60)).timestamp())

    exchange.snapshot_value = {
        **_snapshot(now),
        "book": {
            "timestamp": now,
            "received_at": now,
            "asks": [{"price": Decimal("0.35"), "size": Decimal("100")}],
            "bids": [{"price": Decimal("0.34"), "size": Decimal("100")}],
        },
    }
    service.tick()
    assert len([item for item in exchange.limit_orders if item["side"] == "BUY"]) == 1

    too_soon = service.preview({**request, "review_at": now + timedelta(minutes=1)})
    expired = service.preview({**request, "review_at": now - timedelta(seconds=1)})
    assert too_soon["state"] == "rejected"
    assert expired["state"] == "rejected"


def test_rejected_entry_never_falls_back_to_taker(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    exchange.reject_entry = True
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )

    preview = service.preview(_request(now))
    result = service.start(str(preview["preview_id"]), "lp-rejected-1")

    assert result["state"] == "entry_rejected"
    assert len(exchange.limit_orders) == 1
    assert exchange.limit_orders[0]["side"] == "BUY"
    assert len(exchange.posts) == 1
    assert exchange.protected_sells == []


def test_scoring_loss_requires_continuous_window(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    current = [now]
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    exchange.scoring_responses = [False, False, True, False, False]
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current[0]
    )
    preview = service.preview(_request(now))
    started = service.start(str(preview["preview_id"]), "lp-scoring-1")

    first = service.tick()
    assert first["scoring_status"] == "false"
    assert exchange.cancels == []

    current[0] = now + timedelta(seconds=30)
    exchange.snapshot_value = _snapshot(current[0])
    second = service.tick()
    assert second["scoring_status"] == "false"
    assert exchange.cancels == []

    current[0] = now + timedelta(seconds=35)
    exchange.snapshot_value = _snapshot(current[0])
    fresh = service.tick()
    assert fresh["scoring_status"] == "true"
    assert exchange.cancels == []

    current[0] = now + timedelta(seconds=40)
    exchange.snapshot_value = _snapshot(current[0])
    service.tick()
    current[0] = now + timedelta(seconds=99)
    exchange.snapshot_value = _snapshot(current[0])
    before_deadline = service.tick()
    assert before_deadline["scoring_status"] == "false"
    assert before_deadline["state"] != "review"
    assert exchange.cancels == []

    current[0] = now + timedelta(seconds=100)
    exchange.snapshot_value = _snapshot(current[0])
    ended = service.tick()
    assert ended["scoring_status"] == "false"
    assert ended["state"] == "review"
    assert exchange.cancels == ["order-1"]

    current[0] = now + timedelta(seconds=102)
    exchange.snapshot_value = _snapshot(current[0])
    stale = service.tick()
    assert stale["scoring_status"] == "unknown"


def _inventory_snapshot(
    now: datetime,
    *,
    buy_quantity: Decimal,
    buy_cost: Decimal,
    residual: Decimal,
    residual_value: Decimal,
    sold_quantity: Decimal = Decimal("0"),
    sold_revenue: Decimal = Decimal("0"),
    fees: Decimal = Decimal("0"),
    entry_status: str = "CANCELED",
    passive_status: str | None = None,
    protected_status: str | None = None,
    orders_terminal: bool = True,
    position_flat: bool = False,
    bids: list[dict[str, Decimal]] | None = None,
) -> dict[str, object]:
    result = _snapshot(now)
    result.update(
        {
            "orders_terminal": orders_terminal,
            "position_flat": position_flat,
            "scoring": True,
        }
    )
    result["account"] = {
        **result["account"],  # type: ignore[dict-item]
        "positions": (
            []
            if residual <= 0
            else [{"token_id": "0x" + "1" * 64, "size": residual}]
        ),
    }
    orders: list[dict[str, object]] = []
    if entry_status is not None:
        orders.append(
            {
                "order_id": "order-1",
                "status": entry_status,
                "token_id": "0x" + "1" * 64,
                "side": "BUY",
                "price": Decimal("0.30"),
                "original_size": buy_quantity,
                "size_matched": buy_quantity,
            }
        )
    if passive_status is not None:
        orders.append(
            {
                "order_id": "order-2",
                "status": passive_status,
                "token_id": "0x" + "1" * 64,
                "side": "SELL",
                "price": Decimal("0.29"),
                "original_size": residual + sold_quantity,
                "size_matched": sold_quantity,
                "remaining_size": residual,
            }
        )
    if protected_status is not None:
        orders.append(
            {
                "order_id": "stop-1",
                "status": protected_status,
                "token_id": "0x" + "1" * 64,
                "side": "SELL",
                "price": Decimal("0.225"),
                "original_size": residual + sold_quantity,
                "size_matched": sold_quantity,
                "remaining_size": residual,
            }
        )
    result["orders"] = orders
    trades: list[dict[str, object]] = []
    if buy_quantity > 0:
        trades.append(
            {
                "trade_id": "buy-1",
                "token_id": "0x" + "1" * 64,
                "side": "BUY",
                "status": "CONFIRMED",
                "maker_orders": [
                    {
                        "order_id": "order-1",
                        "token_id": "0x" + "1" * 64,
                        "side": "BUY",
                        "matched_amount": buy_quantity,
                        "price": buy_cost / buy_quantity,
                        "fee": Decimal("0"),
                    }
                ],
            }
        )
    if sold_quantity > 0:
        sell_order_id = (
            "order-2"
            if passive_status is not None
            else "stop-1"
            if protected_status is not None
            else ""
        )
        if sell_order_id:
            trades.append(
                {
                    "trade_id": "sell-1",
                    "token_id": "0x" + "1" * 64,
                    "side": "SELL",
                    "status": "CONFIRMED",
                    "maker_orders": [
                        {
                            "order_id": sell_order_id,
                            "token_id": "0x" + "1" * 64,
                            "side": "SELL",
                            "matched_amount": sold_quantity,
                            "price": sold_revenue / sold_quantity,
                            "fee": fees,
                        }
                    ],
                }
            )
    result["trades"] = trades
    if bids is not None:
        result["book"] = {
            **result["book"],  # type: ignore[dict-item]
            "bids": bids,
        }
    elif residual > 0 and residual_value > 0:
        result["book"] = {
            **result["book"],  # type: ignore[dict-item]
            "bids": [{"price": residual_value / residual, "size": residual}],
        }
    elif residual > 0:
        result["book"] = {
            **result["book"],  # type: ignore[dict-item]
            "bids": [],
        }
    return result


def test_cancel_fill_race_opens_exit_for_actual_inventory(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    base = _snapshot(now)
    exchange.snapshot_value = base
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    preview = service.preview({**_request(now), "quantity": Decimal("40")})
    service.start(str(preview["preview_id"]), "lp-race-1")
    partial = _inventory_snapshot(
        now,
        buy_quantity=Decimal("13"),
        buy_cost=Decimal("3.90"),
        residual=Decimal("13"),
        residual_value=Decimal("3.77"),
        entry_status="LIVE",
        orders_terminal=False,
    )
    settled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
    )
    exchange.snapshots = [base, base, partial, settled]

    first = service.tick()
    assert first["residual_quantity"] == Decimal("13")
    assert exchange.cancels == ["order-1"]
    assert [item for item in exchange.limit_orders if item["side"] == "SELL"] == []

    second = service.tick()
    sells = [item for item in exchange.limit_orders if item["side"] == "SELL"]
    assert second["residual_quantity"] == Decimal("15")
    assert len(sells) == 1
    assert sells[0]["quantity"] == Decimal("15")

    duplicate_trade = {
        **settled,
        "buy_filled_quantity": None,
        "buy_cost": None,
        "trades": [
            {
                "trade_id": "trade-1",
                "status": "CONFIRMED",
                "side": "BUY",
                "maker_orders": [
                    {
                        "order_id": "order-1",
                        "matched_amount": Decimal("15"),
                        "price": Decimal("0.30"),
                        "side": "BUY",
                    }
                ],
            },
            {
                "trade_id": "trade-1",
                "status": "CONFIRMED",
                "side": "BUY",
                "maker_orders": [
                    {
                        "order_id": "order-1",
                        "matched_amount": Decimal("15"),
                        "price": Decimal("0.30"),
                        "side": "BUY",
                    }
                ],
            },
        ],
    }
    exchange.snapshot_value = duplicate_trade
    exchange.snapshots = []
    status = service.tick()
    assert status["buy_filled_quantity"] == Decimal("15")
    assert status["buy_cost"] == Decimal("4.50")


def test_maker_exit_reprices_without_overselling(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    base = _snapshot(now)
    exchange.snapshot_value = base
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    preview = service.preview({**_request(now), "quantity": Decimal("15")})
    service.start(str(preview["preview_id"]), "lp-reprice-1")
    filled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
    )
    changed = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        sold_quantity=Decimal("3"),
        sold_revenue=Decimal("0.93"),
        residual=Decimal("12"),
        residual_value=Decimal("3.48"),
        passive_status="LIVE",
        orders_terminal=False,
    )
    settled_cancel = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        sold_quantity=Decimal("3"),
        sold_revenue=Decimal("0.93"),
        residual=Decimal("12"),
        residual_value=Decimal("3.48"),
        passive_status="CANCELED",
    )
    for item in (changed, settled_cancel):
        item["book"] = {
            **item["book"],  # type: ignore[dict-item]
            "asks": [{"price": Decimal("0.32"), "size": Decimal("100")}],
        }
    exchange.snapshots = [base, base, filled, changed, settled_cancel]

    service.tick()
    assert [item["quantity"] for item in exchange.limit_orders if item["side"] == "SELL"] == [Decimal("15")]
    service.tick()
    assert exchange.cancels == ["order-2"]
    assert [item["quantity"] for item in exchange.limit_orders if item["side"] == "SELL"] == [Decimal("15")]
    service.tick()
    assert [item["quantity"] for item in exchange.limit_orders if item["side"] == "SELL"] == [Decimal("15"), Decimal("12")]
    assert all(item["quantity"] <= Decimal("15") for item in exchange.limit_orders if item["side"] == "SELL")


def test_exit_waits_for_terminal_receipt_before_retry(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    base = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        sold_quantity=Decimal("40"),
        sold_revenue=Decimal("11.60"),
        residual=Decimal("60"),
        residual_value=Decimal("13.50"),
        fees=Decimal("0.10"),
    )
    exchange.snapshot_value = _snapshot(now)
    exchange.protected_responses = [
        {"order_id": "", "status": "REJECTED"},
        TimeoutError("receipt timeout"),
        {"order_id": "stop-3", "status": "FILLED"},
    ]
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    preview = service.preview({**_request(now), "quantity": Decimal("100")})
    service.start(str(preview["preview_id"]), "lp-exit-receipt-1")
    # Observe the confirmed opening first so the service owns the passive
    # order whose later partial fill drives the stop-loss calculation.
    exchange.snapshots = [
        _inventory_snapshot(
            now,
            buy_quantity=Decimal("100"),
            buy_cost=Decimal("30"),
            residual=Decimal("100"),
            residual_value=Decimal("29"),
        )
    ]
    service.tick()
    base["passive_status"] = "CANCELED"
    base["orders"] = [
        {"order_id": "order-1", "status": "CANCELED"},
        {"order_id": "order-2", "status": "CANCELED"},
    ]
    base["trades"].append(
        {
            "trade_id": "sell-1",
            "token_id": "0x" + "1" * 64,
            "side": "SELL",
            "status": "CONFIRMED",
            "maker_orders": [
                {
                    "order_id": "order-2",
                    "token_id": "0x" + "1" * 64,
                    "side": "SELL",
                    "matched_amount": Decimal("40"),
                    "price": Decimal("0.29"),
                    "fee": Decimal("0.10"),
                }
            ],
        }
    )
    exchange.snapshot_value = base
    exchange.snapshots = []

    first = service.tick()
    assert first["state"] == "stop_loss_exit"
    assert first["protected_exit_attempt_state"] == "rejected"
    assert len(exchange.protected_sells) == 1
    second = service.tick()
    assert second["protected_exit_attempt_state"] == "unknown"
    assert len(exchange.protected_sells) == 2
    third = service.tick()
    assert third["protected_exit_attempt_state"] == "unknown"
    assert len(exchange.protected_sells) == 2


def test_restart_reconciles_without_recreating_orders(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    base = _snapshot(now)
    exchange.snapshot_value = base
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview({**_request(now), "quantity": Decimal("15")})
    started = service.start(str(preview["preview_id"]), "lp-restart-1")
    filled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
    )
    exchange.snapshots = [base, base, filled]
    service.tick()
    assert len(exchange.limit_orders) == 2

    live = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        passive_status="LIVE",
        orders_terminal=False,
    )
    exchange.snapshot_value = live
    exchange.snapshots = []
    restarted = PolymarketLPService(store, exchange, clock=lambda: now)
    assert restarted.status(started["session_id"])["session_id"] == started["session_id"]
    restarted.tick()
    assert len(exchange.limit_orders) == 2

    stopped = restarted.stop(started["session_id"])
    assert stopped["state"] == "review"
    reviewed = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        passive_status="CANCELED",
    )
    exchange.snapshot_value = reviewed
    resumed = PolymarketLPService(store, exchange, clock=lambda: now)
    resumed.tick()
    assert len([item for item in exchange.limit_orders if item["side"] == "BUY"]) == 1
    assert len([item for item in exchange.limit_orders if item["side"] == "SELL"]) == 1


def test_review_deadline_cancels_without_forcing_sale(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    current = [now]
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current[0]
    )
    preview = service.preview({**_request(now), "quantity": Decimal("10")})
    started = service.start(str(preview["preview_id"]), "lp-review-1")
    stopped = service.stop(started["session_id"])
    assert stopped["state"] == "review"
    inventory = _inventory_snapshot(
        now,
        buy_quantity=Decimal("10"),
        buy_cost=Decimal("3"),
        residual=Decimal("10"),
        residual_value=Decimal("2.90"),
    )
    exchange.snapshot_value = inventory
    reviewed = service.tick()
    assert reviewed["state"] == "review"
    assert reviewed["residual_quantity"] == Decimal("10")
    assert exchange.protected_sells == []
    assert [item for item in exchange.limit_orders if item["side"] == "SELL"] == []

    current[0] = now + timedelta(minutes=10)
    exchange.snapshot_value = {
        **inventory,
        "book": {**inventory["book"], "received_at": current[0]},  # type: ignore[dict-item]
    }
    still_review = service.tick()
    assert still_review["state"] == "review"
    assert exchange.protected_sells == []


def test_deadline_cancel_rejection_is_retried_after_review_resume(tmp_path) -> None:
    now = datetime(2026, 9, 14, 23, 50, tzinfo=UTC)
    review_at = now + timedelta(minutes=10)
    current = [now]
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])
    preview = service.preview({**_request(now), "review_at": review_at})
    started = service.start(str(preview["preview_id"]), "lp-review-cancel-retry-1")
    session_id = str(started["session_id"])

    def order_snapshot(at: datetime, status: str) -> dict[str, object]:
        snapshot = _snapshot(at)
        order = {
            "order_id": "order-1",
            "status": status,
            "token_id": "0x" + "1" * 64,
            "side": "BUY",
            "price": Decimal("0.30"),
            "original_size": Decimal("10"),
            "size_matched": Decimal("0"),
            "remaining_size": Decimal("10"),
        }
        snapshot["orders"] = [order]
        account = snapshot["account"]
        assert isinstance(account, dict)
        snapshot["account"] = {
            **account,
            "open_orders": [order] if status == "LIVE" else [],
        }
        return snapshot

    current[0] = review_at
    exchange.snapshot_value = order_snapshot(current[0], "LIVE")
    exchange.cancel_responses = [
        {"canceled": [], "not_canceled": {"order-1": "still_live"}}
    ]
    rejected = service.tick()
    assert rejected["state"] == "needs_attention"
    assert rejected["resume_state"] == "review"
    assert rejected["review_status"] == "awaiting_reconciliation"
    assert rejected["order_history"]["order-1"]["status"] == "LIVE"  # type: ignore[index]
    assert rejected["orders_terminal"] is False
    assert rejected["position_reconciled"] is True
    assert exchange.cancels == ["order-1"]

    current[0] = review_at + timedelta(seconds=1)
    exchange.snapshot_value = order_snapshot(current[0], "LIVE")
    exchange.cancel_responses = [{"canceled": ["order-1"], "status": "CANCELED"}]
    restarted = PolymarketLPService(store, exchange, clock=lambda: current[0])
    accepted_but_live = restarted.tick()
    assert accepted_but_live["state"] == "review"
    assert accepted_but_live["review_status"] == "awaiting_reconciliation"
    assert accepted_but_live["order_history"]["order-1"]["status"] == "LIVE"  # type: ignore[index]
    assert accepted_but_live["orders_terminal"] is False
    assert exchange.cancels == ["order-1", "order-1"]
    assert len([order for order in exchange.posts if order.get("side") == "BUY"]) == 1

    current[0] = review_at + timedelta(seconds=2)
    exchange.snapshot_value = order_snapshot(current[0], "CANCELED")
    confirmed = restarted.tick()
    assert confirmed["state"] == "complete"
    assert confirmed["order_history"]["order-1"]["status"] == "CANCELED"  # type: ignore[index]
    assert confirmed["orders_terminal"] is True
    assert confirmed["position_reconciled"] is True
    assert exchange.cancels == ["order-1", "order-1"]
    assert len([order for order in exchange.posts if order.get("side") == "BUY"]) == 1


def test_review_keeps_stop_loss_and_no_repeat_entry(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    preview = service.preview({**_request(now), "quantity": Decimal("100")})
    started = service.start(str(preview["preview_id"]), "lp-review-stop-1")
    exchange.snapshot_value = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("29"),
    )
    service.tick()
    service.stop(started["session_id"])
    no_bid = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        sold_quantity=Decimal("40"),
        sold_revenue=Decimal("11.60"),
        residual=Decimal("60"),
        residual_value=Decimal("13.50"),
        fees=Decimal("0.10"),
        bids=[],
        passive_status="CANCELED",
    )
    exchange.snapshot_value = no_bid
    waiting = service.tick()
    assert waiting["state"] == "review"
    assert waiting["stop_loss_latched"] is False
    assert exchange.protected_sells == []

    with_bid = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        sold_quantity=Decimal("40"),
        sold_revenue=Decimal("11.60"),
        residual=Decimal("60"),
        residual_value=Decimal("13.50"),
        fees=Decimal("0.10"),
        passive_status="CANCELED",
    )
    exchange.snapshot_value = with_bid
    exiting = service.tick()
    assert exiting["state"] == "stop_loss_exit"
    assert exiting["stop_loss_latched"] is True
    assert len(exchange.protected_sells) == 1
    assert len([item for item in exchange.limit_orders if item["side"] == "BUY"]) == 1

    flat = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        sold_quantity=Decimal("100"),
        sold_revenue=Decimal("25.10"),
        residual=Decimal("0"),
        residual_value=Decimal("0"),
        fees=Decimal("0.10"),
        protected_status="FILLED",
        position_flat=True,
    )
    exchange.snapshot_value = flat
    completed = service.tick()
    assert completed["state"] == "complete"
    assert len(exchange.protected_sells) == 1


def test_completion_requires_terminal_orders_and_flat_position(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    preview = service.preview({**_request(now), "quantity": Decimal("15")})
    started = service.start(str(preview["preview_id"]), "lp-completion-1")
    unsettled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("2.70"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        entry_status="FILLED",
        orders_terminal=True,
        position_flat=False,
    )
    exchange.snapshot_value = unsettled
    first = service.tick()
    assert first["state"] != "complete"

    not_flat = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("2.70"),
        sold_quantity=Decimal("15"),
        sold_revenue=Decimal("2.50"),
        residual=Decimal("0"),
        residual_value=Decimal("0"),
        fees=Decimal("0.03"),
        passive_status="LIVE",
        orders_terminal=False,
        position_flat=False,
    )
    exchange.snapshot_value = not_flat
    second = service.tick()
    assert second["state"] != "complete"

    flat = {
        **not_flat,
        "orders": [
            {"order_id": "order-1", "status": "FILLED"},
            {"order_id": "order-2", "status": "FILLED"},
        ],
        "orders_terminal": True,
        "position_flat": True,
    }
    exchange.snapshot_value = flat
    completed = service.tick()
    assert completed["state"] == "complete"
    assert completed["trade_pnl"] == Decimal("-0.23")
    assert completed["total_pnl"] is None
    assert completed["reward_status"] == "unknown"
    assert service.status(started["session_id"])["state"] == "complete"
    assert service.status()["state"] == "complete"


def test_flat_completion_preserves_unknown_economics(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

    def flat_snapshot(passive_status: str) -> dict[str, object]:
        result = _inventory_snapshot(
            now,
            buy_quantity=Decimal("15"),
            buy_cost=Decimal("4.50"),
            sold_quantity=Decimal("15"),
            sold_revenue=Decimal("3.75"),
            residual=Decimal("0"),
            residual_value=Decimal("0"),
            entry_status="FILLED",
            passive_status=passive_status,
            position_flat=True,
        )
        result["market"] = {
            **result["market"],  # type: ignore[dict-item]
            "fees_enabled": True,
            "fee": None,
            "taker_fee_rate": None,
        }
        for trade in result["trades"]:  # type: ignore[index]
            for order in trade["maker_orders"]:  # type: ignore[index]
                order.pop("fee", None)
        return result

    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "normal"), exchange, clock=lambda: now
    )
    preview = service.preview({**_request(now), "quantity": Decimal("15")})
    started = service.start(str(preview["preview_id"]), "lp-flat-unknown-normal")
    exchange.snapshot_value = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        entry_status="FILLED",
    )
    entered = service.tick()
    assert entered["state"] == "passive_exit"
    exchange.snapshot_value = flat_snapshot("FILLED")
    completed = service.tick()
    assert completed["state"] == "complete"
    assert completed["trade_pnl"] is None
    assert completed["total_pnl"] is None
    assert completed["reward_status"] == "unknown"
    assert service.status(started["session_id"])["state"] == "complete"
    assert len([item for item in exchange.limit_orders if item["side"] == "SELL"]) == 1

    review_exchange = _Exchange()
    review_exchange.snapshot_value = _snapshot(now)
    review_service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "review"),
        review_exchange,
        clock=lambda: now,
    )
    review_preview = review_service.preview(
        {**_request(now), "quantity": Decimal("15")}
    )
    review_started = review_service.start(
        str(review_preview["preview_id"]), "lp-flat-unknown-review"
    )
    review_exchange.snapshot_value = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        entry_status="FILLED",
    )
    review_service.tick()
    stopped = review_service.stop(review_started["session_id"])
    assert stopped["state"] == "review"
    review_exchange.snapshot_value = flat_snapshot("CANCELED")
    reviewed = review_service.tick()
    assert reviewed["state"] == "complete"
    assert reviewed["trade_pnl"] is None
    assert reviewed["total_pnl"] is None

    nonflat_exchange = _Exchange()
    nonflat_exchange.snapshot_value = _snapshot(now)
    nonflat_service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "nonflat"),
        nonflat_exchange,
        clock=lambda: now,
    )
    nonflat_preview = nonflat_service.preview(
        {**_request(now), "quantity": Decimal("15")}
    )
    nonflat_service.start(str(nonflat_preview["preview_id"]), "lp-flat-unknown-nonflat")
    nonflat_exchange.snapshot_value = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        entry_status="FILLED",
        passive_status="CANCELED",
        position_flat=False,
    )
    nonflat = nonflat_service.tick()
    assert nonflat["state"] != "complete"


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("post timeout"), BaseException("process lost")],
    ids=["timeout", "crash"],
)
def test_passive_submit_unknown_or_crash_never_reposts(tmp_path, failure) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    base = _snapshot(now)
    exchange.snapshot_value = base
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview({**_request(now), "quantity": Decimal("15")})
    started = service.start(str(preview["preview_id"]), "lp-passive-submit-replay")

    filled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        entry_status="FILLED",
    )
    exchange.snapshot_value = filled
    exchange.post_failures = [failure]
    if isinstance(failure, BaseException) and not isinstance(failure, Exception):
        with pytest.raises(BaseException):
            service.tick()
    else:
        first = service.tick()
        assert first["state"] == "needs_attention"

    restarted = PolymarketLPService(store, exchange, clock=lambda: now)
    second = restarted.tick()
    third = restarted.tick()
    assert second["residual_quantity"] == Decimal("15")
    assert third["residual_quantity"] == Decimal("15")
    assert second["state"] != "complete"
    assert third["state"] != "complete"
    assert len([item for item in exchange.limit_orders if item["side"] == "BUY"]) == 1
    assert len([item for item in exchange.limit_orders if item["side"] == "SELL"]) == 1
    actions = store.lp_actions(started["session_id"])
    passive_actions = [
        action for action in actions if action.get("role") == "passive_exit"
    ]
    assert passive_actions
    assert passive_actions[-1]["state"] in {"pending", "unknown"}


@pytest.mark.parametrize("role", ["entry", "passive", "protected"])
def test_unresolved_submission_keeps_flat_session_active(tmp_path, role) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview({**_request(now), "quantity": Decimal("100")})

    if role == "entry":
        exchange.post_failures = [TimeoutError("entry receipt timeout")]
        started = service.start(str(preview["preview_id"]), "lp-unresolved-entry")
        assert started["state"] == "needs_attention"
        assert started["submit_status"] == "unknown"
        exchange.snapshot_value = _snapshot(now)
    else:
        started = service.start(str(preview["preview_id"]), f"lp-unresolved-{role}")
        entered = _inventory_snapshot(
            now,
            buy_quantity=Decimal("100"),
            buy_cost=Decimal("30"),
            residual=Decimal("100"),
            residual_value=Decimal("29"),
            entry_status="FILLED",
        )
        exchange.snapshot_value = entered
        service.tick()

        if role == "passive":
            repriced = _inventory_snapshot(
                now,
                buy_quantity=Decimal("100"),
                buy_cost=Decimal("30"),
                residual=Decimal("100"),
                residual_value=Decimal("29"),
                entry_status="FILLED",
                passive_status="LIVE",
            )
            repriced["book"] = {
                **repriced["book"],  # type: ignore[dict-item]
                "asks": [{"price": Decimal("0.32"), "size": Decimal("100")}],
            }
            exchange.snapshot_value = repriced
            service.tick()

            exchange.post_failures = [TimeoutError("passive receipt timeout")]
            replacement = _inventory_snapshot(
                now,
                buy_quantity=Decimal("100"),
                buy_cost=Decimal("30"),
                residual=Decimal("100"),
                residual_value=Decimal("29"),
                entry_status="FILLED",
                passive_status="CANCELED",
            )
            replacement["book"] = {
                **replacement["book"],  # type: ignore[dict-item]
                "asks": [{"price": Decimal("0.32"), "size": Decimal("100")}],
            }
            exchange.snapshot_value = replacement
            unresolved = service.tick()
            assert unresolved["state"] == "needs_attention"
            assert unresolved["passive_exit_attempt_state"] == "unknown"
            flat = _inventory_snapshot(
                now,
                buy_quantity=Decimal("100"),
                buy_cost=Decimal("30"),
                sold_quantity=Decimal("100"),
                sold_revenue=Decimal("29"),
                residual=Decimal("0"),
                residual_value=Decimal("0"),
                entry_status="FILLED",
                passive_status="CANCELED",
                position_flat=True,
            )
        else:
            trigger = _inventory_snapshot(
                now,
                buy_quantity=Decimal("100"),
                buy_cost=Decimal("30"),
                residual=Decimal("100"),
                residual_value=Decimal("25"),
                entry_status="FILLED",
                passive_status="LIVE",
                bids=[{"price": Decimal("0.25"), "size": Decimal("100")}],
            )
            exchange.snapshot_value = trigger
            service.tick()
            cancelled = _inventory_snapshot(
                now,
                buy_quantity=Decimal("100"),
                buy_cost=Decimal("30"),
                residual=Decimal("100"),
                residual_value=Decimal("25"),
                entry_status="FILLED",
                passive_status="CANCELED",
                bids=[{"price": Decimal("0.25"), "size": Decimal("100")}],
            )
            exchange.protected_responses = [TimeoutError("protected receipt timeout")]
            exchange.snapshot_value = cancelled
            unresolved = service.tick()
            assert unresolved["state"] == "stop_loss_exit"
            assert unresolved["protected_exit_attempt_state"] == "unknown"
            flat = _inventory_snapshot(
                now,
                buy_quantity=Decimal("100"),
                buy_cost=Decimal("30"),
                sold_quantity=Decimal("100"),
                sold_revenue=Decimal("29"),
                residual=Decimal("0"),
                residual_value=Decimal("0"),
                entry_status="FILLED",
                passive_status="CANCELED",
                position_flat=True,
            )
        exchange.snapshot_value = flat

    restarted = PolymarketLPService(store, exchange, clock=lambda: now)
    first = restarted.tick()
    second = restarted.tick()
    assert first["state"] != "complete"
    assert second["state"] != "complete"
    assert restarted.status(started["session_id"])["state"] != "complete"
    assert len([item for item in exchange.limit_orders if item["side"] == "BUY"]) == 1
    expected_sell_attempts = 0 if role == "entry" else 2 if role == "passive" else 1
    assert len([item for item in exchange.limit_orders if item["side"] == "SELL"]) == expected_sell_attempts
    if role == "protected":
        assert len(exchange.protected_sells) == 1


def test_unknown_passive_submission_blocks_protected_exit(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview({**_request(now), "quantity": Decimal("100")})
    started = service.start(str(preview["preview_id"]), "lp-cross-role-unknown")

    entered = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("29"),
        entry_status="FILLED",
    )
    exchange.snapshot_value = entered
    service.tick()
    assert len([item for item in exchange.posts if item["side"] == "BUY"]) == 1
    assert len([item for item in exchange.posts if item["side"] == "SELL"]) == 1

    repriced = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("29"),
        entry_status="FILLED",
        passive_status="LIVE",
    )
    repriced["book"] = {
        **repriced["book"],  # type: ignore[dict-item]
        "asks": [{"price": Decimal("0.32"), "size": Decimal("100")}],
    }
    exchange.snapshot_value = repriced
    service.tick()

    exchange.post_failures = [TimeoutError("passive receipt timeout")]
    cancelled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("29"),
        entry_status="FILLED",
        passive_status="CANCELED",
    )
    cancelled["book"] = {
        **cancelled["book"],  # type: ignore[dict-item]
        "asks": [{"price": Decimal("0.32"), "size": Decimal("100")}],
    }
    exchange.snapshot_value = cancelled
    unresolved = service.tick()
    assert unresolved["state"] == "needs_attention"
    assert unresolved["passive_exit_attempt_state"] == "unknown"
    assert len([item for item in exchange.posts if item["side"] == "SELL"]) == 2

    trigger = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("25"),
        entry_status="FILLED",
        bids=[{"price": Decimal("0.25"), "size": Decimal("100")}],
    )
    exchange.snapshot_value = trigger
    crossed = service.tick()
    assert crossed["state"] == "stop_loss_exit"
    assert crossed["stop_loss_latched"] is True
    assert crossed["residual_quantity"] == Decimal("100")
    assert crossed["passive_exit_attempt_state"] == "unknown"
    assert exchange.protected_sells == []

    restarted = PolymarketLPService(store, exchange, clock=lambda: now)
    after_restart = restarted.tick()
    assert after_restart["state"] == "stop_loss_exit"
    assert after_restart["stop_loss_latched"] is True
    assert exchange.protected_sells == []

    stopped = restarted.stop(started["session_id"])
    assert stopped["state"] == "review"
    exchange.snapshot_value = trigger
    after_review = restarted.tick()
    assert after_review["stop_loss_latched"] is True
    assert after_review["passive_exit_attempt_state"] == "unknown"
    assert exchange.protected_sells == []
    assert len([item for item in exchange.posts if item["side"] == "BUY"]) == 1
    assert len([item for item in exchange.posts if item["side"] == "SELL"]) == 2



def test_protected_pending_restart_and_confirmed_portions(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    token_id = "0x" + "1" * 64

    crash_dir = tmp_path / "protected-crash"
    crash_dir.mkdir()
    crash_exchange = _Exchange()
    base = _snapshot(now)
    crash_exchange.snapshot_value = base
    crash_store = PredictionArbitrageStore(crash_dir)
    crash_service = PolymarketLPService(crash_store, crash_exchange, clock=lambda: now)
    crash_preview = crash_service.preview({**_request(now), "quantity": Decimal("100")})
    crash_started = crash_service.start(
        str(crash_preview["preview_id"]), "lp-protected-crash"
    )
    entry = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("29"),
        entry_status="FILLED",
    )
    crash_exchange.snapshot_value = entry
    crash_service.tick()
    partial = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("26"),
        passive_status="LIVE",
        bids=[{"price": Decimal("0.26"), "size": Decimal("100")}],
    )
    crash_exchange.snapshot_value = partial
    crash_service.tick()
    trigger = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("25"),
        passive_status="LIVE",
        bids=[{"price": Decimal("0.25"), "size": Decimal("100")}],
    )
    crash_exchange.snapshot_value = trigger
    crash_service.tick()
    cancelled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("25"),
        passive_status="CANCELED",
        bids=[{"price": Decimal("0.25"), "size": Decimal("100")}],
    )
    crash_exchange.snapshot_value = cancelled
    crash_exchange.protected_responses = [BaseException("process lost after FOK POST")]
    with pytest.raises(BaseException):
        crash_service.tick()
    assert len(crash_exchange.protected_sells) == 1
    restarted = PolymarketLPService(crash_store, crash_exchange, clock=lambda: now)
    still_pending = restarted.tick()
    assert still_pending["state"] == "stop_loss_exit"
    assert still_pending["protected_exit_attempt_state"] == "pending"
    assert len(crash_exchange.protected_sells) == 1
    assert still_pending["residual_quantity"] == Decimal("100")
    assert restarted.status(crash_started["session_id"])["protected_exit_attempt_state"] == "pending"

    portions_dir = tmp_path / "protected-portions"
    portions_dir.mkdir()
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    store = PredictionArbitrageStore(portions_dir)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview({**_request(now), "quantity": Decimal("100")})
    started = service.start(str(preview["preview_id"]), "lp-protected-portions")
    entry = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("29"),
        entry_status="FILLED",
    )
    exchange.snapshot_value = entry
    service.tick()
    trigger = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("25"),
        passive_status="LIVE",
        bids=[{"price": Decimal("0.25"), "size": Decimal("100")}],
    )
    exchange.snapshot_value = trigger
    stop_requested = service.tick()
    assert stop_requested["state"] == "stop_loss_exit"
    cancelled_partial_depth = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("10"),
        passive_status="CANCELED",
        bids=[{"price": Decimal("0.25"), "size": Decimal("40")}],
    )
    exchange.snapshot_value = cancelled_partial_depth
    first_exit = service.tick()
    assert first_exit["state"] == "stop_loss_exit"
    assert first_exit["residual_quantity"] == Decimal("100")
    assert first_exit["residual_exit_value"] is None
    assert exchange.protected_sells == [
        {"token_id": token_id, "quantity": Decimal("40"), "min_price": Decimal("0.25")}
    ]
    unsettled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("10"),
        passive_status=None,
        protected_status="FILLED",
        bids=[{"price": Decimal("0.25"), "size": Decimal("40")}],
    )
    exchange.snapshot_value = unsettled
    before_fills = service.tick()
    assert before_fills["protected_exit_order_id"] == "stop-1"
    assert len(exchange.protected_sells) == 1

    confirmed_first = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        sold_quantity=Decimal("40"),
        sold_revenue=Decimal("10"),
        residual=Decimal("60"),
        residual_value=Decimal("15"),
        passive_status=None,
        protected_status="FILLED",
        bids=[{"price": Decimal("0.25"), "size": Decimal("60")}],
    )
    exchange.snapshot_value = confirmed_first
    after_first_fill = service.tick()
    assert after_first_fill["residual_quantity"] == Decimal("60")
    assert exchange.protected_sells == [
        {"token_id": token_id, "quantity": Decimal("40"), "min_price": Decimal("0.25")},
        {"token_id": token_id, "quantity": Decimal("60"), "min_price": Decimal("0.25")},
    ]
    assert "stop-1" in after_first_fill["order_history"]
    assert "stop-2" in after_first_fill["order_history"]

    final = {
        **confirmed_first,
        "account": {**confirmed_first["account"], "positions": []},  # type: ignore[dict-item]
        "book": {
            **confirmed_first["book"],  # type: ignore[dict-item]
            "bids": [],
        },
        "orders": [
            {"order_id": "order-1", "token_id": token_id, "side": "BUY", "status": "FILLED"},
            {"order_id": "stop-1", "token_id": token_id, "side": "SELL", "status": "FILLED"},
            {"order_id": "stop-2", "token_id": token_id, "side": "SELL", "status": "FILLED"},
        ],
        "trades": [
            {
                "trade_id": "buy-1",
                "token_id": token_id,
                "side": "BUY",
                "status": "CONFIRMED",
                "maker_orders": [
                    {
                        "order_id": "order-1",
                        "token_id": token_id,
                        "side": "BUY",
                        "matched_amount": Decimal("100"),
                        "price": Decimal("0.30"),
                        "fee": Decimal("0"),
                    }
                ],
            },
            {
                "trade_id": "sell-1",
                "token_id": token_id,
                "side": "SELL",
                "status": "CONFIRMED",
                "maker_orders": [
                    {
                        "order_id": "stop-1",
                        "token_id": token_id,
                        "side": "SELL",
                        "matched_amount": Decimal("40"),
                        "price": Decimal("0.25"),
                        "fee": Decimal("0"),
                    }
                ],
            },
            {
                "trade_id": "sell-2",
                "token_id": token_id,
                "side": "SELL",
                "status": "CONFIRMED",
                "maker_orders": [
                    {
                        "order_id": "stop-2",
                        "token_id": token_id,
                        "side": "SELL",
                        "matched_amount": Decimal("60"),
                        "price": Decimal("0.25"),
                        "fee": Decimal("0"),
                    }
                ],
            },
        ],
        "orders_terminal": True,
        "position_flat": True,
    }
    exchange.snapshot_value = final
    completed = service.tick()
    assert completed["state"] == "complete"
    assert completed["residual_quantity"] == Decimal("0")
    assert len(exchange.protected_sells) == 2
    assert len([item for item in exchange.limit_orders if item["side"] == "BUY"]) == 1


@pytest.mark.parametrize("mismatch", ["sale_first", "position_first"])
def test_protected_exit_waits_for_matching_trade_and_position(tmp_path, mismatch) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    token_id = "0x" + "1" * 64
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview({**_request(now), "quantity": Decimal("100")})
    started = service.start(str(preview["preview_id"]), "lp-protected-reconcile")

    entry = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("29"),
        entry_status="FILLED",
    )
    exchange.snapshot_value = entry
    service.tick()

    trigger = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("25"),
        passive_status="LIVE",
        bids=[{"price": Decimal("0.25"), "size": Decimal("100")}],
    )
    exchange.snapshot_value = trigger
    latched = service.tick()
    assert latched["stop_loss_latched"] is True

    cancelled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("10"),
        passive_status="CANCELED",
        bids=[{"price": Decimal("0.25"), "size": Decimal("40")}],
    )
    exchange.snapshot_value = cancelled
    first_exit = service.tick()
    assert first_exit["residual_quantity"] == Decimal("100")
    assert exchange.protected_sells == [
        {"token_id": token_id, "quantity": Decimal("40"), "min_price": Decimal("0.25")}
    ]

    if mismatch == "sale_first":
        mismatch_snapshot = _inventory_snapshot(
            now,
            buy_quantity=Decimal("100"),
            buy_cost=Decimal("30"),
            sold_quantity=Decimal("40"),
            sold_revenue=Decimal("10"),
            residual=Decimal("100"),
            residual_value=Decimal("25"),
            protected_status="FILLED",
            bids=[{"price": Decimal("0.25"), "size": Decimal("100")}],
        )
    else:
        mismatch_snapshot = _inventory_snapshot(
            now,
            buy_quantity=Decimal("100"),
            buy_cost=Decimal("30"),
            residual=Decimal("60"),
            residual_value=Decimal("15"),
            protected_status="FILLED",
            bids=[{"price": Decimal("0.25"), "size": Decimal("60")}],
        )
    exchange.snapshot_value = mismatch_snapshot
    blocked = service.tick()
    assert blocked["position_reconciled"] is False
    assert blocked["protected_exit_order_id"] == "stop-1"
    assert blocked["residual_quantity"] == (
        Decimal("100") if mismatch == "sale_first" else Decimal("60")
    )
    assert len(exchange.protected_sells) == 1
    repeated = service.tick()
    assert repeated["protected_exit_order_id"] == "stop-1"
    assert len(exchange.protected_sells) == 1

    matching = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        sold_quantity=Decimal("40"),
        sold_revenue=Decimal("10"),
        residual=Decimal("60"),
        residual_value=Decimal("15"),
        protected_status="FILLED",
        bids=[{"price": Decimal("0.25"), "size": Decimal("60")}],
    )
    exchange.snapshot_value = matching
    reconciled = service.tick()
    assert reconciled["position_reconciled"] is True
    assert reconciled["residual_quantity"] == Decimal("60")
    assert exchange.protected_sells == [
        {"token_id": token_id, "quantity": Decimal("40"), "min_price": Decimal("0.25")},
        {"token_id": token_id, "quantity": Decimal("60"), "min_price": Decimal("0.25")},
    ]
    assert "stop-1" in reconciled["order_history"]
    assert "stop-2" in reconciled["order_history"]

    final = {
        **matching,
        "account": {**matching["account"], "positions": []},  # type: ignore[dict-item]
        "book": {**matching["book"], "bids": []},  # type: ignore[dict-item]
        "orders": [
            {"order_id": "order-1", "token_id": token_id, "side": "BUY", "status": "FILLED"},
            {"order_id": "order-2", "token_id": token_id, "side": "SELL", "status": "CANCELED"},
            {"order_id": "stop-1", "token_id": token_id, "side": "SELL", "status": "FILLED"},
            {"order_id": "stop-2", "token_id": token_id, "side": "SELL", "status": "FILLED"},
        ],
        "trades": [
            {
                "trade_id": "buy-1",
                "token_id": token_id,
                "side": "BUY",
                "status": "CONFIRMED",
                "maker_orders": [
                    {
                        "order_id": "order-1",
                        "token_id": token_id,
                        "side": "BUY",
                        "matched_amount": Decimal("100"),
                        "price": Decimal("0.30"),
                        "fee": Decimal("0"),
                    }
                ],
            },
            {
                "trade_id": "sell-1",
                "token_id": token_id,
                "side": "SELL",
                "status": "CONFIRMED",
                "maker_orders": [
                    {
                        "order_id": "stop-1",
                        "token_id": token_id,
                        "side": "SELL",
                        "matched_amount": Decimal("40"),
                        "price": Decimal("0.25"),
                        "fee": Decimal("0"),
                    }
                ],
            },
            {
                "trade_id": "sell-2",
                "token_id": token_id,
                "side": "SELL",
                "status": "CONFIRMED",
                "maker_orders": [
                    {
                        "order_id": "stop-2",
                        "token_id": token_id,
                        "side": "SELL",
                        "matched_amount": Decimal("60"),
                        "price": Decimal("0.25"),
                        "fee": Decimal("0"),
                    }
                ],
            },
        ],
        "orders_terminal": True,
        "position_flat": True,
    }
    exchange.snapshot_value = final
    completed = service.tick()
    assert completed["state"] == "complete"
    assert len(exchange.protected_sells) == 2
    assert service.status(started["session_id"])["state"] == "complete"


@pytest.mark.parametrize("transition", ["stop", "deadline"])
def test_review_preserves_latched_exit_with_partial_depth(tmp_path, transition) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    current = [now]
    token_id = "0x" + "1" * 64
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current[0]
    )
    preview = service.preview({**_request(now), "quantity": Decimal("100")})
    started = service.start(str(preview["preview_id"]), "lp-review-latched")

    exchange.snapshot_value = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("29"),
    )
    service.tick()

    trigger = _inventory_snapshot(
        now,
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("25"),
        passive_status="LIVE",
        bids=[{"price": Decimal("0.25"), "size": Decimal("100")}],
    )
    exchange.snapshot_value = trigger
    latched = service.tick()
    assert latched["state"] == "stop_loss_exit"
    assert latched["stop_loss_latched"] is True

    if transition == "stop":
        reviewed = service.stop(started["session_id"])
        assert reviewed["state"] == "review"
    else:
        current[0] = now + timedelta(minutes=10)
        deadline = {
            **trigger,
            "orders": [
                {
                    "order_id": "order-1",
                    "status": "FILLED",
                    "token_id": token_id,
                    "side": "BUY",
                    "price": Decimal("0.30"),
                    "original_size": Decimal("100"),
                    "size_matched": Decimal("100"),
                },
                {
                    "order_id": "order-2",
                    "status": "CANCELED",
                    "token_id": token_id,
                    "side": "SELL",
                    "price": Decimal("0.29"),
                    "original_size": Decimal("100"),
                    "size_matched": Decimal("0"),
                    "remaining_size": Decimal("100"),
                },
            ],
            "book": {**trigger["book"], "received_at": current[0]},  # type: ignore[dict-item]
        }
        exchange.snapshot_value = deadline
        reviewed = service.tick()
        assert reviewed["state"] == "review"

    partial = _inventory_snapshot(
        current[0],
        buy_quantity=Decimal("100"),
        buy_cost=Decimal("30"),
        residual=Decimal("100"),
        residual_value=Decimal("0"),
        passive_status="CANCELED",
        bids=[{"price": Decimal("0.25"), "size": Decimal("40")}],
    )
    exchange.snapshot_value = partial
    exiting = service.tick()
    assert exiting["state"] == "stop_loss_exit"
    assert exiting["stop_loss_latched"] is True
    assert exiting["residual_exit_value"] is None
    assert exchange.protected_sells == [
        {"token_id": token_id, "quantity": Decimal("40"), "min_price": Decimal("0.25")}
    ]
    assert len([item for item in exchange.limit_orders if item["side"] == "BUY"]) == 1
    assert len([item for item in exchange.limit_orders if item["side"] == "SELL"]) == 1

    repeat = service.tick()
    assert repeat["stop_loss_latched"] is True
    assert len(exchange.protected_sells) == 1
    assert len([item for item in exchange.limit_orders if item["side"] == "SELL"]) == 1


@pytest.mark.parametrize("mode", ["direct", "wrapper"], ids=["direct", "wrapper"])
def test_cancel_rejection_keeps_owned_quote_actionable(tmp_path, mode) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    if mode == "direct":
        exchange.cancel_responses = [
            {"canceled": [], "not_canceled": {"order-1": "still_live"}},
            {"canceled": [], "not_canceled": {"order-1": "still_live"}},
            {"canceled": ["order-1"], "status": "CANCELED"},
        ]
    else:
        exchange.cancel_order = None  # type: ignore[method-assign]
        exchange.cancel_responses = [(), (), ("order-1",)]
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview(_request(now))
    started = service.start(str(preview["preview_id"]), "lp-cancel-retry")

    live = _inventory_snapshot(
        now,
        buy_quantity=Decimal("0"),
        buy_cost=Decimal("0"),
        residual=Decimal("0"),
        residual_value=Decimal("0"),
        entry_status="LIVE",
        orders_terminal=False,
        position_flat=True,
    )
    exchange.snapshot_value = live
    stopped = service.stop(started["session_id"])
    assert stopped["state"] == "needs_attention"
    assert exchange.cancels == ["order-1"]

    still_live = service.tick()
    assert still_live["state"] in {"needs_attention", "review"}
    assert still_live["state"] != "complete"
    assert exchange.cancels == ["order-1", "order-1"]

    exchange.snapshot_value = live
    acknowledged = service.tick()
    assert acknowledged["state"] != "complete"
    assert exchange.cancels == ["order-1", "order-1", "order-1"]

    canceled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("0"),
        buy_cost=Decimal("0"),
        residual=Decimal("0"),
        residual_value=Decimal("0"),
        entry_status="CANCELED",
        orders_terminal=True,
        position_flat=True,
    )
    exchange.snapshot_value = canceled
    completed = service.tick()
    assert completed["state"] == "complete"
    assert exchange.cancels == ["order-1", "order-1", "order-1"]
    assert "order-1" in completed["order_history"]
    assert completed["order_history"]["order-1"]["status"] == "CANCELED"



def test_unowned_target_order_blocks_active_session_writes(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    token_id = "0x" + "1" * 64
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview({**_request(now), "quantity": Decimal("15")})
    started = service.start(str(preview["preview_id"]), "lp-unowned-target")

    entry_pending = _inventory_snapshot(
        now,
        buy_quantity=Decimal("0"),
        buy_cost=Decimal("0"),
        residual=Decimal("0"),
        residual_value=Decimal("0"),
        entry_status="LIVE",
        orders_terminal=False,
        position_flat=True,
    )
    exchange.snapshot_value = entry_pending
    service.tick()

    foreign = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        entry_status="FILLED",
    )
    foreign["account"] = {
        **foreign["account"],  # type: ignore[dict-item]
        "open_orders": [
            {
                "order_id": "foreign-target-sell",
                "token_id": token_id,
                "side": "SELL",
                "status": "LIVE",
                "price": Decimal("0.31"),
                "remaining_size": Decimal("15"),
            },
            {
                "token_id": token_id,
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.30"),
                "remaining_size": Decimal("1"),
            },
        ],
    }
    exchange.snapshot_value = foreign
    blocked = service.tick()
    assert blocked["state"] == "needs_attention"
    assert blocked["residual_quantity"] == Decimal("15")
    assert blocked["reconciliation"] == "unowned_target_order"
    assert [item for item in exchange.limit_orders if item["side"] == "SELL"] == []
    assert exchange.protected_sells == []
    assert "foreign-target-sell" not in exchange.cancels

    blocked_again = service.tick()
    assert blocked_again["state"] == "needs_attention"
    assert blocked_again["residual_quantity"] == Decimal("15")
    assert [item for item in exchange.limit_orders if item["side"] == "SELL"] == []
    assert "foreign-target-sell" not in exchange.cancels

    resolved = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        entry_status="FILLED",
    )
    exchange.snapshot_value = resolved
    resumed = service.tick()
    assert resumed["state"] == "passive_exit"
    assert len([item for item in exchange.limit_orders if item["side"] == "SELL"]) == 1
    assert len([item for item in exchange.limit_orders if item["side"] == "BUY"]) == 1
    assert exchange.cancels == []



def test_scoring_uses_current_order_cadence_and_status_freshness(tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    current = [now]
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])
    preview = service.preview({**_request(now), "quantity": Decimal("15")})
    started = service.start(str(preview["preview_id"]), "lp-scoring-cadence")

    entry_live = _inventory_snapshot(
        now,
        buy_quantity=Decimal("0"),
        buy_cost=Decimal("0"),
        residual=Decimal("0"),
        residual_value=Decimal("0"),
        entry_status="LIVE",
        orders_terminal=False,
        position_flat=True,
    )
    entry_live.pop("scoring", None)
    exchange.snapshot_value = entry_live
    exchange.scoring_responses = [True]
    first = service.tick()
    assert first["scoring_status"] == "true"
    assert first["scoring_order_id"] == "order-1"
    checked_at = first["scoring_checked_at"]
    assert exchange.scoring_calls == ["order-1"]

    for second in range(1, 5):
        current[0] = now + timedelta(seconds=second)
        observed = service.tick()
        assert observed["scoring_status"] == "true"
        assert observed["scoring_checked_at"] == checked_at
        assert service.status(started["session_id"])["scoring_checked_at"] == checked_at
    assert exchange.scoring_calls == ["order-1"]

    current[0] = now + timedelta(seconds=5)
    filled = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        entry_status="FILLED",
    )
    exchange.snapshot_value = filled
    exchange.scoring_responses = [True, False]
    transitioned = service.tick()
    assert transitioned["state"] == "passive_exit"
    assert transitioned["scoring_status"] == "unknown"
    assert transitioned["scoring_order_id"] == "order-2"
    assert transitioned["scoring_order_role"] == "passive_exit"
    assert transitioned["scoring_checked_at"] is None
    assert len([item for item in exchange.limit_orders if item["side"] == "SELL"]) == 1

    current[0] = now + timedelta(seconds=6)
    passive = _inventory_snapshot(
        now,
        buy_quantity=Decimal("15"),
        buy_cost=Decimal("4.50"),
        residual=Decimal("15"),
        residual_value=Decimal("4.35"),
        passive_status="LIVE",
        orders_terminal=False,
    )
    exchange.snapshot_value = passive
    next_due = service.tick()
    assert next_due["state"] == "passive_exit"
    assert next_due["scoring_status"] == "false"
    assert next_due["scoring_order_id"] == "order-2"
    assert exchange.scoring_calls == ["order-1", "order-1", "order-2"]

    current[0] = now + timedelta(seconds=22)
    old = service.status(started["session_id"])
    assert old["scoring_status"] == "unknown"
    assert old["scoring_checked_at"] == next_due["scoring_checked_at"]
    assert exchange.scoring_calls == ["order-1", "order-1", "order-2"]

    current[0] = now + timedelta(seconds=27)
    exchange.snapshot_value = {
        **passive,
        "book": {**passive["book"], "timestamp": current[0]},  # type: ignore[dict-item]
    }
    exchange.snapshot_value["book"]["received_at"] = current[0]  # type: ignore[index]
    exchange.scoring_responses = [TimeoutError("score timeout")]
    after_error = service.tick()
    assert after_error["scoring_status"] == "unknown"
    assert after_error["residual_quantity"] == Decimal("15")
    assert after_error["state"] == "passive_exit"
    assert exchange.scoring_calls[-1] == "order-2"


class _SDKAccountClient:
    def __init__(self, now: datetime) -> None:
        address = "0x3333333333333333333333333333333333333333"
        condition_id = "0x" + "c" * 64
        token_id = "0x" + "1" * 64
        self.environment = SimpleNamespace(
            standard_exchange=address
        )
        self.now = now
        self.scoring_calls = 0
        maker_order = MakerOrder(
            order_id="order-1",
            asset_id=token_id,
            maker_address=address,
            owner=address,
            side="BUY",
            price=Decimal("0.30"),
            matched_amount=Decimal("100"),
            outcome="YES",
            fee_rate_bps=Decimal("0"),
        )
        self.trade = ClobTrade(
            id="trade-1",
            market=condition_id,
            asset_id=token_id,
            owner=address,
            maker_address=address,
            taker_order_id="taker-1",
            side="BUY",
            trader_side="MAKER",
            price=Decimal("0.30"),
            size=Decimal("100"),
            outcome="YES",
            status="CONFIRMED",
            fee_rate_bps=Decimal("0"),
            bucket_index=0,
            transaction_hash="0xtrade",
            maker_orders=(maker_order,),
            match_time=now,
            last_update=now,
        )
        self.open_order = OpenOrder(
            id="order-open",
            market=condition_id,
            asset_id=token_id,
            owner=address,
            maker_address=address,
            side="SELL",
            price=Decimal("0.31"),
            original_size=Decimal("100"),
            size_matched=Decimal("25"),
            outcome="YES",
            order_type="GTD",
            status="LIVE",
            associate_trades=("trade-1",),
            created_at=now,
            expiration=now + timedelta(minutes=10),
        )

    def get_balance_allowance(self, **_kwargs: object) -> object:
        return SimpleNamespace(
            balance="100000000",
            allowances={self.environment.standard_exchange: "100000000"},
        )

    def list_open_orders(self, **_kwargs: object) -> list[object]:
        return [self.open_order]

    def list_account_trades(self, **_kwargs: object) -> list[object]:
        return [self.trade]

    def list_positions(self, **_kwargs: object) -> list[object]:
        return []

    def get_order_scoring(self, *, order_id: str) -> bool:
        self.scoring_calls += 1
        del order_id
        return True


class _SDKPublicClient:
    def __init__(self, now: datetime, *, source_timestamp: datetime | None = None) -> None:
        self.now = now
        self.source_timestamp = source_timestamp or now
        self.scoring_calls = 0

    def get_market(self, *, id: str) -> object:
        assert id == "market-1"
        condition_id = "0x" + "c" * 64
        token_id = "0x" + "1" * 64
        return Market(
            id="market-1",
            condition_id=condition_id,
            state=MarketState(
                active=True,
                closed=False,
                acceptingOrders=True,
                enableOrderBook=True,
            ),
            outcomes=MarketOutcomes(
                yes=MarketOutcome(label="Yes", tokenId=token_id),
                no=MarketOutcome(label="No", tokenId="0x" + "2" * 64),
            ),
            metrics=MarketMetrics(),
            prices=MarketPrices(),
            trading=MarketTrading(
                minimumOrderSize=Decimal("1"),
                minimumTickSize=Decimal("0.001"),
                feesEnabled=True,
                feeSchedule=FeeSchedule(
                    exponent=1,
                    rate=Decimal("0.009558"),
                    takerOnly=True,
                    rebateRate=Decimal("0"),
                ),
            ),
            resolution=MarketResolution(source="UMA"),
            rewards=MarketRewards(
                rewardsMinSize=Decimal("1"), rewardsMaxSpread=10
            ),
            sports=MarketSportsMetadata(),
            events=(),
            tags=(),
        )

    def get_order_book(self, *, token_id: str) -> object:
        assert token_id == "0x" + "1" * 64
        return OrderBook(
            market="0x" + "c" * 64,
            asset_id="0x" + "1" * 64,
            timestamp=self.source_timestamp,
            bids=(OrderBookLevel(price=Decimal("0.29"), size=Decimal("100")),),
            asks=(OrderBookLevel(price=Decimal("0.31"), size=Decimal("100")),),
            min_order_size=Decimal("1"),
            tick_size=Decimal("0.001"),
            neg_risk=False,
            hash="book-hash",
        )

    def get_order_scoring(self, *, order_id: str) -> bool:
        self.scoring_calls += 1
        del order_id
        return True

    def close(self) -> None:
        return None


def test_production_adapter_normalizes_sdk_account_market_book_and_trades() -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    account_client = _SDKAccountClient(now)
    public_client = _SDKPublicClient(now)
    adapter = PolymarketTradingClient(
        TradingConfig(
            "0x1111111111111111111111111111111111111111",
            "0x2222222222222222222222222222222222222222",
        ),
        account_client,
        public_client_factory=lambda: public_client,
    )

    snapshot = adapter.lp_snapshot(
        {
            "market_id": "market-1",
            "condition_id": "0x" + "c" * 64,
            "token_id": "0x" + "1" * 64,
            "outcome": "YES",
            "entry_order_id": "order-1",
        }
    )

    assert account_client.scoring_calls == 0
    assert snapshot["market"] == {
        "market_id": "market-1",
        "condition_id": "0x" + "c" * 64,
        "token_id": "0x" + "1" * 64,
        "outcome": "YES",
        "accepting_orders": True,
        "exchange_type": "CLOB",
        "tick_size": Decimal("0.001"),
        "minimum_order_size": Decimal("1"),
        "fee": Decimal("0"),
        "fees_enabled": True,
        "fee_exponent": Decimal("1"),
        "taker_fee_rate": Decimal("0.009558"),
        "reward_min_size": Decimal("1"),
        "reward_max_spread": Decimal("0.10"),
    }
    assert snapshot["account"]["authenticated"] is True  # type: ignore[index]
    assert snapshot["account"]["open_orders"] == [  # type: ignore[index]
        {
            "id": "order-open",
            "order_id": "order-open",
            "market": "0x" + "c" * 64,
            "condition_id": "0x" + "c" * 64,
            "market_id": None,
            "market_title": None,
            "market_url": None,
            "token_id": "0x" + "1" * 64,
            "asset_id": "0x" + "1" * 64,
            "side": "SELL",
            "price": Decimal("0.31"),
            "original_size": Decimal("100"),
            "size_matched": Decimal("25"),
            "remaining_size": Decimal("75"),
            "size": Decimal("75"),
            "outcome": "YES",
            "order_type": "GTD",
            "status": "LIVE",
            "expiration": now + timedelta(minutes=10),
            "created_at": now,
        }
    ]
    assert snapshot["book"]["bids"] == [  # type: ignore[index]
        {"price": Decimal("0.29"), "size": Decimal("100")}
    ]
    assert snapshot["book"]["timestamp"] == now  # type: ignore[index]
    assert snapshot["trades"][0]["trade_id"] == "trade-1"  # type: ignore[index]
    assert snapshot["trades"][0]["status"] == "CONFIRMED"  # type: ignore[index]
    assert snapshot["trades"][0]["maker_orders"][0]["order_id"] == "order-1"  # type: ignore[index]
    assert snapshot["trades"][0]["price"] == Decimal("0.30")  # type: ignore[index]
    assert snapshot["trades"][0]["size"] == Decimal("100")  # type: ignore[index]
    assert snapshot["trades"][0]["matched_at"] == now  # type: ignore[index]
    assert snapshot["trades"][0]["updated_at"] == now  # type: ignore[index]

class _NoOpenOrdersSDKAccountClient(_SDKAccountClient):
    def list_open_orders(self, **_kwargs: object) -> list[object]:
        return []


def test_fresh_local_receipt_accepts_unchanged_source_book(tmp_path) -> None:
    before = datetime.now(UTC)
    source_timestamp = before - timedelta(minutes=10)
    account_client = _NoOpenOrdersSDKAccountClient(source_timestamp)
    adapter = PolymarketTradingClient(
        TradingConfig(
            "0x1111111111111111111111111111111111111111",
            "0x2222222222222222222222222222222222222222",
        ),
        account_client,
        public_client_factory=lambda: _SDKPublicClient(
            source_timestamp, source_timestamp=source_timestamp
        ),
    )
    request = {
        "market_id": "market-1",
        "condition_id": "0x" + "c" * 64,
        "token_id": "0x" + "1" * 64,
        "outcome": "YES",
    }
    snapshot = adapter.lp_snapshot(request)
    after = datetime.now(UTC)
    book = snapshot["book"]
    assert book["timestamp"] == source_timestamp  # type: ignore[index]
    assert before <= book["received_at"] <= after  # type: ignore[index]

    now = datetime.now(UTC)
    service_request = {
        **request,
        "question": "Will it happen?",
        "price": Decimal("0.30"),
        "quantity": Decimal("10"),
        "review_at": now + timedelta(minutes=10),
    }
    fresh_exchange = _Exchange()
    fresh_exchange.snapshot_value = snapshot
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path),
        fresh_exchange,
        clock=lambda: datetime.now(UTC),
    )
    preview = service.preview(service_request)
    assert preview["state"] == "previewed"
    started = service.start(str(preview["preview_id"]), "lp-local-receipt")
    assert started["state"] == "entry_open"

    stale = {
        **snapshot,
        "book": {
            **snapshot["book"],  # type: ignore[dict-item]
            "received_at": datetime.now(UTC) - timedelta(seconds=30),
        },
    }
    stale_exchange = _Exchange()
    stale_exchange.snapshot_value = stale
    stale_service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "stale"),
        stale_exchange,
        clock=lambda: datetime.now(UTC),
    )
    assert stale_service.preview(service_request)["reason"] == "book_freshness_stale"

    missing = {
        **snapshot,
        "book": {key: value for key, value in snapshot["book"].items() if key != "received_at"},  # type: ignore[union-attr]
    }
    missing_exchange = _Exchange()
    missing_exchange.snapshot_value = missing
    missing_service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "missing"),
        missing_exchange,
        clock=lambda: datetime.now(UTC),
    )
    assert missing_service.preview(service_request)["reason"] == "book_freshness_unknown"


def test_refresh_candidates_projects_trial_funnel_without_risk(tmp_path) -> None:
    """A8: 候选漏斗为 读取/基础筛选/排序/待测候选 四阶段，风控阶段移除。"""

    now = datetime(2026, 9, 17, 1, tzinfo=UTC)

    class Exchange:
        def __init__(self) -> None:
            self.competition_reads = 0
            self.book_token_reads: tuple[tuple[str, ...], ...] = ()

        def lp_reward_catalog(
            self, *, condition_ids=None, stop_event=None
        ) -> dict[str, object]:
            del condition_ids, stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": now,
                "markets": (
                    {
                        "condition_id": "condition-A",
                        "daily_pool_usd": Decimal("120"),
                        "reward_active": True,
                    },
                    {
                        "condition_id": "condition-B",
                        "daily_pool_usd": Decimal("90"),
                        "reward_active": True,
                    },
                    {
                        "condition_id": "condition-Z",
                        "daily_pool_usd": Decimal("500"),
                        "reward_active": True,
                    },
                ),
            }

        def lp_market_metadata(
            self, condition_ids, *, stop_event=None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            sizes = {"condition-A": "20", "condition-B": "20", "condition-Z": "5000"}
            return {
                condition_id: {
                    "market_id": f"market-{condition_id.removeprefix('condition-')}",
                    "condition_id": condition_id,
                    "market_title": f"Market {condition_id}",
                    "market_url": f"https://polymarket.com/event/{condition_id}",
                    "accepting_orders": True,
                    "metadata_checked_at": now,
                    "fees_checked_at": now,
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal(sizes[condition_id]),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "fee": Decimal("0"),
                    "outcomes": {
                        "yes": {
                            "label": "YES",
                            "token_id": f"token-{condition_id}",
                        }
                    },
                }
                for condition_id in condition_ids
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("1000"),
                "allowance": Decimal("1000"),
                "open_orders": [],
                "positions": [],
                "checked_at": now,
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(self, token_ids, *, stop_event=None):
            del stop_event
            self.book_token_reads = (*self.book_token_reads, tuple(token_ids))
            return {
                token_id: {
                    "condition_id": token_id.removeprefix("token-"),
                    "token_id": token_id,
                    "received_at": now,
                    "bids": [
                        {"price": Decimal("0.34"), "size": Decimal("100")},
                        {"price": Decimal("0.33"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.36"), "size": Decimal("100")}],
                }
                for token_id in token_ids
            }

        def lp_market_competitiveness(
            self, *, stop_event=None, previous=None
        ) -> dict[str, object]:
            del stop_event, previous
            self.competition_reads += 1
            return {
                "state": "known",
                "complete": True,
                "checked_at": now,
                "round_checked_at": now,
                "competitiveness": {
                    "condition-A": (Decimal("0.12"), now),
                    "condition-B": (Decimal("0"), now),
                    "condition-Z": (Decimal("5"), now),
                },
                "not_updated": [],
            }

    def lp_price_history(
        self, token_ids, *, start_ts, end_ts, fidelity=1, stop_event=None
    ):
        del fidelity, stop_event
        return {
            "state": "known",
            "history": {
                token_id: [
                    {"t": start_ts, "p": "0.500"},
                    {"t": end_ts, "p": "0.505"},
                ]
                for token_id in token_ids
            },
            "unknown_token_ids": [],
        }

    Exchange.lp_price_history = lp_price_history  # type: ignore[attr-defined]

    store = PredictionArbitrageStore(tmp_path)
    exchange = Exchange()
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    prepared = lp.refresh_price_history()
    assert prepared["state"] == "known"
    # Issue #157: competition refreshes on its own thread; the candidate
    # batch path only reads the cache, so the explicit-zero exclusion needs
    # the cache warmed first.
    assert lp.refresh_competition_cache()["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    funnel = snapshot["funnel"]
    assert {"read", "base", "sort", "trial"} <= set(funnel)
    assert "risk" not in funnel
    assert "risk_directions" not in funnel
    assert funnel["read"] == 3
    assert funnel["base"] == 3
    # condition-B carries explicit zero competition: dropped before ranking.
    assert funnel["sort"] == 2
    assert funnel["excluded"]["competition_empty"] == 1
    # condition-Z needs ~5000 × 0.505 capital over the 1000 available.
    assert funnel["excluded"]["over_available"] == 1
    assert funnel["trial"] == 1
    assert funnel["compared_range"] == {
        "compared": 3,
        "total": 3,
        "pending": 0,
    }
    candidates = snapshot["candidates"]
    assert [row["condition_id"] for row in candidates] == ["condition-A"]
    row = candidates[0]
    assert row["min_quantity"] == "20"
    assert row["reference_capital"] == "10.100"  # 20.00 × 0.505 exact
    assert row["competition"]["value"] == "0.12"
    assert row["competition"]["state"] == "known"
    # The selected candidate's live book is read once and merged in-place.
    assert exchange.book_token_reads == (("token-condition-A",),)
    assert row["realtime_price"] == "0.34"
    assert row["realtime_capital"] == "6.80"
    assert len(snapshot["recommendations"]) == 1
    assert snapshot["recommendations"][0]["selected_direction"]["outcome"] == "YES"
    assert snapshot["selected_market_ids"] == ["market-A"]
    # Only the pre-warm read: batches never call the competition reader.
    assert exchange.competition_reads == 1


def test_refresh_candidates_drops_rows_whose_realtime_capital_over_available(tmp_path) -> None:
    """A market whose live capital exceeds available funds is not displayed.

    Issue 143: only live-qualified passers are published, so a head rejected
    at the risk gate for exceeding the reserved available capital stays out
    of the table entirely (counted in the scan funnel instead).  Reference
    capital 20 × 0.505 = 10.100 fits the 11 available, so market A forms the
    normal queue and its live book is read; the realtime bid 0.60 lifts
    capital to 12.00 which exceeds available, so the gate rejects it.
    condition-Z (reference capital 2525) never enters a queue.
    """

    now = datetime(2026, 9, 17, 2, tzinfo=UTC)

    class Exchange:
        def __init__(self) -> None:
            self.book_token_reads: tuple[tuple[str, ...], ...] = ()

        def lp_reward_catalog(
            self, *, condition_ids=None, stop_event=None
        ) -> dict[str, object]:
            del condition_ids, stop_event
            return {
                "state": "known",
                "complete": True,
                "checked_at": now,
                "markets": (
                    {
                        "condition_id": "condition-A",
                        "daily_pool_usd": Decimal("120"),
                        "reward_active": True,
                    },
                    {
                        "condition_id": "condition-Z",
                        "daily_pool_usd": Decimal("500"),
                        "reward_active": True,
                    },
                ),
            }

        def lp_market_metadata(
            self, condition_ids, *, stop_event=None
        ) -> dict[str, dict[str, object]]:
            del stop_event
            sizes = {"condition-A": "20", "condition-Z": "5000"}
            return {
                condition_id: {
                    "market_id": f"market-{condition_id.removeprefix('condition-')}",
                    "condition_id": condition_id,
                    "market_title": f"Market {condition_id}",
                    "market_url": f"https://polymarket.com/event/{condition_id}",
                    "accepting_orders": True,
                    "metadata_checked_at": now,
                    "fees_checked_at": now,
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal(sizes[condition_id]),
                    "reward_min_size": Decimal("20"),
                    "reward_max_spread": Decimal("0.10"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "fee": Decimal("0"),
                    "outcomes": {
                        "yes": {
                            "label": "YES",
                            "token_id": f"token-{condition_id}",
                        }
                    },
                }
                for condition_id in condition_ids
            }

        def lp_account_snapshot(self) -> dict[str, object]:
            return {
                "authenticated": True,
                "balance": Decimal("11"),
                "allowance": Decimal("11"),
                "open_orders": [],
                "positions": [],
                "checked_at": now,
                "open_orders_complete": True,
                "positions_complete": True,
            }

        def lp_order_books(self, token_ids, *, stop_event=None):
            del stop_event
            self.book_token_reads = (*self.book_token_reads, tuple(token_ids))
            return {
                token_id: {
                    "condition_id": token_id.removeprefix("token-"),
                    "token_id": token_id,
                    "received_at": now,
                    "bids": [
                        {"price": Decimal("0.60"), "size": Decimal("100")},
                        {"price": Decimal("0.59"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.62"), "size": Decimal("100")}],
                }
                for token_id in token_ids
            }

        def lp_market_competitiveness(
            self, *, stop_event=None, previous=None
        ) -> dict[str, object]:
            del stop_event, previous
            return {
                "state": "known",
                "complete": True,
                "checked_at": now,
                "round_checked_at": now,
                "competitiveness": {
                    "condition-A": (Decimal("0.12"), now),
                    "condition-Z": (Decimal("5"), now),
                },
                "not_updated": [],
            }

    def lp_price_history(
        self, token_ids, *, start_ts, end_ts, fidelity=1, stop_event=None
    ):
        del fidelity, stop_event
        return {
            "state": "known",
            "history": {
                token_id: [
                    {"t": start_ts, "p": "0.500"},
                    {"t": end_ts, "p": "0.505"},
                ]
                for token_id in token_ids
            },
            "unknown_token_ids": [],
        }

    Exchange.lp_price_history = lp_price_history  # type: ignore[attr-defined]

    store = PredictionArbitrageStore(tmp_path)
    exchange = Exchange()
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    prepared = lp.refresh_price_history()
    assert prepared["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    # The rejected market's live book was read once and never retried.
    assert exchange.book_token_reads == (("token-condition-A",),)
    funnel = snapshot["funnel"]
    # condition-Z exceeds available at the reference stage and enters no
    # queue; market A is checked, rejected by the risk gate, and therefore
    # not published at all under the passers-only table.
    assert funnel["excluded"]["over_available"] == 1
    assert funnel["normal_queue_count"] == 1
    assert funnel["backup_queue_count"] == 0
    assert funnel["reference_price_unknown"] == 0
    # Passers-only publication: the trial stage counts published passers,
    # so a batch whose only market was rejected reports 0, matching the
    # empty table and the status line.
    assert funnel["trial"] == 0
    assert funnel["checked"] == 1
    assert funnel["passed"] == 0
    assert funnel["rejected"] == 1
    assert funnel["unknown"] == 0
    assert funnel["unchecked"] == 0
    assert snapshot["candidates"] == []
    assert snapshot["recommendations"] == []
    assert snapshot["selected_results"] == []
    assert snapshot["selected_market_ids"] == []
    # Issue #157: the rolling pool has no per-round shortfall reason.
    assert funnel["gap_reason"] is None


class _LPCandidateQueryExchange:
    """Fake exchange for the issue-141 query-queue candidate refresh.

    Every market qualifies: minimum order size 20, a fresh 0.505 summary
    (prepared via refresh_price_history), and a fresh non-zero competition
    value.  `pools` maps market suffixes to daily pools; distinct pools give
    distinct assumed upper bounds, so suffix M01 (largest pool) is the
    batch head.
    """

    def __init__(
        self,
        now: datetime,
        pools: dict[str, Decimal],
        *,
        available: str = "1000",
        book_bid: str = "0.34",
        min_sizes: dict[str, str] | None = None,
    ) -> None:
        self.now = now
        self.pools = pools
        self.available = Decimal(available)
        self.book_bid = Decimal(book_bid)
        self.min_sizes = min_sizes or {}
        self.competition_reads = 0
        self.history_calls = 0
        self.book_token_reads: tuple[tuple[str, ...], ...] = ()

    def lp_reward_catalog(
        self, *, condition_ids=None, stop_event=None
    ) -> dict[str, object]:
        del condition_ids, stop_event
        return {
            "state": "known",
            "complete": True,
            "checked_at": self.now,
            "markets": tuple(
                {
                    "condition_id": f"condition-{suffix}",
                    "daily_pool_usd": pool,
                    "reward_active": True,
                }
                for suffix, pool in self.pools.items()
            ),
        }

    def lp_market_metadata(
        self, condition_ids, *, stop_event=None
    ) -> dict[str, dict[str, object]]:
        del stop_event
        return {
            condition_id: {
                "market_id": f"market-{condition_id.removeprefix('condition-')}",
                "condition_id": condition_id,
                "market_title": f"Market {condition_id}",
                "market_url": f"https://polymarket.com/event/{condition_id}",
                "accepting_orders": True,
                "metadata_checked_at": self.now,
                "fees_checked_at": self.now,
                "tick_size": Decimal("0.01"),
                "minimum_order_size": Decimal(
                    self.min_sizes.get(
                        condition_id.removeprefix("condition-"), "20"
                    )
                ),
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "fee": Decimal("0"),
                "outcomes": {
                    "yes": {
                        "label": "YES",
                        "token_id": f"token-{condition_id}",
                    }
                },
            }
            for condition_id in condition_ids
        }

    def lp_account_snapshot(self) -> dict[str, object]:
        return {
            "authenticated": True,
            "balance": self.available,
            "allowance": self.available,
            "open_orders": [],
            "positions": [],
            "checked_at": self.now,
            "open_orders_complete": True,
            "positions_complete": True,
        }

    def lp_order_books(self, token_ids, *, stop_event=None):
        del stop_event
        self.book_token_reads = (*self.book_token_reads, tuple(token_ids))
        return {
            token_id: {
                "condition_id": token_id.removeprefix("token-"),
                "token_id": token_id,
                "received_at": self.now,
                "bids": [
                    {"price": self.book_bid, "size": Decimal("100")},
                    {
                        "price": self.book_bid - Decimal("0.01"),
                        "size": Decimal("100"),
                    },
                ],
                "asks": [
                    {
                        "price": self.book_bid + Decimal("0.02"),
                        "size": Decimal("100"),
                    }
                ],
            }
            for token_id in token_ids
        }

    def lp_market_competitiveness(
        self, *, stop_event=None, previous=None
    ) -> dict[str, object]:
        del stop_event, previous
        self.competition_reads += 1
        return {
            "state": "known",
            "complete": True,
            "checked_at": self.now,
            "round_checked_at": self.now,
            "competitiveness": {
                f"condition-{suffix}": (
                    Decimal(index) + Decimal("1"),
                    self.now,
                )
                for index, suffix in enumerate(self.pools)
            },
            "not_updated": [],
        }

    def lp_price_history(
        self, token_ids, *, start_ts, end_ts, fidelity=1, stop_event=None
    ):
        del fidelity, stop_event
        self.history_calls += 1
        return {
            "state": "known",
            "history": {
                token_id: [
                    {"t": start_ts, "p": "0.500"},
                    {"t": end_ts, "p": "0.505"},
                ]
                for token_id in token_ids
            },
            "unknown_token_ids": [],
        }


def test_batch_refresh_reads_books_per_batch_of_ten_markets(tmp_path) -> None:
    """Issue 143 + #157: twelve qualifying markets roll through two batches.

    The first call reads ten markets (ten single-outcome tokens) in one
    call; the second call reads the remaining two; no token is requested
    twice, and the published table follows the rolling pool.
    """

    now = datetime(2026, 9, 17, 3, tzinfo=UTC)
    current = {"now": now}
    exchange = _LPCandidateQueryExchange(
        now, {f"M{index:02d}": Decimal(200 - index) for index in range(1, 13)}
    )
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    prepared = lp.refresh_price_history()
    assert prepared["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    candidates = snapshot["candidates"]
    assert len(candidates) == 10
    head = candidates[0]
    assert head["market_id"] == "market-M01"
    assert head["verification"] == "verified"
    assert "realtime_price" in head
    assert "realtime_capital" in head
    for row in candidates:
        assert row["verification"] == "verified"
        assert "realtime_price" in row
        assert "realtime_capital" in row
    # The first batch covers the first ten queued markets in one call.
    assert len(exchange.book_token_reads) == 1
    assert len(exchange.book_token_reads[0]) == 10
    funnel = snapshot["funnel"]
    assert funnel["checked"] == 10
    assert funnel["passed"] == 10
    assert funnel["batches"] == 1
    assert snapshot["candidate_pending_count"] == 2

    # The second call processes the two remaining never-tried markets plus
    # the oldest-tried rotation tail (issue #157 rotation order).
    current["now"] = now + timedelta(seconds=5)
    exchange.now = current["now"]
    second = lp.refresh_candidates(force=True)
    assert len(exchange.book_token_reads) == 2
    second_tokens = exchange.book_token_reads[1]
    assert len(second_tokens) == 10
    assert {
        "token-condition-M11",
        "token-condition-M12",
    } <= set(second_tokens)
    funnel = second["funnel"]
    assert funnel["checked"] == 20
    assert funnel["passed"] == 20
    assert funnel["rejected"] == 0
    assert funnel["unknown"] == 0
    assert funnel["unchecked"] == 0
    assert funnel["batches"] == 2
    assert funnel["backup_read"] == 0
    assert funnel["trial"] == 12
    assert funnel["normal_queue_count"] == 12
    assert funnel["backup_queue_count"] == 0
    assert funnel["reference_price_unknown"] == 0
    assert second["candidate_valid_count"] == 12
    assert second["candidate_pending_count"] == 0


class _LPBatchQueryExchange(_LPCandidateQueryExchange):
    """Two-outcome batch fixture for the issue-143 batch backfill scans.

    Extends the query-queue exchange with YES/NO token pairs.  `backup`
    suffixes get an unknown history summary (no reference price, so the
    market queues as backup); `reject` suffixes get an off-tick live book so
    both directions reject at the candidate risk gate; `omit` token ids are
    left out of the book response entirely.
    """

    def __init__(
        self,
        now: datetime,
        pools: dict[str, Decimal],
        *,
        backup: frozenset[str] = frozenset(),
        reject: frozenset[str] = frozenset(),
        omit_tokens: frozenset[str] = frozenset(),
        bid_by_suffix: dict[str, Decimal] | None = None,
        available: str = "1000",
    ) -> None:
        super().__init__(now, pools, available=available)
        self.backup = backup
        self.reject = reject
        self.omit_tokens = omit_tokens
        self.bid_by_suffix = bid_by_suffix or {}

    @staticmethod
    def _suffix(token_id: str) -> str:
        return token_id.removeprefix("token-condition-").rsplit("-", 1)[0]

    def lp_market_metadata(self, condition_ids, *, stop_event=None):
        del stop_event
        return {
            condition_id: {
                "market_id": f"market-{condition_id.removeprefix('condition-')}",
                "condition_id": condition_id,
                "market_title": f"Market {condition_id}",
                "market_url": f"https://polymarket.com/event/{condition_id}",
                "accepting_orders": True,
                "exchange_type": "CLOB",
                "metadata_checked_at": self.now,
                "fees_checked_at": self.now,
                "tick_size": Decimal("0.01"),
                "minimum_order_size": Decimal("20"),
                "reward_min_size": Decimal("20"),
                "reward_max_spread": Decimal("0.10"),
                "fees_enabled": False,
                "taker_fee_rate": Decimal("0"),
                "fee_exponent": Decimal("1"),
                "fee": Decimal("0"),
                "outcomes": {
                    "yes": {
                        "label": "YES",
                        "token_id": f"token-{condition_id}-yes",
                    },
                    "no": {
                        "label": "NO",
                        "token_id": f"token-{condition_id}-no",
                    },
                },
            }
            for condition_id in condition_ids
        }

    def lp_price_history(
        self, token_ids, *, start_ts, end_ts, fidelity=1, stop_event=None
    ):
        del fidelity, stop_event
        history = {}
        unknown = []
        for token_id in tuple(token_ids):
            if self._suffix(token_id) in self.backup:
                unknown.append(token_id)
                continue
            history[token_id] = [
                {"t": start_ts, "p": "0.500"},
                {"t": end_ts, "p": "0.505"},
            ]
        return {"state": "known", "history": history, "unknown_token_ids": unknown}

    def lp_order_books(self, token_ids, *, stop_event=None):
        del stop_event
        self.book_token_reads = (*self.book_token_reads, tuple(token_ids))
        books = {}
        for token_id in tuple(token_ids):
            if token_id in self.omit_tokens:
                continue
            suffix = self._suffix(token_id)
            if suffix in self.reject:
                best = Decimal("0.345")
            elif suffix in self.bid_by_suffix:
                best = self.bid_by_suffix[suffix]
            else:
                best = Decimal("0.34")
            books[token_id] = {
                "condition_id": token_id.removeprefix("token-").rsplit("-", 1)[0],
                "token_id": token_id,
                "received_at": self.now,
                "bids": [
                    {"price": best, "size": Decimal("100")},
                    {"price": best - Decimal("0.01"), "size": Decimal("100")},
                ],
                "asks": [{"price": best + Decimal("0.02"), "size": Decimal("100")}],
            }
        return books


class _LPAdvancingClockExchange(_LPBatchQueryExchange):
    """Advancing-clock batch fixture for the issue-143 repair-2 tests.

    The shared clock jumps forward ``step_seconds`` after every
    ``lp_order_books`` call, so batch N is evaluated ``step_seconds * N``
    seconds after the round-start facts were built — the real scan reads a
    ten-thousand-market catalog plus five batches of books and evaluation
    and crosses the 60-second fact window (production saw every late batch
    die as ``market_metadata_stale``).  Stamp deltas let a test choose which
    fact class is already stale when the scan starts (prepared-cache ageing),
    and the renewal adapters serve fresh stamps at the current clock while
    recording every call so the once-per-batch targeted renewal semantics is
    assertable.
    """

    def __init__(
        self,
        now: datetime,
        pools: dict[str, Decimal],
        *,
        step_seconds: int = 30,
        metadata_stamp_delta: timedelta = timedelta(0),
        reward_stamp_delta: timedelta = timedelta(0),
        account_stamp_delta: timedelta = timedelta(0),
        metadata_fresh_mode: str = "fresh",
        refreshed_reward_override: dict[str, object] | None = None,
        open_orders: tuple[dict[str, object], ...] = (),
        backup: frozenset[str] = frozenset(),
        reject: frozenset[str] = frozenset(),
        omit_tokens: frozenset[str] = frozenset(),
        bid_by_suffix: dict[str, Decimal] | None = None,
        available: str = "1000",
    ) -> None:
        super().__init__(
            now,
            pools,
            backup=backup,
            reject=reject,
            omit_tokens=omit_tokens,
            bid_by_suffix=bid_by_suffix,
            available=available,
        )
        self.step = timedelta(seconds=step_seconds)
        self.metadata_stamp_delta = metadata_stamp_delta
        self.reward_stamp_delta = reward_stamp_delta
        self.account_stamp_delta = account_stamp_delta
        self.metadata_fresh_mode = metadata_fresh_mode
        self.refreshed_reward_override = refreshed_reward_override or {}
        self.open_orders = open_orders
        self.metadata_fresh_calls = 0
        self.metadata_fresh_reads: tuple[tuple[str, ...], ...] = ()
        self.targeted_reward_reads: tuple[tuple[str, ...], ...] = ()
        self.account_reads = 0

    def lp_market_metadata(self, condition_ids, *, stop_event=None):
        metadata = super().lp_market_metadata(condition_ids, stop_event=stop_event)
        stamp = self.now + self.metadata_stamp_delta
        return {
            condition_id: {
                **dict(row),
                "metadata_checked_at": stamp,
                "fees_checked_at": stamp,
            }
            for condition_id, row in metadata.items()
        }

    def lp_market_metadata_fresh(self, condition_ids, *, stop_event=None):
        self.metadata_fresh_calls += 1
        self.metadata_fresh_reads = (
            *self.metadata_fresh_reads,
            tuple(dict.fromkeys(condition_ids)),
        )
        if self.metadata_fresh_mode == "failure":
            raise RuntimeError("metadata_refresh_failed")
        if self.metadata_fresh_mode == "missing":
            return {}
        return self.lp_market_metadata(condition_ids, stop_event=stop_event)

    def lp_reward_catalog(self, *, condition_ids=None, stop_event=None):
        catalog = super().lp_reward_catalog(
            condition_ids=condition_ids, stop_event=stop_event
        )
        stamp = self.now + self.reward_stamp_delta
        catalog["checked_at"] = stamp
        catalog["markets"] = tuple(
            {**dict(row), "reward_checked_at": stamp} for row in catalog["markets"]
        )
        if condition_ids is not None:
            self.targeted_reward_reads = (
                *self.targeted_reward_reads,
                tuple(dict.fromkeys(condition_ids)),
            )
            if self.refreshed_reward_override:
                catalog["markets"] = tuple(
                    {**dict(row), **self.refreshed_reward_override}
                    for row in catalog["markets"]
                )
        return catalog

    def lp_account_snapshot(self):
        self.account_reads += 1
        account = super().lp_account_snapshot()
        if self.account_reads == 1:
            # The round-start receipt carries the stale-origins delta; a
            # renewal is a real adapter read and stamps the current clock.
            account["checked_at"] = self.now + self.account_stamp_delta
        else:
            account["checked_at"] = self.now
        if self.open_orders:
            account["open_orders"] = list(self.open_orders)
        return account

    def lp_order_books(self, token_ids, *, stop_event=None):
        books = super().lp_order_books(token_ids, stop_event=stop_event)
        self.now = self.now + self.step
        return books


class _LPWallClockRenewalExchange(_LPAdvancingClockExchange):
    """Renewal adapters whose stamps postdate the call, like real HTTP reads.

    Issue #143 review round 3: every shared-fact read (metadata/fees,
    reward catalog, account) advances the fixture clock one second before
    stamping — the wall-clock latency of a real network read — so renewal
    stamps land strictly after a clock captured before the renewal.  With
    the batch evaluation clock taken before ``_renew_batch_shared_facts``
    this made every renewed batch die as stale unknowns even though the
    renewal had just succeeded.
    """

    def lp_market_metadata(self, condition_ids, *, stop_event=None):
        self.now = self.now + timedelta(seconds=1)
        return super().lp_market_metadata(condition_ids, stop_event=stop_event)

    def lp_reward_catalog(self, *, condition_ids=None, stop_event=None):
        self.now = self.now + timedelta(seconds=1)
        return super().lp_reward_catalog(
            condition_ids=condition_ids, stop_event=stop_event
        )

    def lp_account_snapshot(self):
        self.now = self.now + timedelta(seconds=1)
        return super().lp_account_snapshot()


def _batch_pools() -> dict[str, Decimal]:
    pools = {
        f"N{index:02d}": Decimal(500 - index) for index in range(1, 19)
    }
    pools["B1"] = Decimal(400)
    pools["B2"] = Decimal(300)
    return pools


def _seed_stale_backup_summaries(
    store: PredictionArbitrageStore,
    now: datetime,
    suffixes: tuple[str, ...],
) -> None:
    """Pre-seed known summaries whose reference price is older than 1h.

    The batch exchange answers history reads for backup tokens with
    "unknown", so the refresh keeps these prior summaries: base facts stay
    valid (amplitude known, window valid) while the reference price is
    stale, which is exactly the backup-queue definition.
    """

    checked_at = now - timedelta(hours=2)
    store.lp_save_price_history_batch(
        [
            {
                "condition_id": f"condition-{suffix}",
                "token_id": f"token-condition-{suffix}-yes",
                "samples": [
                    {
                        "t": int((checked_at - timedelta(hours=1)).timestamp()),
                        "p": "0.300",
                    },
                    {
                        "t": int(checked_at.timestamp()),
                        "p": "0.305",
                    },
                ],
                "summary": {
                    "state": "known",
                    "amplitude": Decimal("0.005"),
                    "checked_at": checked_at,
                    "window_start": checked_at - timedelta(hours=22),
                    "window_end": checked_at,
                    "sample_count": 2,
                    "latest_midpoint": Decimal("0.305"),
                    "valid_until": now + timedelta(hours=22),
                },
            }
            for suffix in suffixes
        ]
    )


def test_batch_refresh_backfills_until_ten_passed(tmp_path) -> None:
    """S2 acceptance case 1, issue #157 rotation: two exploration batches
    cover the twenty queued markets (normal queue order first, then the
    backup queue), ten passers publish, and nothing is read twice."""
    now = datetime(2026, 9, 19, 6, tzinfo=UTC)
    current = {"now": now}
    exchange = _LPBatchQueryExchange(
        now,
        _batch_pools(),
        backup=frozenset({"B1", "B2"}),
        reject=frozenset({
            "N04", "N05", "N06", "N07", "N08", "N09", "N17", "N18", "B1", "B2",
        }),
    )
    store = PredictionArbitrageStore(tmp_path)
    _seed_stale_backup_summaries(store, now, ("B1", "B2"))
    lp = PolymarketLPService(store, exchange, clock=lambda: current["now"])
    # Backup markets keep their stale summaries, so preparation reports
    # partial; the scan proceeds and treats those markets as backup rows.
    assert lp.refresh_price_history()["state"] in {"known", "partial"}

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    # Batch one: the first ten normal-queue markets, 20 tokens in one call.
    assert len(exchange.book_token_reads) == 1
    first = exchange.book_token_reads[0]
    assert len(first) == 20
    first_markets = {
        token.removeprefix("token-").rsplit("-", 1)[0] for token in first
    }
    assert first_markets == {
        f"condition-N{index:02d}" for index in range(1, 11)
    }
    funnel = snapshot["funnel"]
    assert funnel["checked"] == 10
    assert funnel["passed"] == 4
    assert funnel["rejected"] == 6
    assert funnel["batches"] == 1
    assert snapshot["candidate_valid_count"] == 4
    assert snapshot["candidate_pending_count"] == 10

    # Batch two: the rest of the normal queue plus both backup markets.
    current["now"] = now + timedelta(seconds=5)
    exchange.now = current["now"]
    second = lp.refresh_candidates(force=True)

    assert len(exchange.book_token_reads) == 2
    second_tokens = exchange.book_token_reads[1]
    assert len(second_tokens) == 20
    assert len(set(first) | set(second_tokens)) == 40
    second_markets = {
        token.removeprefix("token-").rsplit("-", 1)[0] for token in second_tokens
    }
    assert second_markets == {
        *{f"condition-N{index:02d}" for index in range(11, 19)},
        "condition-B1",
        "condition-B2",
    }
    funnel = second["funnel"]
    assert funnel["checked"] == 20
    assert funnel["passed"] == 10
    assert funnel["rejected"] == 10
    assert funnel["unknown"] == 0
    assert funnel["unchecked"] == 0
    assert funnel["batches"] == 2
    assert funnel["backup_read"] == 2
    candidates = second["candidates"]
    assert len(candidates) == 10
    passed_markets = {
        "market-N01", "market-N02", "market-N03",
        *{f"market-N{index:02d}" for index in range(10, 17)},
    }
    assert {row["market_id"] for row in candidates} == passed_markets
    # Every published row is a live-qualified passer.
    for row in candidates:
        assert row["selected_direction"] is not None
        assert "realtime_capital" in row
        assert row["state"] == "eligible"
        assert row["verification"] == "verified"
    # Ranking key (issue #157): equal yields at equal estimate times fall
    # to the stable identity, so the lowest condition id heads the table.
    head_id = min(
        passed_markets,
        key=lambda market_id: int(market_id.removeprefix("market-N")),
    )
    assert candidates[0]["market_id"] == head_id == "market-N01"
    assert second["recommendations"][0]["market_id"] == head_id
    assert second["selected_results"][0]["market_id"] == head_id
    assert second["selected_market_ids"] == [
        row["market_id"] for row in candidates
    ]


def test_batch_refresh_stops_at_fifty_markets_checked(tmp_path) -> None:
    """S3 acceptance case 2, issue #157 rotation: only four of the queued
    markets pass and every batch is one capped book read with no repeated
    token — the old 50-market round budget is gone, batches just keep
    rolling on the next call."""
    now = datetime(2026, 9, 19, 7, tzinfo=UTC)
    current = {"now": now}
    pools = {f"N{index:02d}": Decimal(600 - index) for index in range(1, 61)}
    exchange = _LPBatchQueryExchange(
        now,
        pools,
        reject=frozenset({f"N{index:02d}" for index in range(5, 61)}),
    )
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    funnel = snapshot["funnel"]
    assert funnel["stop_reason"] is None
    assert funnel["checked"] == 10
    assert funnel["passed"] == 4
    assert funnel["rejected"] == 6
    assert funnel["unknown"] == 0
    assert funnel["unchecked"] == 50
    assert funnel["batches"] == 1
    assert funnel["backup_read"] == 0
    # Binary markets: ten markets × 2 tokens in one ≤20-token call.
    assert len(exchange.book_token_reads) == 1
    assert len(exchange.book_token_reads[0]) == 20
    candidates = snapshot["candidates"]
    assert [row["market_id"] for row in candidates] == [
        "market-N01", "market-N02", "market-N03", "market-N04",
    ]
    assert all(row["selected_direction"] is not None for row in candidates)
    assert snapshot["recommendations"][0]["market_id"] == "market-N01"
    assert snapshot["candidate_pending_count"] == 50

    # The roll continues: the next batch reads the following ten markets
    # (all rejected) without re-reading any earlier token.
    current["now"] = now + timedelta(seconds=5)
    exchange.now = current["now"]
    second = lp.refresh_candidates(force=True)
    all_tokens = [
        token for batch in exchange.book_token_reads for token in batch
    ]
    assert len(exchange.book_token_reads) == 2
    assert len(all_tokens) == 40
    assert len(set(all_tokens)) == 40
    assert second["funnel"]["checked"] == 20
    assert second["funnel"]["rejected"] == 16
    assert second["candidate_valid_count"] == 4


def test_batch_refresh_renews_stale_market_facts_mid_round(tmp_path) -> None:
    """R2-1 (issue #143 repair 2): real-scan latency must not poison batches.

    Batches 1-2 evaluate on facts inside the 60-second window; batch 3
    crosses it (+90 seconds against the round-start stamps).  The repair
    renews each shared fact class once per batch, targeted at that batch's
    conditions, so batch 3 qualifies on fresh facts instead of every market
    dying as ``market_metadata_stale``.
    """

    now = datetime(2026, 9, 19, 9, tzinfo=UTC)
    pools = {f"N{index:02d}": Decimal(600 - index) for index in range(1, 31)}
    exchange = _LPAdvancingClockExchange(
        now,
        pools,
        reject=frozenset({f"N{index:02d}" for index in range(1, 21)}),
    )
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: exchange.now
    )
    assert lp.refresh_price_history()["state"] == "known"

    # Issue #157: each batch is one call.  The advancing clock moves 30s per
    # book read, so batches 1-2 evaluate inside the 60-second window while
    # batch 3 crosses it.
    lp.refresh_candidates(force=True)
    lp.refresh_candidates(force=True)
    snapshot = lp.refresh_candidates(force=True)

    funnel = snapshot["funnel"]
    assert funnel["checked"] == 30
    assert funnel["batches"] == 3
    assert funnel["rejected"] == 20
    assert funnel["passed"] == 10
    assert funnel["unknown"] == 0
    # Batch three (condition-N21..N30) crossed the window and was renewed:
    # exactly one targeted metadata read covering exactly that batch's
    # conditions, one targeted reward read, one account renewal.  Batches
    # 1-2 stayed inside the window and renewed nothing.
    assert len(exchange.metadata_fresh_reads) == 1
    assert set(exchange.metadata_fresh_reads[0]) == {
        f"condition-N{index:02d}" for index in range(21, 31)
    }
    assert len(exchange.targeted_reward_reads) == 1
    assert set(exchange.targeted_reward_reads[0]) == set(
        exchange.metadata_fresh_reads[0]
    )
    assert exchange.account_reads == 2
    candidates = snapshot["candidates"]
    assert {row["market_id"] for row in candidates} == {
        f"market-N{index:02d}" for index in range(21, 31)
    }
    assert all(row["state"] == "eligible" for row in candidates)


def test_batch_refresh_renews_facts_stale_at_scan_start(tmp_path) -> None:
    """R2-2 (issue #143 repair 2): a stale prepared cache cannot sink batch 1.

    The prepared input snapshot may serve a preparation that is hours old,
    so the very first batch can start past the 60-second window.  The scan
    must renew the expired metadata and reward facts for that batch's
    conditions before qualifying them.
    """

    now = datetime(2026, 9, 19, 10, tzinfo=UTC)
    pools = {f"N{index:02d}": Decimal(600 - index) for index in range(1, 11)}
    exchange = _LPAdvancingClockExchange(now, pools)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: exchange.now
    )
    assert lp.refresh_price_history()["state"] == "known"
    # The prepared snapshot serves a preparation built ten minutes ago.
    exchange.now = now + timedelta(minutes=10)

    snapshot = lp.refresh_candidates(force=True)

    funnel = snapshot["funnel"]
    assert funnel["checked"] == 10
    assert funnel["batches"] == 1
    assert funnel["passed"] == 10
    assert funnel["unknown"] == 0
    # The first batch renewed the expired facts once, targeted at exactly
    # its own conditions.
    assert len(exchange.metadata_fresh_reads) == 1
    assert set(exchange.metadata_fresh_reads[0]) == {
        f"condition-N{index:02d}" for index in range(1, 11)
    }
    assert len(exchange.targeted_reward_reads) == 1
    assert set(exchange.targeted_reward_reads[0]) == set(
        exchange.metadata_fresh_reads[0]
    )
    # The round-start account was read inside the window: no renewal.
    assert exchange.account_reads == 1
    assert all(row["state"] == "eligible" for row in snapshot["candidates"])


def test_batch_refresh_renews_stale_reward_and_account_facts(tmp_path) -> None:
    """R2-3 (issue #143 repair 2): reward and account facts renew targeted.

    Two scans, each with exactly one stale fact class at the batch clock.
    A refreshed reward row that flips ``reward_active`` to ``False`` must
    turn the verdict into a ``reward_inactive`` rejection (judged on the
    new facts).  A refreshed account must be judged on its own receipts and
    apply the open-order reservation exactly once: balance 50 minus the 30
    reserved order still funds the 6.80 minimum, a double deduction would
    not.
    """

    now = datetime(2026, 9, 19, 11, tzinfo=UTC)
    pools = {f"N{index:02d}": Decimal(600 - index) for index in range(1, 11)}

    # Scenario A: only the reward receipt is stale when the batch evaluates.
    reward_exchange = _LPAdvancingClockExchange(
        now,
        pools,
        metadata_stamp_delta=timedelta(minutes=10),
        refreshed_reward_override={"reward_active": False},
    )
    reward_lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "reward"),
        reward_exchange,
        clock=lambda: reward_exchange.now,
    )
    assert reward_lp.refresh_price_history()["state"] == "known"
    reward_exchange.now = now + timedelta(minutes=10)

    reward_snapshot = reward_lp.refresh_candidates(force=True)

    reward_funnel = reward_snapshot["funnel"]
    assert reward_funnel["checked"] == 10
    assert reward_funnel["passed"] == 0
    assert reward_funnel["rejected"] == 10
    assert reward_funnel["unknown"] == 0
    # The refreshed reward facts were judged: once, targeted, and the
    # renewed ``reward_active=False`` row rejected every market.  Metadata
    # and account stayed inside the window and renewed nothing.
    assert len(reward_exchange.targeted_reward_reads) == 1
    assert set(reward_exchange.targeted_reward_reads[0]) == {
        f"condition-N{index:02d}" for index in range(1, 11)
    }
    assert reward_exchange.metadata_fresh_reads == ()
    assert reward_exchange.account_reads == 1

    # Scenario B: only the account receipt is stale when the batch
    # evaluates; the renewed account carries a 30.00 open BUY on another
    # market that must be reserved exactly once.
    other_order = {
        "order_id": "res-other",
        "market_id": "market-OTHER",
        "condition_id": "condition-OTHER",
        "token_id": "token-OTHER-yes",
        "side": "BUY",
        "status": "LIVE",
        "price": Decimal("1"),
        "remaining_size": Decimal("30"),
    }
    account_exchange = _LPAdvancingClockExchange(
        now,
        pools,
        metadata_stamp_delta=timedelta(minutes=10),
        reward_stamp_delta=timedelta(minutes=10),
        account_stamp_delta=timedelta(minutes=-10),
        open_orders=(other_order,),
        available="50",
    )
    account_lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "account"),
        account_exchange,
        clock=lambda: account_exchange.now,
    )
    assert account_lp.refresh_price_history()["state"] == "known"
    account_exchange.now = now + timedelta(minutes=10)

    account_snapshot = account_lp.refresh_candidates(force=True)

    account_funnel = account_snapshot["funnel"]
    assert account_funnel["checked"] == 10
    assert account_funnel["passed"] == 10
    assert account_funnel["rejected"] == 0
    assert account_funnel["unknown"] == 0
    # The account renewed exactly once (round start plus one renewal) and
    # eligibility ran on the renewed receipts; metadata and reward were
    # still fresh and renewed nothing.
    assert account_exchange.account_reads == 2
    assert account_exchange.metadata_fresh_reads == ()
    assert account_exchange.targeted_reward_reads == ()
    # 50.00 balance minus the 30.00 reserved order funds exactly one 6.80
    # minimum order — a duplicated reservation deduction would reject.
    for row in account_snapshot["candidates"]:
        assert row["state"] == "eligible"
        assert Decimal(str(row["realtime_capital"])) == Decimal("6.80")


def test_batch_renewal_latency_keeps_renewed_facts_evaluable(tmp_path) -> None:
    """R3-1 (issue #143 repair 3): renewal latency must not poison the batch.

    Real renewal reads carry network latency, so their stamps land
    strictly after the moment the batch began renewing.  The batch
    evaluation clock is captured only after ``_renew_batch_shared_facts``
    returns, so the renewed stamps are judged on a clock at or after their
    own time (age >= 0) and the batch qualifies on the renewed facts
    instead of every market dying as a stale unknown.
    """

    now = datetime(2026, 9, 19, 13, tzinfo=UTC)
    pools = {f"N{index:02d}": Decimal(600 - index) for index in range(1, 11)}
    exchange = _LPWallClockRenewalExchange(now, pools)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: exchange.now
    )
    assert lp.refresh_price_history()["state"] == "known"
    # The prepared snapshot is ten minutes old at scan start (the R2-2
    # scenario): batch one must renew its metadata and reward facts, and
    # each renewal read takes one wall-clock second.
    exchange.now = now + timedelta(minutes=10)

    snapshot = lp.refresh_candidates(force=True)

    funnel = snapshot["funnel"]
    assert funnel["checked"] == 10
    assert funnel["batches"] == 1
    assert funnel["passed"] == 10
    assert funnel["rejected"] == 0
    assert funnel["unknown"] == 0
    # Each expired fact class renewed once, targeted at the batch's own
    # conditions, and the renewed stamps were judged fresh.
    assert len(exchange.metadata_fresh_reads) == 1
    assert set(exchange.metadata_fresh_reads[0]) == {
        f"condition-N{index:02d}" for index in range(1, 11)
    }
    assert len(exchange.targeted_reward_reads) == 1
    assert set(exchange.targeted_reward_reads[0]) == set(
        exchange.metadata_fresh_reads[0]
    )
    # The round-start account receipt is inside the window: no renewal.
    assert exchange.account_reads == 1
    assert all(row["state"] == "eligible" for row in snapshot["candidates"])


def test_batch_refresh_failure_keeps_honest_stale_unknown(tmp_path) -> None:
    """R2-4 (issue #143 repair 2): a failed renewal degrades honestly.

    The metadata renewal for batch 3 raises.  Batch 3 and batch 4 then
    qualify on the old facts and report ``market_metadata_stale`` unknowns,
    the round makes exactly one renewal attempt for the class, and a forced
    next round retries and recovers.
    """

    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    pools = {f"N{index:02d}": Decimal(600 - index) for index in range(1, 41)}
    exchange = _LPAdvancingClockExchange(
        now,
        pools,
        reject=frozenset({f"N{index:02d}" for index in range(1, 21)}),
        metadata_fresh_mode="failure",
    )
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: exchange.now
    )
    assert lp.refresh_price_history()["state"] == "known"

    # Issue #157: four batches, one call each.  Batches 3 and 4 cross the
    # fact window; their metadata renewal attempts fail while the reward
    # class renews targeted.
    for _ in range(3):
        lp.refresh_candidates(force=True)
    first = lp.refresh_candidates(force=True)

    funnel = first["funnel"]
    assert funnel["checked"] == 40
    assert funnel["batches"] == 4
    assert funnel["rejected"] == 20
    assert funnel["passed"] == 0
    assert funnel["unknown"] == 20
    stale_reasons = {
        row["condition_id"]: row["code"]
        for row in funnel["reasons"]["trial"]
    }
    assert len(stale_reasons) == 20
    assert all(
        stale_reasons[f"condition-N{index:02d}"] == "market_metadata_stale"
        for index in range(21, 41)
    )
    # Exactly one renewal attempt per class per batch: the batch-3 metadata
    # failure is not retried on batch 4 (one call total), while the reward
    # class lawfully renews once per batch for that batch's own conditions
    # (their round-start receipts each age past the window).  The account
    # fact is shared across conditions, so its batch-3 renewal covers
    # batch 4 and reads only twice in the round.
    # Per-call batches retry the failed class on the next call: one failed
    # attempt per batch, targeted at that batch's own conditions.
    assert exchange.metadata_fresh_calls == 2
    assert exchange.metadata_fresh_reads == (
        tuple(f"condition-N{index:02d}" for index in range(21, 31)),
        tuple(f"condition-N{index:02d}" for index in range(31, 41)),
    )
    assert len(exchange.targeted_reward_reads) == 2
    assert set(exchange.targeted_reward_reads[0]) == {
        f"condition-N{index:02d}" for index in range(21, 31)
    }
    assert set(exchange.targeted_reward_reads[1]) == {
        f"condition-N{index:02d}" for index in range(31, 41)
    }
    assert exchange.account_reads == 2

    # The next calls retry the renewal and recover: one batch per call,
    # each renewing its own conditions once (their receipts are the
    # still-stale prepared stamps).
    exchange.metadata_fresh_mode = "fresh"
    lp.refresh_candidates(force=True)
    lp.refresh_candidates(force=True)
    lp.refresh_candidates(force=True)
    second = lp.refresh_candidates(force=True)

    second_funnel = second["funnel"]
    assert second_funnel["checked"] == 80
    assert second_funnel["batches"] == 8
    # Batches three and four both pass on the recovered facts: twenty
    # passers merge, and the published table keeps the best ten yields
    # (N21..N30, the highest pools among them).
    assert second_funnel["passed"] == 20
    assert second_funnel["rejected"] == 40
    assert second_funnel["unknown"] == 20
    assert exchange.metadata_fresh_calls == 6
    assert exchange.metadata_fresh_reads == (
        tuple(f"condition-N{index:02d}" for index in range(21, 31)),
        tuple(f"condition-N{index:02d}" for index in range(31, 41)),
        tuple(f"condition-N{index:02d}" for index in range(1, 11)),
        tuple(f"condition-N{index:02d}" for index in range(11, 21)),
        tuple(f"condition-N{index:02d}" for index in range(21, 31)),
        tuple(f"condition-N{index:02d}" for index in range(31, 41)),
    )
    assert {row["market_id"] for row in second["candidates"]} == {
        f"market-N{index:02d}" for index in range(21, 31)
    }
    assert all(row["state"] == "eligible" for row in second["candidates"])


def test_batch_refresh_isolates_missing_books_and_reads_backup_once(tmp_path) -> None:
    """S4: a batch's missing books isolate to unknown, backups read once."""
    now = datetime(2026, 9, 19, 8, tzinfo=UTC)
    pools = {f"N{index:02d}": Decimal(500 - index) for index in range(1, 12)}
    pools["B1"] = Decimal(400)
    pools["B2"] = Decimal(300)
    exchange = _LPBatchQueryExchange(
        now,
        pools,
        backup=frozenset({"B1", "B2"}),
        omit_tokens=frozenset({
            "token-condition-B1-yes",
            "token-condition-B1-no",
        }),
    )
    store = PredictionArbitrageStore(tmp_path)
    _seed_stale_backup_summaries(store, now, ("B1", "B2"))
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    assert lp.refresh_price_history()["state"] in {"known", "partial"}

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    # Batch one (issue #157 rotation): the ten normal-queue markets.
    assert len(exchange.book_token_reads) == 1
    assert len(exchange.book_token_reads[0]) == 20
    funnel = snapshot["funnel"]
    assert funnel["checked"] == 10
    assert funnel["passed"] == 10

    # Batch two: N11 plus both backup markets and the oldest-tried rotation
    # tail; B1's two books are missing from the response and were requested
    # exactly once, never retried.
    second = lp.refresh_candidates(force=True)
    assert len(exchange.book_token_reads) == 2
    missing = {"token-condition-B1-yes", "token-condition-B1-no"}
    for token in missing:
        reads = sum(batch.count(token) for batch in exchange.book_token_reads)
        assert reads == 1
    # The backup markets were each read in exactly one batch call.
    for suffix in ("B1", "B2"):
        batches_with_backup = [
            batch
            for batch in exchange.book_token_reads
            if any(f"condition-{suffix}-" in token for token in batch)
        ]
        assert len(batches_with_backup) == 1
    funnel = second["funnel"]
    # Rotation: batch two = 3 never-tried + 7 oldest-tried markets.
    assert funnel["checked"] == 20
    assert funnel["passed"] == 19
    assert funnel["rejected"] == 0
    assert funnel["unknown"] == 1
    assert funnel["backup_read"] == 2
    unknown_reasons = [
        row for row in funnel["reasons"]["trial"]
        if row.get("condition_id") == "condition-B1"
    ]
    assert unknown_reasons
    assert all(row.get("code") == "book_unknown" for row in unknown_reasons)
    candidates = second["candidates"]
    assert len(candidates) == 10
    assert all(row["state"] == "eligible" for row in candidates)
    assert "market-B1" not in {row["market_id"] for row in candidates}


def test_batch_scan_funnel_trial_counts_published_passers(tmp_path) -> None:
    """Reviewer fix 5: after a batch scan the funnel's trial stage counts the
    published passers, not the ≤10 view preview batch, so the funnel, the
    scan progress line, and the table agree."""
    now = datetime(2026, 9, 19, 10, tzinfo=UTC)
    pools = {f"N{index:02d}": Decimal(500 - index) for index in range(1, 13)}
    exchange = _LPBatchQueryExchange(
        now,
        pools,
        reject=frozenset({"N03", "N07", "N08", "N12"}),
    )
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    assert lp.refresh_price_history()["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    funnel = snapshot["funnel"]
    candidates = snapshot["candidates"]
    # Issue #157 rotation: two batches cover the 12 kept markets; 8 pass
    # the live check (4 reject off-tick) and only passers enter the pool.
    second = lp.refresh_candidates(force=True)
    funnel = second["funnel"]
    candidates = second["candidates"]
    assert funnel["checked"] == 20
    # 8 unique passers: the second batch re-passes five tried markets, so
    # the cumulative passed counter counts re-estimates too.
    assert funnel["passed"] == 13
    assert len(candidates) == 8
    assert all(row["state"] == "eligible" for row in candidates)
    # The funnel's trial stage equals the valid pool, which the table shows
    # in full here.
    assert funnel["trial"] == 8
    assert funnel["trial"] == second["candidate_valid_count"]


def test_candidate_qualification_facts_writes_hold_state_lock() -> None:
    """Reviewer fix 2 (lock discipline): every in-place subscript write to
    `_candidate_qualification_facts` sits inside a
    `with self._candidate_state_lock` block, so a concurrent
    `candidate_snapshot()` deepcopy can never iterate a dict that changes
    size. Whole-reference swaps are atomic and stay allowed anywhere."""
    source = Path(polymarket_lp.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def inside_state_lock(node: ast.AST) -> bool:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, ast.With):
                for item in current.items:
                    context = item.context_expr
                    if (
                        isinstance(context, ast.Attribute)
                        and context.attr == "_candidate_state_lock"
                    ):
                        return True
            current = parents.get(current)
        return False

    def targets_facts_subscription(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "_candidate_qualification_facts"
        )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(targets_facts_subscription(target) for target in node.targets)
            and not inside_state_lock(node)
        ):
            offenders.append(f"line {node.lineno}")
    assert not offenders, (
        "unlocked in-place writes to _candidate_qualification_facts: "
        + ", ".join(offenders)
    )


def test_batch_refresh_ranks_passers_by_actual_capital_and_maintains_top_one(
    tmp_path,
) -> None:
    """S5 (#138 round 2): batches merge by estimated target-share yield;
    the 60-second maintenance refreshes all published rows in one batch."""
    now = datetime(2026, 9, 19, 9, tzinfo=UTC)
    current = {"now": now}
    pools = {f"A{index:02d}": Decimal(480) for index in range(1, 10)}
    pools["Z1"] = Decimal(480)
    pools["B1"] = Decimal(480)

    class Exchange(_LPBatchQueryExchange):
        def __init__(self, initial_now: datetime) -> None:
            super().__init__(
                initial_now,
                pools,
                backup=frozenset({"B1"}),
                reject=frozenset({"A09"}),
                bid_by_suffix={
                    **{f"A{index:02d}": Decimal("0.50") for index in range(1, 9)},
                    "Z1": Decimal("0.30"),
                    "B1": Decimal("0.34"),
                },
            )
            self.competitiveness = {
                **{f"condition-A{index:02d}": index for index in range(1, 10)},
                "condition-Z1": 10,
                "condition-B1": 11,
            }

        def lp_market_competitiveness(self, *, stop_event=None, previous=None):
            del stop_event, previous
            return {
                "state": "known",
                "complete": True,
                "checked_at": self.now,
                "round_checked_at": self.now,
                "competitiveness": {
                    key: (Decimal(value), self.now)
                    for key, value in self.competitiveness.items()
                },
                "not_updated": [],
            }

        def lp_order_books(self, token_ids, *, stop_event=None):
            books = super().lp_order_books(token_ids, stop_event=stop_event)
            current["now"] += timedelta(seconds=1)
            self.now = current["now"]
            return {
                token_id: {**dict(book), "received_at": self.now}
                for token_id, book in books.items()
            }

    exchange = Exchange(now)
    store = PredictionArbitrageStore(tmp_path)
    _seed_stale_backup_summaries(store, now, ("B1",))
    lp = PolymarketLPService(store, exchange, clock=lambda: current["now"])
    assert lp.refresh_price_history()["state"] in {"known", "partial"}

    snapshot = lp.refresh_candidates(force=True)
    # Issue #157: Z1 rolls in the second exploration batch (it queues last
    # behind the tied A-group and B1).
    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    assert len(exchange.book_token_reads) == 2
    # Independent arithmetic (quantity 20 × live bid, pool 480):
    #   Z1 capital 6.00 → 480/(24×6)×100  = 333.333333 (checked in batch two)
    #   B1 capital 6.80 → 480/(24×6.8)×100 = 294.117647 (backup, batch one)
    #   A01..A08 capital 10.00 → 200.000000, ties fall to competition then
    #   token_id (A05 before A06 share competition 5); A09 rejects off-tick.
    candidates = snapshot["candidates"]
    assert [row["market_id"] for row in candidates] == [
        "market-Z1", "market-B1",
        "market-A01", "market-A02", "market-A03", "market-A04",
        "market-A05", "market-A06", "market-A07", "market-A08",
    ]
    # Independent arithmetic for the #138 round-2 estimate: identical books
    # give every market the min-dominated target 20.00 shares, so the hourly
    # $480×5%/24 = $1.00 divides by 20×bid capital:
    #   Z1 20×0.30 = 6.00  → 16.666667%/h (checked in batch two)
    #   B1 20×0.34 = 6.80  → 14.705882%/h (backup, batch one)
    #   A01..A08 20×0.50 = 10.00 → 10.000000%/h, ties fall to competition
    #   then token_id (A05 before A06 share competition 5); A09 rejects.
    assert Decimal(
        str(candidates[0]["estimated_yield_pct_per_hour"])
    ) == Decimal("16.666667")
    assert Decimal(
        str(candidates[1]["estimated_yield_pct_per_hour"])
    ) == Decimal("14.705882")
    assert snapshot["recommendations"][0]["market_id"] == "market-Z1"

    # Immediately after the scan the 30-second lead window is still open
    # (issue #146): the maintenance call is a pure snapshot read.
    before_maintenance_reads = len(exchange.book_token_reads)
    current["now"] = now + timedelta(seconds=29)
    exchange.now = current["now"]
    assert lp.refresh_candidate_recommendations()["recommendations"]
    assert len(exchange.book_token_reads) == before_maintenance_reads

    # Once the oldest source passes the 30-second lead the maintenance
    # refreshes every published row with one batched book read (ten rows ×
    # two tokens = the 20-token cap in one call).
    current["now"] = now + timedelta(seconds=65)
    exchange.now = current["now"]
    maintained = lp.refresh_candidate_recommendations()
    assert maintained["recommendations"][0]["market_id"] == "market-Z1"
    new_reads = exchange.book_token_reads[before_maintenance_reads:]
    assert len(new_reads) == 1
    assert len(new_reads[0]) == 20
    # The maintained rows refresh in place in the published table.
    assert maintained["candidates"][0]["realtime_checked_at"] == (
        current["now"]
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    assert len(maintained["selected_results"]) == 10
    assert maintained["selected_results"][1]["market_id"] == "market-B1"
    assert maintained["candidates"][1]["market_id"] == "market-B1"
    for row in maintained["candidates"]:
        assert row["estimate_updated"] is True
    # A second maintenance tick inside the fresh window stays read-free.
    steady = len(exchange.book_token_reads)
    assert lp.refresh_candidate_recommendations()["recommendations"]
    assert len(exchange.book_token_reads) == steady


def test_maintenance_recomputes_upper_bound_from_new_capital(tmp_path) -> None:
    """Reviewer fix 4 (#138 round 2): when 60-second maintenance refreshes a
    row's live price, the 5% target-share estimate is recomputed from the
    new book — the target capital and hourly yield follow the new bid."""
    now = datetime(2026, 9, 19, 9, tzinfo=UTC)
    current = {"now": now}
    pools = {f"A{index:02d}": Decimal(480) for index in range(1, 10)}
    pools["Z1"] = Decimal(480)

    class Exchange(_LPBatchQueryExchange):
        def __init__(self, initial_now: datetime) -> None:
            super().__init__(
                initial_now,
                pools,
                reject=frozenset({"A09"}),
                bid_by_suffix={
                    **{f"A{index:02d}": Decimal("0.50") for index in range(1, 9)},
                    "Z1": Decimal("0.30"),
                },
            )
            self.competitiveness = {
                **{f"condition-A{index:02d}": index for index in range(1, 10)},
                "condition-Z1": 10,
            }

        def lp_market_competitiveness(self, *, stop_event=None, previous=None):
            del stop_event, previous
            return {
                "state": "known",
                "complete": True,
                "checked_at": self.now,
                "round_checked_at": self.now,
                "competitiveness": {
                    key: (Decimal(value), self.now)
                    for key, value in self.competitiveness.items()
                },
                "not_updated": [],
            }

    exchange = Exchange(now)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: current["now"])
    assert lp.refresh_price_history()["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["recommendations"][0]["market_id"] == "market-Z1"
    # Independent arithmetic: pool 480, hourly gross reward 480×5%/24 =
    # $1.00; at bid 0.30 the min-dominated target is 20.00 shares for
    # 6.00 capital → 1/6×100 = 16.666667%/h.
    assert Decimal(
        str(snapshot["candidates"][0]["estimated_yield_pct_per_hour"])
    ) == Decimal("16.666667")
    assert Decimal(
        str(snapshot["candidates"][0]["estimated_target_capital_usd"])
    ) == Decimal("6.00")

    # The live bid moves before the maintenance tick: the min-dominated
    # target becomes 20 × 0.45 = 9.00 capital and the yield must follow the
    # new book (1/9×100 = 11.111111%/h), not the stale scan value.
    exchange.bid_by_suffix["Z1"] = Decimal("0.45")
    current["now"] = now + timedelta(seconds=65)
    exchange.now = current["now"]
    maintained = lp.refresh_candidate_recommendations()

    assert maintained["recommendations"][0]["market_id"] == "market-Z1"
    assert Decimal(
        str(maintained["candidates"][0]["realtime_capital"])
    ) == Decimal("9.00")
    assert Decimal(
        str(maintained["candidates"][0]["estimated_target_capital_usd"])
    ) == Decimal("9.00")
    assert Decimal(
        str(maintained["candidates"][0]["estimated_yield_pct_per_hour"])
    ) == Decimal("11.111111")


def test_batch_scan_cadence_and_shared_round_protection(tmp_path) -> None:
    """S6 + issue #157: batches roll with no round window, concurrent calls
    share one in-flight batch, and a newer durable pool wins the save
    arbiter over an older writer."""
    now = datetime(2026, 9, 19, 10, tzinfo=UTC)
    current = {"now": now}
    pools = {f"N{index:02d}": Decimal(500 - index) for index in range(1, 4)}
    exchange = _LPBatchQueryExchange(now, pools)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: current["now"])
    assert lp.refresh_price_history()["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert first["state"] == "ready"
    reads_after_first = len(exchange.book_token_reads)
    assert reads_after_first == 1

    # Issue #157: there is no 300-second window — the next call rolls the
    # next batch (the same three markets re-estimate) and reads again.
    current["now"] = now + timedelta(seconds=60)
    exchange.now = current["now"]
    exchanged = lp.refresh_candidates(force=False)
    assert len(exchange.book_token_reads) == reads_after_first + 1
    assert exchanged["funnel"]["batches"] == 2
    # Batches never read the competition reader: it is cache-only.
    assert exchange.competition_reads == 0

    # Concurrent calls share one in-flight batch: while a batch is blocked
    # inside its book read, another call returns the scanning snapshot
    # instead of starting a second batch.
    release = threading.Event()
    entered = threading.Event()

    class BlockingExchange(_LPBatchQueryExchange):
        def lp_order_books(self, token_ids, *, stop_event=None):
            entered.set()
            release.wait(timeout=5)
            return super().lp_order_books(token_ids, stop_event=stop_event)

    blocked_exchange = BlockingExchange(now, pools)
    shared_store = PredictionArbitrageStore(tmp_path / "shared")
    blocked_lp = PolymarketLPService(
        shared_store,
        blocked_exchange,
        clock=lambda: current["now"],
    )
    assert blocked_lp.refresh_price_history()["state"] == "known"
    current["now"] = now + timedelta(seconds=62)
    blocked_exchange.now = current["now"]
    round_result: dict[str, object] = {}

    def run_round() -> None:
        round_result["snapshot"] = blocked_lp.refresh_candidates(force=True)

    round_thread = threading.Thread(target=run_round)
    round_thread.start()
    assert entered.wait(timeout=5)
    blocked_reads = len(blocked_exchange.book_token_reads)
    shared = blocked_lp.refresh_candidates(force=False)
    assert shared["scanning"] is True
    assert len(blocked_exchange.book_token_reads) == blocked_reads

    # A pool persisted by a newer writer wins the durable save arbiter over
    # this older writer, while the blocked batch keeps its own honest rows
    # in memory.
    newer_started = current["now"] + timedelta(seconds=3600)
    marker = {
        "market_id": "market-newer",
        "condition_id": "condition-newer",
        "updated_at": _iso_z(newer_started),
        "expires_at": _iso_z(newer_started + timedelta(seconds=300)),
        "refresh_failed": False,
        "state": "eligible",
    }
    shared_store.lp_save_screening_snapshot(
        {
            "pool_version": 2,
            "scan_started_at": _iso_z(newer_started),
            "pool": {"condition-newer": marker},
            "rotation": {},
            "event_end_confirmations": {},
        }
    )
    release.set()
    round_thread.join(timeout=5)
    settled = round_result["snapshot"]
    assert settled["scanning"] is False
    assert settled["candidate_valid_count"] == 3
    durable = shared_store.lp_screening_snapshot()
    assert "condition-newer" in durable["pool"]
    assert blocked_lp.candidate_snapshot()["scanning"] is False


def test_batch_refresh_early_stops_when_account_unavailable(tmp_path) -> None:
    """S7: an unusable account fact ends the round before any batch read."""
    now = datetime(2026, 9, 19, 11, tzinfo=UTC)
    current = {"now": now}
    pools = {f"N{index:02d}": Decimal(500 - index) for index in range(1, 4)}

    class Exchange(_LPBatchQueryExchange):
        def __init__(self, initial_now: datetime) -> None:
            super().__init__(initial_now, pools)
            self.account_mode = "valid"

        def lp_account_snapshot(self) -> dict[str, object]:
            if self.account_mode == "failure":
                raise RuntimeError("account_read_failed")
            return super().lp_account_snapshot()

    exchange = Exchange(now)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"
    healthy = lp.refresh_candidates(force=True)
    assert [row["market_id"] for row in healthy["candidates"]] == [
        "market-N01", "market-N02", "market-N03",
    ]
    reads_after_healthy = len(exchange.book_token_reads)

    # The account read fails once the cached receipt ages past the shared
    # fact window: the batch is not consumed at all.
    exchange.account_mode = "failure"
    current["now"] = now + timedelta(seconds=65)
    exchange.now = current["now"]
    failed = lp.refresh_candidates(force=True)

    # Issue #157: the pool keeps its valid rows, so the snapshot stays
    # ready; the funnel notes the account gate in the payload only.
    assert failed["state"] == "ready"
    assert failed["funnel"]["stop_reason"] == "account_unavailable"
    # Continuous counters: the gate consumed nothing, so the totals still
    # reflect the healthy batch only.
    assert failed["funnel"]["checked"] == 3
    assert failed["funnel"]["passed"] == 3
    assert failed["funnel"]["batches"] == 1
    assert len(exchange.book_token_reads) == reads_after_healthy
    assert exchange.competition_reads == 0  # batches never read competition
    # The previous batches' rows are kept for read-only display.
    assert [row["market_id"] for row in failed["candidates"]] == [
        "market-N01", "market-N02", "market-N03",
    ]

    # A refresh with the account back recovers immediately.
    exchange.account_mode = "valid"
    current["now"] = now + timedelta(seconds=66)
    exchange.now = current["now"]
    recovered = lp.refresh_candidates(force=True)
    assert recovered["state"] == "ready"
    # Only three queue markets exist, so the recovered batch covers them all
    # (continuous counters: 3 from the healthy batch + 3 now).
    assert recovered["funnel"]["checked"] == 6
    assert recovered["funnel"]["passed"] == 6
    assert [row["market_id"] for row in recovered["candidates"]] == [
        "market-N01", "market-N02", "market-N03",
    ]
    assert recovered["recommendations"][0]["market_id"] == "market-N01"


def test_trial_refresh_qualifies_head_and_selects_lowest_capital(tmp_path) -> None:
    now = datetime(2026, 9, 17, 3, 30, tzinfo=UTC)
    current = {"now": now}

    class Exchange(_LPCandidateQueryExchange):
        def __init__(self, initial_now, *, scenario="budget", available="15"):
            super().__init__(
                initial_now,
                {"M01": Decimal("200"), "M02": Decimal("190")},
                available=available,
            )
            self.scenario = scenario
            self.phase = "normal"

        def lp_market_metadata(self, condition_ids, *, stop_event=None):
            del stop_event
            return {
                condition_id: {
                    "market_id": f"market-{condition_id.removeprefix('condition-')}",
                    "condition_id": condition_id,
                    "market_title": condition_id,
                    "market_url": f"https://polymarket.com/event/{condition_id}",
                    "accepting_orders": True,
                    "metadata_checked_at": self.now,
                    "fees_checked_at": self.now,
                    "tick_size": Decimal(
                        "0.0001" if self.scenario == "capital" else "0.01"
                    ),
                    "minimum_order_size": Decimal(
                        "100"
                        if self.scenario == "capital"
                        and condition_id == "condition-M01"
                        else "20"
                    ),
                    "reward_min_size": Decimal(
                        "100"
                        if self.scenario == "capital"
                        and condition_id == "condition-M01"
                        else "20"
                    ),
                    "reward_max_spread": Decimal(
                        "0.02"
                        if self.scenario == "capital"
                        and condition_id == "condition-M01"
                        else "0.10"
                    ),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "fee": Decimal("0"),
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": f"token-{condition_id}-yes"},
                        "no": {"label": "NO", "token_id": f"token-{condition_id}-no"},
                    },
                }
                for condition_id in condition_ids
            }

        def lp_order_books(self, token_ids, *, stop_event=None):
            del stop_event
            self.book_token_reads = (*self.book_token_reads, tuple(token_ids))
            # The adapter stamps the returned books after the external read;
            # candidate evaluation must use this later clock value.
            current["now"] += timedelta(seconds=1)
            self.now = current["now"]
            if self.phase == "none":
                return {}
            books = {}
            for token_id in token_ids:
                if self.phase == "unknown_yes" and token_id.endswith("-yes"):
                    continue
                if self.scenario == "capital" and token_id.endswith("-yes"):
                    best, lower = Decimal("0.90"), Decimal("0.89")
                    bid_sizes = (Decimal("10"), Decimal("100"))
                    ask_size = Decimal("100")
                    ask_price = Decimal("0.91")
                elif self.scenario == "capital":
                    best, lower = Decimal("0.09"), Decimal("0.0873")
                    bid_sizes = (Decimal("100"), Decimal("100"))
                    ask_size = Decimal("100")
                    ask_price = Decimal("0.11")
                elif token_id.endswith("-yes"):
                    best, lower = (
                        (Decimal("0.50"), Decimal("0.48"))
                        if self.phase == "tie_loss"
                        else (Decimal("0.50"), Decimal("0.495"))
                    )
                    bid_sizes = (Decimal("200"), Decimal("200"))
                    ask_size = Decimal("200")
                    ask_price = best + Decimal("0.02")
                else:
                    best, lower = (
                        (Decimal("0.50"), Decimal("0.495"))
                        if self.phase in {"tie_loss", "tie_token"}
                        else (Decimal("0.45"), Decimal("0.43"))
                    )
                    bid_sizes = (Decimal("200"), Decimal("200"))
                    ask_size = Decimal("200")
                    ask_price = best + Decimal("0.02")
                if self.phase == "tie_token":
                    best, lower = Decimal("0.50"), Decimal("0.495")
                    bid_sizes = (Decimal("200"), Decimal("200"))
                    ask_size = Decimal("200")
                    ask_price = best + Decimal("0.02")
                books[token_id] = {
                    "condition_id": token_id.removeprefix("token-").rsplit("-", 1)[0],
                    "token_id": token_id,
                    "received_at": self.now,
                    "bids": [
                        {"price": best, "size": bid_sizes[0]},
                        {"price": lower, "size": bid_sizes[1]},
                    ],
                    "asks": [{"price": ask_price, "size": ask_size}],
                }
            return books

        def lp_price_history(
            self, token_ids, *, start_ts, end_ts, fidelity=1, stop_event=None
        ):
            del fidelity, stop_event
            return {
                "state": "known",
                "history": {
                    token_id: [
                        {"t": start_ts, "p": "0.090" if self.scenario == "capital" and token_id.startswith("token-condition-M01") else "0.450"},
                        {"t": end_ts, "p": "0.090" if self.scenario == "capital" and token_id.startswith("token-condition-M01") else "0.450"},
                    ]
                    for token_id in token_ids
                },
                "unknown_token_ids": [],
            }

    capital_exchange = Exchange(now, scenario="capital", available="100")
    capital_lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "capital"),
        capital_exchange,
        clock=lambda: current["now"],
    )
    assert capital_lp.refresh_price_history()["state"] == "known"
    capital_snapshot = capital_lp.refresh_candidates(force=True)
    # Both markets are checked in the same batch and both pass.  M02's cheap
    # NO direction (0.09 × 20 = 1.80) wins the merged ranking by the
    # actual-capital optimistic upper bound (190/(24×1.80)×100 ≈ 439.8),
    # ahead of M01 (200/(24×9)×100 ≈ 92.6 with its NO at 9.00).
    capital_rows = {
        str(row["condition_id"]): row
        for row in capital_snapshot["candidates"]
    }
    capital_row = capital_rows["condition-M01"]
    assert capital_row["directions"]["YES"]["state"] == "eligible"
    assert capital_row["directions"]["NO"]["state"] == "eligible"
    assert Decimal(
        str(capital_row["directions"]["YES"]["required_capital"])
    ) == Decimal("90.00")
    assert Decimal(
        str(capital_row["directions"]["NO"]["required_capital"])
    ) == Decimal("9.00")
    capital_rank_one = capital_snapshot["recommendations"][0]
    assert capital_rank_one["condition_id"] == "condition-M02"
    assert Decimal(
        str(capital_rank_one["selected_direction"]["required_capital"])
    ) == Decimal("1.80")
    current["now"] = now

    exchange = Exchange(now, scenario="budget", available="15")
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    # One batch covers both markets: their four token books in one call.
    assert len(exchange.book_token_reads) == 1
    assert set(exchange.book_token_reads[0]) == {
        "token-condition-M01-yes",
        "token-condition-M01-no",
        "token-condition-M02-yes",
        "token-condition-M02-no",
    }
    recommendations = snapshot["recommendations"]
    assert len(recommendations) == 1
    row = recommendations[0]
    assert row["condition_id"] == "condition-M01"
    assert row["selected_direction"]["outcome"] == "NO"
    assert Decimal(str(row["selected_direction"]["required_capital"])) == Decimal(
        "9.00"
    )
    assert {
        str(candidate["condition_id"]): Decimal(str(candidate["reference_capital"]))
        for candidate in snapshot["candidates"]
    } == {
        "condition-M01": Decimal("9.00"),
        "condition-M02": Decimal("9.00"),
    }
    assert snapshot["funnel"]["budget"]["available_capital"] == "15"
    assert row["directions"]["YES"]["state"] == "eligible"
    assert row["directions"]["NO"]["state"] == "eligible"
    assert "condition-M02" not in row["condition_id"]

    # One direction can be UNKNOWN while the other remains a usable current
    # recommendation; the head stays ranked first by its actual-capital
    # upper bound even though both markets are checked in the same batch.
    exchange.phase = "unknown_yes"
    unknown_direction = lp.refresh_candidates(force=True)
    unknown_row = unknown_direction["recommendations"][0]
    assert unknown_row["selected_direction"]["outcome"] == "NO"
    assert unknown_row["directions"]["YES"]["state"] == "unknown"
    assert unknown_row["directions"]["NO"]["state"] == "eligible"
    assert len(unknown_direction["recommendations"]) == 1
    assert "condition-M02" not in unknown_direction["recommendations"][0]["condition_id"]

    # Direction choice (issue #138 round 2) follows the higher estimated
    # target-share yield first; the token id only breaks full estimate ties.
    # tie_loss: both directions need the same 20×0.50 = $10.00 trial, but
    # YES's thinner far side (0.48 bid) shrinks its competition bound C, so
    # its 5% target (≈37.95 × 0.50 = $18.98) beats NO's (≈40.97 × 0.50 =
    # $20.49) on capital yield.
    exchange.phase = "tie_loss"
    tie_loss = lp.refresh_candidates(force=True)
    assert tie_loss["recommendations"][0]["selected_direction"]["outcome"] == "YES"
    assert Decimal(
        str(tie_loss["recommendations"][0]["directions"]["YES"]["required_capital"])
    ) == Decimal("10.00")
    assert Decimal(
        str(tie_loss["recommendations"][0]["directions"]["NO"]["required_capital"])
    ) == Decimal("10.00")
    assert Decimal(
        str(
            tie_loss["recommendations"][0]["directions"]["YES"]["estimate"][
                "target_capital_usd"
            ]
        )
    ) < Decimal(
        str(
            tie_loss["recommendations"][0]["directions"]["NO"]["estimate"][
                "target_capital_usd"
            ]
        )
    )

    # Identical books give identical estimates: the token id fallback keeps
    # the NO direction ("-no" < "-yes").
    exchange.phase = "tie_token"
    tie_token = lp.refresh_candidates(force=True)
    assert tie_token["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    assert tie_token["recommendations"][0]["selected_direction"]["token_id"].endswith(
        "-no"
    )

    exchange.phase = "none"
    no_direction = lp.refresh_candidates(force=True)
    # Issue #157: the failed re-estimate keeps the stored row (marked
    # refresh_failed) instead of clearing the table.
    assert no_direction["recommendations"][0]["condition_id"] == "condition-M01"
    assert no_direction["candidates"][0]["refresh_failed"] is True
    # The batch funnel reports the two bookless markets as unknown, and the
    # continuous counters accumulate every batch call.
    assert no_direction["funnel"]["checked"] == 10
    assert no_direction["funnel"]["passed"] == 8
    assert no_direction["funnel"]["unknown"] == 2
    unknown_codes = [
        row["code"] for row in no_direction["funnel"]["reasons"]["trial"]
    ]
    assert unknown_codes.count("book_unknown") == 2


def test_trial_refresh_expiry_removal_and_next_round_recovery(tmp_path) -> None:
    now = datetime(2026, 9, 17, 4, tzinfo=UTC)

    class Exchange(_LPCandidateQueryExchange):
        def __init__(self, initial_now):
            super().__init__(initial_now, {"M01": Decimal("200")}, available="100")
            self.phase = "initial"
            self.account_reads = 0
            self.reward_reads = 0
            self.metadata_reads = 0
            self.account_mode = "valid"
            self.reward_mode = "valid"
            self.metadata_mode = "valid"
            self.book_age_seconds = 0

        def lp_account_snapshot(self):
            self.account_reads += 1
            if self.account_mode == "failure":
                raise RuntimeError("account_read_failed")
            account = super().lp_account_snapshot()
            if self.account_mode == "insufficient":
                account["balance"] = Decimal("8")
                account["allowance"] = Decimal("8")
            return account

        def lp_reward_catalog(self, *, condition_ids=None, stop_event=None):
            self.reward_reads += 1
            if self.reward_mode == "failure":
                return {
                    "state": "unknown",
                    "complete": False,
                    "checked_at": self.now,
                    "markets": ({
                        "condition_id": "condition-M01",
                        "state": "unknown",
                        "complete": False,
                        "reason_codes": ["reward_read_failed"],
                    },),
                }
            catalog = super().lp_reward_catalog(
                condition_ids=condition_ids, stop_event=stop_event
            )
            catalog["markets"] = tuple(
                {
                    **dict(market),
                    "rewards_min_size": Decimal("20"),
                    "rewards_max_spread": Decimal("10"),
                    "reward_checked_at": self.now,
                }
                for market in catalog["markets"]
            )
            return catalog

        def lp_market_metadata(self, condition_ids, *, stop_event=None):
            del stop_event
            self.metadata_reads += 1
            if self.metadata_mode == "failure":
                return {}
            return {
                condition_id: {
                    "market_id": f"market-{condition_id.removeprefix('condition-')}",
                    "condition_id": condition_id,
                    "market_title": condition_id,
                    "market_url": f"https://polymarket.com/event/{condition_id}",
                    "accepting_orders": True,
                    "metadata_checked_at": self.now,
                    "fees_checked_at": self.now,
                    "tick_size": Decimal("0.01"),
                    "minimum_order_size": Decimal("20"),
                    "fees_enabled": False,
                    "taker_fee_rate": Decimal("0"),
                    "fee_exponent": Decimal("1"),
                    "fee": Decimal("0"),
                    "outcomes": {
                        "yes": {"label": "YES", "token_id": f"token-{condition_id}-yes"},
                        "no": {"label": "NO", "token_id": f"token-{condition_id}-no"},
                    },
                }
                for condition_id in condition_ids
            }

        def lp_market_metadata_fresh(self, condition_ids, *, stop_event=None):
            return self.lp_market_metadata(condition_ids, stop_event=stop_event)

        def lp_order_books(self, token_ids, *, stop_event=None):
            del stop_event
            self.book_token_reads = (*self.book_token_reads, tuple(token_ids))
            if self.phase == "failure":
                return {}
            books = {}
            for token_id in token_ids:
                if self.phase == "reverse":
                    best = Decimal("0.44") if token_id.endswith("-yes") else Decimal("0.48")
                else:
                    best = Decimal("0.50") if token_id.endswith("-yes") else Decimal("0.45")
                books[token_id] = {
                    "condition_id": token_id.removeprefix("token-").rsplit("-", 1)[0],
                    "token_id": token_id,
                    "received_at": self.now - timedelta(seconds=self.book_age_seconds),
                    "bids": [
                        {"price": best, "size": Decimal("20")},
                        {"price": best - Decimal("0.02"), "size": Decimal("20")},
                    ],
                    "asks": [{"price": best + Decimal("0.02"), "size": Decimal("20")}],
                }
            return books

    current = {"now": now}
    exchange = Exchange(now)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path),
        exchange,
        clock=lambda: current["now"],
    )
    assert lp.refresh_price_history()["state"] == "known"
    first = lp.refresh_candidates(force=True)
    assert first["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    reads_so_far = len(exchange.book_token_reads)
    assert len(exchange.book_token_reads) == reads_so_far
    initial_reader_counts = (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
    )

    # Below the 30-second source lead (issue #146) maintenance is a pure
    # snapshot read: no source has reached the lead boundary or expiry yet.
    current["now"] = now + timedelta(seconds=29)
    exchange.now = current["now"]
    at_boundary = lp.refresh_candidate_recommendations()
    assert at_boundary["recommendations"]
    assert len(exchange.book_token_reads) == reads_so_far
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
    ) == initial_reader_counts

    current["now"] = now + timedelta(seconds=60, microseconds=1)
    exchange.now = current["now"]
    exchange.phase = "reverse"
    exchange.account_mode = "insufficient"
    reversed_snapshot = lp.refresh_candidate_recommendations()
    # Issue #157: the deterministic rejection (insufficient funds) removes
    # the row from the pool immediately instead of displaying it degraded,
    # and the attempt counts toward the maintenance backoff ladder.
    assert reversed_snapshot["recommendations"] == []
    assert reversed_snapshot["selected_results"] == []
    assert reversed_snapshot["candidate_valid_count"] == 0
    assert reversed_snapshot["maintenance_consecutive_failures"] == 1
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
    ) == tuple(value + 1 for value in initial_reader_counts)
    reads_so_far += 1
    assert len(exchange.book_token_reads) == reads_so_far
    first_maintenance_counts = (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    )
    # With the pool empty a retry is a pure snapshot read.
    retry_same_round = lp.refresh_candidate_recommendations()
    assert retry_same_round["recommendations"] == []
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    ) == first_maintenance_counts

    # While the account stays insufficient the exploration cannot restore
    # the market: its re-estimate is rejected again and the pool stays empty.
    current["now"] = now + timedelta(seconds=120, microseconds=2)
    exchange.now = current["now"]
    no_retry = lp.refresh_candidates(force=True)
    assert no_retry["recommendations"] == []
    assert no_retry["candidate_valid_count"] == 0
    reads_so_far += 1
    assert len(exchange.book_token_reads) == reads_so_far

    # A new normal scan starts a new round and can restore the candidate.
    exchange.account_mode = "valid"
    exchange.phase = "initial"
    assert lp.refresh_price_history()["state"] == "known"
    recovered_round = lp.refresh_candidates(force=True)
    assert recovered_round["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    reads_so_far += 1
    assert len(exchange.book_token_reads) == reads_so_far

    # With the whole fact bundle expired, all selected readers run once and
    # the actual returned timestamps permit the reversed direction.
    current["now"] = now + timedelta(seconds=180, microseconds=3)
    exchange.now = current["now"]
    exchange.phase = "reverse"
    refreshed = lp.refresh_candidate_recommendations()
    assert refreshed["recommendations"][0]["selected_direction"]["outcome"] == "YES"
    assert refreshed["recommendations"][0]["realtime_checked_at"] == current["now"].isoformat().replace("+00:00", "Z")
    reads_so_far += 1
    assert len(exchange.book_token_reads) == reads_so_far
    successful_refresh_counts = (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    )
    retry_success = lp.refresh_candidate_recommendations()
    assert retry_success["recommendations"][0]["selected_direction"]["outcome"] == "YES"
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    ) == successful_refresh_counts

    # Each failed fact refresh is isolated by a fresh normal round.
    current["now"] = now + timedelta(seconds=240, microseconds=2)
    exchange.now = current["now"]
    exchange.phase = "initial"
    assert lp.refresh_price_history()["state"] == "known"
    assert lp.refresh_candidates(force=True)["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    reads_so_far += 1
    assert len(exchange.book_token_reads) == reads_so_far

    current["now"] = now + timedelta(seconds=300, microseconds=3)
    exchange.now = current["now"]
    exchange.phase = "reverse"
    exchange.reward_mode = "failure"
    failed_reward = lp.refresh_candidate_recommendations()
    # Issue #157: a failed re-estimate keeps the stored row marked
    # refresh_failed until its original expiry instead of clearing it.
    assert failed_reward["recommendations"][0]["market_id"] == "market-M01"
    assert failed_reward["candidates"][0]["refresh_failed"] is True
    assert failed_reward["candidate_failed_recent_count"] == 1
    reads_so_far += 1
    assert len(exchange.book_token_reads) == reads_so_far
    failed_reward_counts = (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    )
    current["now"] = now + timedelta(seconds=360, microseconds=3)
    exchange.now = current["now"]
    # The 60-second backoff has elapsed, so the retry runs and fails again;
    # the row still keeps its last successful values.
    retried = lp.refresh_candidate_recommendations()
    assert retried["recommendations"][0]["market_id"] == "market-M01"
    assert retried["candidates"][0]["refresh_failed"] is True
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    ) == tuple(value + 1 for value in failed_reward_counts)
    reads_so_far += 1  # the retried maintenance read the batch books once

    current["now"] = now + timedelta(seconds=360, microseconds=3)
    exchange.now = current["now"]
    exchange.reward_mode = "valid"
    exchange.phase = "initial"
    assert lp.refresh_price_history()["state"] == "known"
    # Issue #157: the recovery exploration batch is empty at this clock —
    # the market sits in its per-market failure backoff after the two
    # failed maintenance attempts — so it reads nothing; the still-valid
    # stored row keeps the table populated.
    assert lp.refresh_candidates(force=True)["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    assert len(exchange.book_token_reads) == reads_so_far

    current["now"] = now + timedelta(seconds=420, microseconds=4)
    exchange.now = current["now"]
    exchange.phase = "reverse"
    exchange.metadata_mode = "failure"
    suppressed = lp.refresh_candidate_recommendations()
    # Issue #146/#157: two consecutive failures hold the maintenance for
    # 120 seconds, so this attempt is suppressed — the stored row keeps its
    # last successful values and nothing is read.
    assert suppressed["recommendations"][0]["market_id"] == "market-M01"
    assert suppressed["candidates"][0]["refresh_failed"] is True
    assert len(exchange.book_token_reads) == reads_so_far
    failed_metadata_counts = (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    )
    current["now"] = now + timedelta(seconds=480, microseconds=4)
    exchange.now = current["now"]
    # The 120-second backoff has elapsed, so the retry runs and fails
    # again; the stored row keeps its values.
    metadata_retry = lp.refresh_candidate_recommendations()
    assert metadata_retry["recommendations"][0]["market_id"] == "market-M01"
    assert metadata_retry["candidates"][0]["refresh_failed"] is True
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    ) == tuple(value + 1 for value in failed_metadata_counts)

    current["now"] = now + timedelta(seconds=480, microseconds=4)
    exchange.now = current["now"]
    exchange.metadata_mode = "valid"
    exchange.phase = "initial"
    assert lp.refresh_price_history()["state"] == "known"
    assert lp.refresh_candidates(force=True)["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    reads_so_far += 1
    assert len(exchange.book_token_reads) == reads_so_far

    # Issue #157: at +540 the +240 estimate expires (no failed refresh ever
    # extended it), so the pool is empty and the books failure cannot land
    # on any row — maintenance is a pure snapshot read.
    current["now"] = now + timedelta(seconds=540, microseconds=5)
    exchange.now = current["now"]
    exchange.phase = "failure"
    failed_books = lp.refresh_candidate_recommendations()
    assert failed_books["recommendations"] == []
    assert failed_books["candidates"] == []
    assert len(exchange.book_token_reads) == reads_so_far

    # The per-market ladder (issue #157) now holds until +780: the +300 and
    # +360 maintenance failures count 60s/120s and the +480 one pushed the
    # ceiling to 300s, so the exploration consumes nothing and the pool
    # stays honestly empty.
    current["now"] = now + timedelta(seconds=600, microseconds=5)
    exchange.now = current["now"]
    in_backoff = lp.refresh_candidates(force=True)
    assert in_backoff["recommendations"] == []
    assert in_backoff["candidate_valid_count"] == 0
    assert len(exchange.book_token_reads) == reads_so_far

    # Past +780 the exploration re-estimates the market once — the failing
    # books keep it out of the pool and re-arm the ladder.
    current["now"] = now + timedelta(seconds=780, microseconds=5)
    exchange.now = current["now"]
    books_retry = lp.refresh_candidates(force=True)
    assert books_retry["recommendations"] == []
    assert books_retry["candidate_valid_count"] == 0
    reads_so_far += 1
    assert len(exchange.book_token_reads) == reads_so_far

    # Inside the fresh 300-second per-market backoff the exploration
    # consumes nothing again.
    current["now"] = now + timedelta(seconds=780, microseconds=6)
    exchange.now = current["now"]
    exchange.phase = "initial"
    assert lp.refresh_price_history()["state"] == "known"
    recovered = lp.refresh_candidates(force=True)
    assert recovered["recommendations"] == []
    assert recovered["candidate_valid_count"] == 0
    assert len(exchange.book_token_reads) == reads_so_far

    # Past the ladder the exploration restores the market.
    current["now"] = now + timedelta(seconds=1080, microseconds=7)
    exchange.now = current["now"]
    recovered = lp.refresh_candidates(force=True)
    assert recovered["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    reads_so_far += 1
    assert len(exchange.book_token_reads) == reads_so_far

    # A source receipt 59s old at publication expires two seconds later even
    # though the publication itself is only two seconds old.  Maintenance
    # refreshes that book only, rather than treating publication time as the
    # source timestamp.
    boundary_now = now + timedelta(seconds=700)
    boundary_current = {"now": boundary_now}
    boundary_exchange = Exchange(boundary_now)
    boundary_exchange.book_age_seconds = 59
    boundary_lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "source-boundary"),
        boundary_exchange,
        clock=lambda: boundary_current["now"],
    )
    assert boundary_lp.refresh_price_history()["state"] == "known"
    boundary_first = boundary_lp.refresh_candidates(force=True)
    assert boundary_first["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    boundary_counts = (
        boundary_exchange.account_reads,
        boundary_exchange.reward_reads,
        boundary_exchange.metadata_reads,
    )
    assert len(boundary_exchange.book_token_reads) == 1
    boundary_current["now"] = boundary_now + timedelta(seconds=2)
    boundary_exchange.now = boundary_current["now"]
    boundary_refreshed = boundary_lp.refresh_candidate_recommendations()
    assert boundary_refreshed["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    assert len(boundary_exchange.book_token_reads) == 2
    assert (
        boundary_exchange.account_reads,
        boundary_exchange.reward_reads,
        boundary_exchange.metadata_reads,
    ) == boundary_counts

    # Reward facts can expire independently while account, metadata, and book
    # receipts remain fresh.  A changed reward minimum must replace the old
    # normalized guidance instead of reusing it.
    class RewardOnlyExchange(Exchange):
        def __init__(self, initial_now):
            super().__init__(initial_now)
            self.reward_minimum = Decimal("20")
            self.reward_response_checked_at = initial_now - timedelta(seconds=30)
            self.nonreward_checked_at = initial_now

        def lp_reward_catalog(self, *, condition_ids=None, stop_event=None):
            catalog = super().lp_reward_catalog(
                condition_ids=condition_ids, stop_event=stop_event
            )
            catalog["checked_at"] = self.reward_response_checked_at
            catalog["markets"] = tuple(
                {
                    **dict(market),
                    "rewards_min_size": self.reward_minimum,
                    "rewards_max_spread": Decimal("10"),
                    "reward_checked_at": self.reward_response_checked_at,
                }
                for market in catalog["markets"]
            )
            return catalog

        def lp_account_snapshot(self):
            account = super().lp_account_snapshot()
            account["checked_at"] = self.nonreward_checked_at
            return account

        def lp_market_metadata(self, condition_ids, *, stop_event=None):
            metadata = super().lp_market_metadata(
                condition_ids, stop_event=stop_event
            )
            return {
                condition_id: {
                    **dict(market),
                    "metadata_checked_at": self.nonreward_checked_at,
                    "fees_checked_at": self.nonreward_checked_at,
                }
                for condition_id, market in metadata.items()
            }

        def lp_order_books(self, token_ids, *, stop_event=None):
            books = super().lp_order_books(token_ids, stop_event=stop_event)
            return {
                token_id: {
                    **dict(book),
                    "received_at": self.nonreward_checked_at,
                    "bids": [
                        {**dict(level), "size": Decimal("100")}
                        for level in book.get("bids", ())
                    ],
                    "asks": [
                        {**dict(level), "size": Decimal("100")}
                        for level in book.get("asks", ())
                    ],
                }
                for token_id, book in books.items()
            }

    reward_only_now = now + timedelta(seconds=800)
    reward_only_current = {"now": reward_only_now}
    reward_only_exchange = RewardOnlyExchange(reward_only_now)
    reward_only_lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "reward-only"),
        reward_only_exchange,
        clock=lambda: reward_only_current["now"],
    )
    assert reward_only_lp.refresh_price_history()["state"] == "known"
    initial_reward_only = reward_only_lp.refresh_candidates(force=True)
    assert Decimal(
        str(initial_reward_only["recommendations"][0]["selected_direction"]["quantity"])
    ) == Decimal("20")
    reward_only_counts = (
        reward_only_exchange.account_reads,
        reward_only_exchange.metadata_reads,
        len(reward_only_exchange.book_token_reads),
    )

    reward_only_exchange.reward_minimum = Decimal("40")
    reward_only_exchange.reward_response_checked_at = reward_only_now + timedelta(
        seconds=30, microseconds=1
    )
    reward_only_current["now"] = reward_only_exchange.reward_response_checked_at
    reward_only_exchange.now = reward_only_current["now"]
    reward_only_refreshed = reward_only_lp.refresh_candidate_recommendations()
    reward_only_selected = reward_only_refreshed["recommendations"][0][
        "selected_direction"
    ]
    assert Decimal(str(reward_only_selected["quantity"])) == Decimal("40")
    assert Decimal(str(reward_only_selected["required_capital"])) == Decimal("18.00")
    assert reward_only_exchange.reward_reads == 2
    # Issue #146 30-second lead: every source past the lead age is refreshed
    # in the same maintenance pass, so the non-reward readers each run once
    # more alongside the expired reward read.
    assert (
        reward_only_exchange.account_reads,
        reward_only_exchange.metadata_reads,
        len(reward_only_exchange.book_token_reads),
    ) == tuple(value + 1 for value in reward_only_counts)

    # When only metadata and books expire, the cached reward spread remains a
    # raw percentage.  A fresh metadata read must normalize it once, so a
    # 0.15 quote distance is rejected against the 0.10 spread.
    class InverseExpiryExchange(Exchange):
        def __init__(self, initial_now):
            super().__init__(initial_now)
            self.initial_now = initial_now

        def lp_market_metadata(self, condition_ids, *, stop_event=None):
            metadata = super().lp_market_metadata(
                condition_ids, stop_event=stop_event
            )
            checked_at = (
                self.initial_now - timedelta(seconds=59)
                if self.phase == "initial"
                else self.now
            )
            return {
                condition_id: {
                    **dict(market),
                    "metadata_checked_at": checked_at,
                    "fees_checked_at": checked_at,
                }
                for condition_id, market in metadata.items()
            }

        def lp_order_books(self, token_ids, *, stop_event=None):
            books = super().lp_order_books(token_ids, stop_event=stop_event)
            checked_at = (
                self.initial_now - timedelta(seconds=59)
                if self.phase == "initial"
                else self.now
            )
            if self.phase != "invalid":
                return {
                    token_id: {**dict(book), "received_at": checked_at}
                    for token_id, book in books.items()
                }
            return {
                token_id: {
                    **dict(book),
                    "received_at": checked_at,
                    "bids": [
                        {"price": Decimal("0.40"), "size": Decimal("100")},
                        {"price": Decimal("0.38"), "size": Decimal("100")},
                    ],
                    "asks": [{"price": Decimal("0.70"), "size": Decimal("100")}],
                }
                for token_id, book in books.items()
            }

    inverse_now = now + timedelta(seconds=900)
    inverse_current = {"now": inverse_now}
    inverse_exchange = InverseExpiryExchange(inverse_now)
    inverse_lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path / "inverse-expiry"),
        inverse_exchange,
        clock=lambda: inverse_current["now"],
    )
    assert inverse_lp.refresh_price_history()["state"] == "known"
    inverse_first = inverse_lp.refresh_candidates(force=True)
    assert inverse_first["recommendations"]
    inverse_counts = (
        inverse_exchange.account_reads,
        inverse_exchange.reward_reads,
        inverse_exchange.metadata_reads,
        len(inverse_exchange.book_token_reads),
    )

    inverse_exchange.phase = "invalid"
    inverse_current["now"] = inverse_now + timedelta(seconds=2)
    inverse_exchange.now = inverse_current["now"]
    inverse_refreshed = inverse_lp.refresh_candidate_recommendations()
    # Issue #157: the deterministic rejection (reward distance invalid on
    # both directions) removes the row from the pool instead of showing it
    # degraded; the reads that produced the verdict still ran.
    assert inverse_refreshed["recommendations"] == []
    assert inverse_refreshed["selected_results"] == []
    assert inverse_refreshed["candidate_valid_count"] == 0
    assert (
        inverse_exchange.account_reads,
        inverse_exchange.reward_reads,
        inverse_exchange.metadata_reads,
        len(inverse_exchange.book_token_reads),
    ) == (
        inverse_counts[0],
        inverse_counts[1],
        inverse_counts[2] + 1,
        inverse_counts[3] + 1,
    )


def test_refresh_candidates_excludes_reference_capital_over_available(tmp_path) -> None:
    """B3: available 480; a market needing 1000 × 0.505 = 505 never shows."""

    now = datetime(2026, 9, 17, 4, tzinfo=UTC)
    exchange = _LPCandidateQueryExchange(
        now,
        {"M01": Decimal("120"), "BIG": Decimal("500")},
        available="480",
        min_sizes={"BIG": "1000"},
    )
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    prepared = lp.refresh_price_history()
    assert prepared["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    candidates = snapshot["candidates"]
    assert [row["condition_id"] for row in candidates] == ["condition-M01"]
    assert snapshot["selected_market_ids"] == ["market-M01"]
    funnel = snapshot["funnel"]
    assert funnel["excluded"]["over_available"] == 1
    assert funnel["normal_queue_count"] == 1
    assert funnel["backup_queue_count"] == 0


def test_refresh_candidates_never_calls_price_history_endpoint(tmp_path) -> None:
    """B4: the candidate refresh consumes cached summaries only."""

    now = datetime(2026, 9, 17, 5, tzinfo=UTC)
    exchange = _LPCandidateQueryExchange(
        now, {"M01": Decimal("120"), "M02": Decimal("90")}
    )
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    prepared = lp.refresh_price_history()
    assert prepared["state"] == "known"
    assert exchange.history_calls > 0

    exchange.history_calls = 0
    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    assert len(snapshot["candidates"]) == 2
    assert exchange.history_calls == 0


def test_refresh_candidates_keeps_valid_markets_when_metadata_missing(tmp_path) -> None:
    """B5: one market without metadata → incomplete scan, others still queue."""

    now = datetime(2026, 9, 17, 6, tzinfo=UTC)
    exchange = _LPCandidateQueryExchange(
        now, {"M01": Decimal("120"), "M02": Decimal("90")}
    )
    original_metadata = exchange.lp_market_metadata

    def metadata_without_m02(condition_ids, *, stop_event=None):
        return original_metadata(
            tuple(cid for cid in condition_ids if cid != "condition-M02"),
            stop_event=stop_event,
        )

    exchange.lp_market_metadata = metadata_without_m02
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    lp.refresh_price_history()

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "incomplete"
    assert snapshot["complete"] is False
    assert "condition-M02" in snapshot["missing_metadata_condition_ids"]
    candidates = snapshot["candidates"]
    assert [row["condition_id"] for row in candidates] == ["condition-M01"]
    assert snapshot["funnel"]["read"] == 1
    assert snapshot["funnel"]["trial"] == 1


class _LPYieldBooksExchange(_LPBatchQueryExchange):
    """YES/NO fixture for the 5% target-share yield ordering contracts.

    ``books_by_token`` overrides the default book per token id (values are
    (bids, asks) (price, size) pair lists); unknown tokens get the default
    0.34/0.33/0.36 book.  ``high_reference`` suffixes receive 0.885/0.890
    histories so their optimistic queue upper bound sinks below the rest
    (ref capital 20×0.89) — they are checked last regardless of pool.
    """

    def __init__(
        self,
        now: datetime,
        pools: dict[str, Decimal],
        *,
        spread: str = "0.10",
        high_reference: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(now, pools)
        self.override_spread = Decimal(spread)
        self.high_reference = high_reference
        self.books_by_token: dict[
            str, tuple[list[tuple[str, str]], list[tuple[str, str]]]
        ] = {}
        self.fail_book_reads = False

    def lp_market_metadata(self, condition_ids, *, stop_event=None):
        metadata = super().lp_market_metadata(condition_ids, stop_event=stop_event)
        return {
            condition_id: {
                **dict(row),
                "reward_max_spread": self.override_spread,
            }
            for condition_id, row in metadata.items()
        }

    def lp_price_history(
        self, token_ids, *, start_ts, end_ts, fidelity=1, stop_event=None
    ):
        result = super().lp_price_history(
            token_ids,
            start_ts=start_ts,
            end_ts=end_ts,
            fidelity=fidelity,
            stop_event=stop_event,
        )
        for token_id in tuple(token_ids):
            if self._suffix(token_id) in self.high_reference:
                result["history"][token_id] = [
                    {"t": start_ts, "p": "0.885"},
                    {"t": end_ts, "p": "0.890"},
                ]
        return result

    def lp_order_books(self, token_ids, *, stop_event=None):
        del stop_event
        self.book_token_reads = (*self.book_token_reads, tuple(token_ids))
        if self.fail_book_reads:
            raise RuntimeError("book read failed")
        books = {}
        for token_id in tuple(token_ids):
            if token_id in self.omit_tokens:
                continue
            spec = self.books_by_token.get(token_id)
            if spec is None:
                best = Decimal("0.34")
                bids = [
                    ("0.34", "100"),
                    ("0.33", "100"),
                ]
                asks = [("0.36", "100")]
            else:
                bids, asks = spec
            books[token_id] = {
                "condition_id": token_id.removeprefix("token-").rsplit("-", 1)[0],
                "token_id": token_id,
                "received_at": self.now,
                "bids": [
                    {"price": Decimal(price), "size": Decimal(size)}
                    for price, size in bids
                ],
                "asks": [
                    {"price": Decimal(price), "size": Decimal(size)}
                    for price, size in asks
                ],
            }
        return books


def test_scan_checks_past_ten_passers_and_ranks_by_target_share_yield(tmp_path) -> None:
    """C1: the scan no longer stops at ten passers; the 11th checked market
    with the higher estimated yield enters first place with estimate fields.

    Independent arithmetic: identical books give every market target
    q_display 20.00 shares (q ≈ 19.95 < min 20) at 20×0.34 = $6.80, so
    yields track pools; M11's pool 150 gives hourly $150×5%/24 = $0.3125 and
    yield 0.3125/6.80×100 = 4.595588…% — above the others' pool-100 figure.
    """

    now = datetime(2026, 9, 20, 4, tzinfo=UTC)
    pools = {f"M{index:02d}": Decimal(100) for index in range(1, 11)}
    pools["M11"] = Decimal(150)
    # The high reference price (20×0.89) sinks M11's optimistic queue upper
    # bound below the rest, so it is checked last — after ten already passed.
    exchange = _LPYieldBooksExchange(now, pools, high_reference=frozenset({"M11"}))
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    assert lp.refresh_price_history()["state"] == "known"

    lp.refresh_candidates(force=True)
    # Issue #157: the eleventh market rolls in the second exploration batch
    # (the never-tried head plus the nine oldest-tried markets).
    snapshot = lp.refresh_candidates(force=True)

    funnel = snapshot["funnel"]
    assert funnel["checked"] == 20
    assert funnel["passed"] == 20
    assert funnel["gap_reason"] is None
    candidates = snapshot["candidates"]
    head = candidates[0]
    assert head["market_id"] == "market-M11"
    assert head["estimate_state"] == "known"
    assert Decimal(str(head["estimated_target_quantity"])) == Decimal("20.00")
    assert Decimal(str(head["estimated_target_capital_usd"])) == Decimal("6.80")
    assert Decimal(str(head["estimated_hourly_reward_usd"])) == Decimal("0.3125")
    assert Decimal(str(head["estimated_yield_pct_per_hour"])) == Decimal("4.595588")
    assert Decimal(str(head["estimated_yield_raw"])) > Decimal("4.595588")
    assert head["estimate_checked_at"]
    assert "realtime_query_rate_upper_bound" not in head
    # M10 is trimmed: its yield ties with M01..M09 but its competition value
    # is the highest, and the eleven-passer table keeps the best ten.
    assert [row["market_id"] for row in candidates[1:]] == [
        f"market-M{index:02d}" for index in range(1, 10)
    ]


def test_scan_checks_fifty_markets_then_publishes_best_ten(tmp_path) -> None:
    """C2: 60 queued markets — the scan checks at most 50, then publishes
    the best ten without claiming a passer shortfall."""

    now = datetime(2026, 9, 20, 5, tzinfo=UTC)
    current = {"now": now}
    pools = {f"M{index:02d}": Decimal(index) for index in range(1, 61)}
    exchange = _LPYieldBooksExchange(now, pools)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: current["now"])
    assert lp.refresh_price_history()["state"] == "known"

    for _ in range(6):
        lp.refresh_candidates(force=True)
        current["now"] = current["now"] + timedelta(seconds=5)
        exchange.now = current["now"]
    snapshot = lp.candidate_snapshot()

    funnel = snapshot["funnel"]
    assert funnel["checked"] == 60
    assert funnel["passed"] == 60
    assert funnel["gap_reason"] is None
    assert funnel["unchecked"] == 0
    assert funnel["batches"] == 6
    assert len(exchange.book_token_reads) == 6
    all_tokens = [
        token for batch in exchange.book_token_reads for token in batch
    ]
    assert len(all_tokens) == 120
    assert len(set(all_tokens)) == 120
    candidates = snapshot["candidates"]
    assert [row["market_id"] for row in candidates] == [
        f"market-M{index}" for index in range(60, 50, -1)
    ]


def test_direction_selection_follows_target_share_yield(tmp_path) -> None:
    """C3a: YES/NO direction choice follows the higher estimated target-share
    yield, not the smaller minimum-trial capital.

    YES quotes 0.20 on a very deep book: min trial 20×0.20 = $4.00 wins the
    old capital rule, but its 5% target quantity is ~1800 shares ($360) so
    its yield is tiny.  NO quotes 0.80 with min-dominated target 20.00
    shares ($16.00): yield 100×5%/24/16×100 = 1.302083% wins the new rule.
    """

    now = datetime(2026, 9, 20, 6, tzinfo=UTC)
    exchange = _LPYieldBooksExchange(now, {"D01": Decimal(100)}, spread="0.03")
    exchange.books_by_token = {
        "token-condition-D01-yes": (
            [("0.20", "11400"), ("0.19", "100")],
            [("0.22", "11400")],
        ),
        "token-condition-D01-no": (
            [("0.80", "20"), ("0.79", "100")],
            [("0.82", "20")],
        ),
    }
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    assert lp.refresh_price_history()["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    row = snapshot["candidates"][0]
    selected = row["selected_direction"]
    assert selected["outcome"] == "NO"
    assert Decimal(str(selected["price"])) == Decimal("0.80")
    assert Decimal(str(row["realtime_price"])) == Decimal("0.80")
    assert Decimal(str(row["estimated_target_quantity"])) == Decimal("20.00")
    assert Decimal(str(row["estimated_target_capital_usd"])) == Decimal("16.00")
    assert row["directions"]["YES"]["eligible"] is True
    assert row["directions"]["NO"]["eligible"] is True
    assert snapshot["recommendations"][0]["market_id"] == "market-D01"


def test_maintenance_refreshes_all_rows_and_reranks_by_new_yield(tmp_path) -> None:
    """C3b: the 60-second maintenance refreshes every published row with one
    batch book read, re-ranks the whole table by the new estimates, moves the
    current recommendation with the new head, keeps failed rows' old values
    marked not-updated behind the refreshed rows, and degrades the whole
    table when the round fails."""

    now = datetime(2026, 9, 20, 7, tzinfo=UTC)
    current = {"now": now}
    pools = {"M01": Decimal(100), "M02": Decimal(90), "M03": Decimal(80)}
    exchange = _LPYieldBooksExchange(now, pools)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: current["now"])
    assert lp.refresh_price_history()["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)
    assert [row["market_id"] for row in snapshot["candidates"]] == [
        "market-M01", "market-M02", "market-M03",
    ]
    reads_after_scan = len(exchange.book_token_reads)

    # Deepen M01's book: its target quantity explodes (capital ≈ $613) and
    # its yield collapses below M02/M03, so the whole table re-ranks.
    deep = ([("0.34", "11400"), ("0.33", "100")], [("0.36", "11400")])
    exchange.books_by_token["token-condition-M01-yes"] = deep
    exchange.books_by_token["token-condition-M01-no"] = deep
    current["now"] = now + timedelta(seconds=65)
    exchange.now = current["now"]
    maintained = lp.refresh_candidate_recommendations()

    new_reads = exchange.book_token_reads[reads_after_scan:]
    assert len(new_reads) == 1
    assert len(new_reads[0]) == 6
    reranked = maintained["candidates"]
    assert [row["market_id"] for row in reranked] == [
        "market-M02", "market-M03", "market-M01",
    ]
    assert maintained["recommendations"][0]["market_id"] == "market-M02"
    assert maintained["recommendations"][0]["condition_id"] == reranked[0][
        "condition_id"
    ]
    deep_row = reranked[2]
    assert deep_row["market_id"] == "market-M01"
    assert Decimal(str(deep_row["estimated_target_capital_usd"])) > Decimal("600")
    assert deep_row["estimate_updated"] is True
    # Every refreshed row carries the maintenance clock on its estimate.
    for row in reranked:
        assert row["estimate_checked_at"] == row["realtime_checked_at"]

    # A per-row book failure keeps the old estimate, marks the row
    # not-updated, and ranks it behind every refreshed row.
    reads_before_failure = len(exchange.book_token_reads)
    exchange.omit_tokens = frozenset({
        "token-condition-M03-yes",
        "token-condition-M03-no",
    })
    current["now"] = current["now"] + timedelta(seconds=65)
    exchange.now = current["now"]
    failed_row_round = lp.refresh_candidate_recommendations()

    assert len(exchange.book_token_reads) == reads_before_failure + 1
    rows = failed_row_round["candidates"]
    # Issue #157: the failed row keeps its values and its OLD estimate time,
    # so it ranks by its stored yield — above the collapsed M01.
    assert [row["market_id"] for row in rows] == [
        "market-M02", "market-M03", "market-M01",
    ]
    stale_row = rows[1]
    assert stale_row["market_id"] == "market-M03"
    assert stale_row["refresh_failed"] is True
    assert Decimal(str(stale_row["estimated_yield_pct_per_hour"])) == Decimal("2.450980")
    assert Decimal(str(stale_row["estimated_target_capital_usd"])) == Decimal("6.80")
    assert failed_row_round["recommendations"][0]["market_id"] == "market-M02"

    # A whole-round book failure keeps every stored row marked
    # refresh_failed with frozen values; the head still auto-fills from the
    # best valid row.
    exchange.fail_book_reads = True
    current["now"] = current["now"] + timedelta(seconds=65)
    exchange.now = current["now"]
    degraded_round = lp.refresh_candidate_recommendations()

    degraded_rows = degraded_round["candidates"]
    assert [row["market_id"] for row in degraded_rows] == [
        "market-M02", "market-M03", "market-M01",
    ]
    assert all(row["refresh_failed"] is True for row in degraded_rows)
    assert degraded_round["recommendations"][0]["market_id"] == "market-M02"
def test_status_returns_none_when_only_reserved_manual_anchor(tmp_path) -> None:
    """R2: store 仅含保留锚点会话时,status() 不把锚点当最新会话,报 none。"""

    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        LP_RESERVED_MANUAL_SESSION_ID,
        "manual-anchor",
        state="complete",
        payload={"context": "manual_cancel_audit"},
    )
    service = PolymarketLPService(store, _Exchange(), clock=lambda: datetime.now(UTC))

    assert service.status() == {"state": "none", "session_id": None}


def test_status_and_stop_unknown_session_id_do_not_touch_active(tmp_path) -> None:
    """Issue 165 护栏：status/stop 传不存在的 session_id 返回 none 载荷，
    绝不回退到活动会话，也不触碰活动会话及其订单。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-unknown-id"
    )
    session_id = str(started["session_id"])

    none_payload = {"state": "none", "session_id": None}
    assert service.status("nope") == none_payload
    assert service.stop("nope") == none_payload

    active = store.lp_session(session_id)
    assert active is not None
    assert active["state"] == "entry_open"
    assert not active.get("entry_cancel_requested")
    assert exchange.cancels == []


def test_stop_historical_session_leaves_active_untouched(tmp_path) -> None:
    """Issue 165 护栏：stop 显式指定完结会话只回读其终态载荷，不撤单、
    不触碰活动会话；默认 stop(None) 仍作用于活动会话。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-stop-history"
    )
    active_id = str(started["session_id"])
    store.lp_create_session(
        "lp-history",
        "lp-history-key",
        state="complete",
        payload={"market_id": "market-history", "outcome": "YES"},
    )
    historical = store.lp_session("lp-history")
    assert historical is not None
    historical_updated_at = historical["updated_at"]

    stopped = service.stop("lp-history")
    assert stopped["state"] == "complete"
    assert stopped == service.status("lp-history")
    after = store.lp_session("lp-history")
    assert after is not None
    assert after["updated_at"] == historical_updated_at

    active = store.lp_session(active_id)
    assert active is not None
    assert active["state"] == "entry_open"
    assert not active.get("entry_cancel_requested")
    assert exchange.cancels == []

    # 默认行为兼容：stop(None) 仍作用于唯一活动会话。
    default_stop = service.stop()
    assert default_stop["state"] == "review"
    assert default_stop["stop_requested"] is True
    assert exchange.cancels == ["order-1"]
    stopped_active = store.lp_session(active_id)
    assert stopped_active is not None
    assert stopped_active["state"] == "review"
    assert stopped_active["stop_requested"] is True


def test_daily_report_session_excludes_reserved_manual_anchor(tmp_path) -> None:
    """R3: 日报会话装配对保留锚点会话返回 relevant=False(调用方跳过)。"""

    now = datetime.now(UTC)
    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        LP_RESERVED_MANUAL_SESSION_ID,
        "manual-anchor",
        state="complete",
        payload={"context": "manual_cancel_audit"},
    )
    (anchor_row,) = store.lp_sessions()

    _report, relevant = PolymarketLPService._daily_report_session(
        anchor_row,
        period_start=now - timedelta(hours=1),
        period_end=now + timedelta(hours=1),
        generated_at=now,
    )
    assert relevant is False


# ---- Issue 152: LP BUY 队列位置保护（Seam 2 登记与提交） ----


def _queue_book_snapshot(now: datetime, bid_size: Decimal) -> dict[str, object]:
    base = _snapshot(now)
    base["book"] = {
        "timestamp": now,
        "received_at": now,
        "source_timestamp": "2026-09-14T11:59:59Z",
        "hash": "book-hash-queue-1",
        "asks": [{"price": Decimal("0.31"), "size": Decimal("100")}],
        "bids": [
            {"price": Decimal("0.30"), "size": bid_size},
            {"price": Decimal("0.29"), "size": Decimal("100")},
        ],
    }
    return base


def _queue_tick_snapshot(now: datetime, bid_size: Decimal) -> dict[str, object]:
    snapshot = _queue_book_snapshot(now, bid_size)
    snapshot["orders"] = [
        {
            "order_id": "order-1",
            "token_id": "0x" + "1" * 64,
            "side": "BUY",
            "status": "LIVE",
            "price": Decimal("0.30"),
            "original_size": Decimal("10"),
            "size_matched": Decimal("0"),
            "remaining_size": Decimal("10"),
        }
    ]
    return snapshot


def test_queue_protection_registered_at_submit_boundary(tmp_path) -> None:
    """T10: 正常 start 落基线：session payload、entry action、盘口档、preview 估算。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _queue_book_snapshot(now, Decimal("8000"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)

    request = _request(now)
    preview = service.preview(request)
    estimate = preview["queue_protection_estimate"]
    assert Decimal(str(estimate["baseline_front"])) == Decimal("8000")
    assert Decimal(str(estimate["projected_ratio"])) == Decimal("8000") / Decimal("8010")

    started = service.start(str(preview["preview_id"]), "lp-queue-registered-1")
    session_id = str(started["session_id"])
    session = store.lp_session(session_id)
    protection = session["queue_protection"]
    assert Decimal(str(protection["baseline_front"])) == Decimal("8000")
    assert Decimal(str(protection["baseline_price"])) == Decimal("0.30")
    assert protection["baseline_book_received_at"] is not None
    assert protection["baseline_source_timestamp"] == "2026-09-14T11:59:59Z"
    assert protection["baseline_book_hash"] == "book-hash-queue-1"
    assert protection["baseline_version"] == 1
    assert Decimal(str(protection["threshold"])) == Decimal("0.5")
    assert protection["data_failures"] == 0
    assert protection["state"] == "registered"
    assert protection["notification_sent"] is False
    assert protection["cancel_scope"] == "own_buys_at_level"

    (entry_action,) = [
        action
        for action in store.lp_actions(session_id)
        if action["action_key"].endswith("entry-submit")
    ]
    assert entry_action["state"] == "accepted"
    assert entry_action["submit_requested_at"]
    assert entry_action["submit_receipt_at"]
    baseline_summary = entry_action["queue_protection_baseline"]
    assert Decimal(str(baseline_summary["baseline_front"])) == Decimal("8000")
    assert Decimal(str(baseline_summary["baseline_price"])) == Decimal("0.30")

    condition_id = request["condition_id"]
    token_id = request["token_id"]
    samples = store.lp_book_samples(
        condition_id,
        token_id,
        since=now - timedelta(minutes=1),
        until=now + timedelta(minutes=1),
    )
    assert len(samples) == 1
    assert Decimal(str(samples[0]["best_bid_price"])) == Decimal("0.30")
    assert Decimal(str(samples[0]["best_bid_size"])) == Decimal("8000")

    # 空档位：基线为 0、比例 0，不拒绝不加门槛。
    empty_level_exchange = _Exchange()
    empty_book = _snapshot(now)
    empty_level_exchange.snapshot_value = empty_book
    empty_service = PolymarketLPService(
        store, empty_level_exchange, clock=lambda: now
    )
    empty_preview = empty_service.preview(request)
    empty_estimate = empty_preview["queue_protection_estimate"]
    assert Decimal(str(empty_estimate["baseline_front"])) == Decimal("0")
    assert Decimal(str(empty_estimate["projected_ratio"])) == Decimal("0")


def test_queue_protection_stale_book_keeps_existing_rejection(tmp_path) -> None:
    """T11: 盘口过期沿用现有拒绝路径：不建会话不下单。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _queue_book_snapshot(now, Decimal("8000"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)

    preview = service.preview(_request(now))
    exchange.snapshots = [
        _queue_book_snapshot(now - timedelta(seconds=30), Decimal("8000"))
    ]
    result = service.start(str(preview["preview_id"]), "lp-queue-stale-1")

    assert result["state"] == "rejected"
    assert result["reason"] == "book_freshness_stale"
    assert store.lp_active_session() is None
    assert exchange.posts == []


def test_queue_protection_baseline_survives_restart(tmp_path) -> None:
    """T12: 同 store 新建实例后基线仍在，tick 评估用持久 baseline。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _queue_book_snapshot(now, Decimal("8000"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview(_request(now))
    started = service.start(str(preview["preview_id"]), "lp-queue-restart-1")
    session_id = str(started["session_id"])

    restarted = PolymarketLPService(store, exchange, clock=lambda: now)
    recovered = restarted.status(session_id)
    protection = recovered["queue_protection"]
    assert Decimal(str(protection["baseline_front"])) == Decimal("8000")
    assert Decimal(str(protection["baseline_price"])) == Decimal("0.30")

    exchange.snapshot_value = _queue_tick_snapshot(now, Decimal("10000"))
    ticked = restarted.tick()
    after = ticked["queue_protection"]
    assert after["state"] == "monitoring"
    assert Decimal(str(after["level_total"])) == Decimal("10000")
    assert Decimal(str(after["front_estimate"])) == Decimal("8000")
    assert Decimal(str(after["ratio"])) == Decimal("0.80")
    assert Decimal(str(after["baseline_front"])) == Decimal("8000")
    assert exchange.cancels == []


# ---- Issue 158: 提交时刻买一校验（start 复核段） ----


def _queue_book_moved_bid_snapshot(now: datetime, bid_size: Decimal) -> dict[str, object]:
    """Same shape as _queue_book_snapshot but the best bid dropped to 0.29."""

    base = _snapshot(now)
    base["book"] = {
        "timestamp": now,
        "received_at": now,
        "source_timestamp": "2026-09-14T11:59:59Z",
        "hash": "book-hash-queue-moved",
        "asks": [{"price": Decimal("0.31"), "size": Decimal("100")}],
        "bids": [
            {"price": Decimal("0.29"), "size": bid_size},
            {"price": Decimal("0.28"), "size": Decimal("100")},
        ],
    }
    return base


def test_start_rejects_when_best_bid_moved_since_preview(tmp_path) -> None:
    """T10: 预检后买一变化 → best_bid_changed 拒；无会话创建、无交易所 POST。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _queue_book_snapshot(now, Decimal("8000"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)

    preview = service.preview(_request(now))
    assert preview["state"] == "previewed"
    assert Decimal(str(preview["preflight"]["best_bid"])) == Decimal("0.30")

    exchange.snapshot_value = _queue_book_moved_bid_snapshot(now, Decimal("8000"))
    result = service.start(str(preview["preview_id"]), "lp-bid-moved-1")

    assert result == {"state": "rejected", "reason": "best_bid_changed"}
    assert store.lp_active_session() is None
    assert store.lp_preview(str(preview["preview_id"]))["consumed_at"] is None
    assert exchange.posts == []


def test_start_accepts_when_best_bid_unchanged(tmp_path) -> None:
    """T11: 盘口未动 → start 正常建会话（回归：买一校验不误伤）。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _queue_book_snapshot(now, Decimal("8000"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)

    preview = service.preview(_request(now))
    started = service.start(str(preview["preview_id"]), "lp-bid-same-1")

    assert started["state"] == "entry_open"
    assert len(exchange.posts) == 1
    assert store.lp_active_session() is not None


def test_start_idempotent_retry_precedes_best_bid_check(tmp_path) -> None:
    """T12: 成功后同 key 重试（买一已再变）→ 返既有会话，无第二单。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _queue_book_snapshot(now, Decimal("8000"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)

    preview = service.preview(_request(now))
    started = service.start(str(preview["preview_id"]), "lp-bid-retry-1")
    assert started["state"] == "entry_open"
    session_id = str(started["session_id"])
    assert len(exchange.posts) == 1

    exchange.snapshot_value = _queue_book_moved_bid_snapshot(now, Decimal("9000"))
    retried = service.start(str(preview["preview_id"]), "lp-bid-retry-1")

    assert str(retried["session_id"]) == session_id
    assert retried["state"] == "entry_open"
    assert len(exchange.posts) == 1
    assert store.lp_session_by_idempotency("lp-bid-retry-1") is not None


# ---- Issue 158: estimated_target_quantity 透传（加量 5% 默认量，informational only） ----


def test_entry_estimated_target_quantity_flows_to_session_and_status(tmp_path) -> None:
    """T18: preview 带 estimated_target_quantity → 会话 payload 存原值，lp_status 投影透出。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _queue_book_snapshot(now, Decimal("8000"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)

    preview = service.preview({**_request(now), "estimated_target_quantity": "90"})
    assert preview["state"] == "previewed"
    started = service.start(str(preview["preview_id"]), "lp-target-qty-1")

    assert started["state"] == "entry_open"
    stored = store.lp_session(str(started["session_id"]))
    assert stored is not None
    assert Decimal(str(stored["estimated_target_quantity"])) == Decimal("90")
    status = service.status(str(started["session_id"]))
    assert Decimal(str(status["estimated_target_quantity"])) == Decimal("90")


def test_entry_without_estimated_target_quantity_projects_null(tmp_path) -> None:
    """T18: preview 不带该字段 → start 正常建会话，投影 estimated_target_quantity=null 不炸。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _queue_book_snapshot(now, Decimal("8000"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)

    preview = service.preview(_request(now))
    assert preview["state"] == "previewed"
    started = service.start(str(preview["preview_id"]), "lp-no-target-qty-1")

    assert started["state"] == "entry_open"
    status = service.status(str(started["session_id"]))
    assert status["estimated_target_quantity"] is None


# ---- Issue 158: 候选行 review_at 投影（下次北京 08:00 → UTC） ----


def _candidate_row_review_at(now_utc: datetime, tmp_path) -> object:
    exchange = _LPCandidateQueryExchange(now_utc, {"M01": Decimal("200")})
    service = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now_utc
    )
    prepared = service.refresh_price_history()
    assert prepared["state"] == "known"
    snapshot = service.refresh_candidates(force=True)
    assert snapshot["state"] == "ready"
    assert snapshot["candidates"]
    return snapshot["candidates"][0].get("review_at")


@pytest.mark.parametrize(
    ("now_utc", "expected_utc"),
    [
        (datetime(2026, 9, 22, 2, 0, tzinfo=UTC), datetime(2026, 9, 23, 0, 0, tzinfo=UTC)),
        (datetime(2026, 9, 21, 23, 30, tzinfo=UTC), datetime(2026, 9, 22, 0, 0, tzinfo=UTC)),
    ],
)
def test_candidate_rows_project_next_review_at(
    tmp_path, now_utc, expected_utc
) -> None:
    """T1: 行内 review_at = 下次北京 08:00；断言解析回 datetime 比瞬间。"""

    raw = _candidate_row_review_at(now_utc, tmp_path)
    assert isinstance(raw, str) and raw
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    assert parsed == expected_utc


# ---- Issue 158: 加量服务面（augment preview/start） ----


def _augment_running_service(
    tmp_path, now: datetime, *, key: str, clock_cell: list | None = None
):
    """Start one registered entry session: quantity 120 at price 0.30."""

    exchange = _Exchange()
    exchange.snapshot_value = _queue_book_snapshot(now, Decimal("120"))
    store = PredictionArbitrageStore(tmp_path)
    current = clock_cell if clock_cell is not None else [now]
    service = PolymarketLPService(store, exchange, clock=lambda: current[0])
    request = {**_request(now), "quantity": Decimal("120")}
    preview = service.preview(request)
    started = service.start(str(preview["preview_id"]), key)
    assert started["state"] == "entry_open"
    return store, exchange, service, started


def _augment_preview_snapshot(
    now: datetime, *, level: Decimal, own_original: Decimal
) -> dict[str, object]:
    """Fresh book at the entry price plus the session's resting entry BUY."""

    snapshot = _queue_book_snapshot(now, level)
    snapshot["account"]["open_orders"] = [
        _queue_receipt("order-1", original=own_original, matched="0")
    ]
    return snapshot


def _augment_preview(
    service: PolymarketLPService,
    session_id: str,
    quantity: object,
    price: object | None = None,
):
    request: dict[str, object] = {"session_id": session_id, "quantity": quantity}
    if price is not None:
        request["price"] = price
    return service.augment_preview(request)


def test_augment_preview_projects_merged_queue_ratio(tmp_path) -> None:
    """T13（#167 改写）：追加价=新价位 → 预检估算按该价档位深度（front/(front+qty)）；
    同价（组价，入场单仍在挂）预检 → price_level_active 拒（#158 合并重锚语义退役）。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _augment_running_service(
        tmp_path, now, key="lp-aug-t13"
    )
    session_id = str(started["session_id"])
    exchange.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )

    preview = _augment_preview(service, session_id, 90, price="0.29")
    assert preview["state"] == "previewed"
    assert Decimal(str(preview["price"])) == Decimal("0.29")
    assert Decimal(str(preview["request"]["quantity"])) == Decimal("90")
    estimate = preview["queue_protection_estimate"]
    # 新价位口径：front = 该价档位他人量 100，A = 100/(100+90)。
    assert Decimal(str(estimate["baseline_front"])) == Decimal("100")
    assert Decimal(str(estimate["projected_ratio"])) == Decimal("100") / Decimal("190")

    bigger = _augment_preview(service, session_id, 160, price="0.29")
    assert bigger["state"] == "previewed"
    assert Decimal(
        str(bigger["queue_protection_estimate"]["projected_ratio"])
    ) == Decimal("100") / Decimal("260")

    # 同价补量：入场单仍在挂 → price_level_active。
    same = _augment_preview(service, session_id, 90)
    assert same == {"state": "rejected", "reason": "price_level_active"}


def test_augment_submit_registers_order_into_session(tmp_path) -> None:
    """T14（#167 改写）：恰一单 post-only BUY@新价位；两价位桶落载荷+审计；不新建第二会话。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _augment_running_service(
        tmp_path, now, key="lp-aug-t14"
    )
    session_id = str(started["session_id"])
    exchange.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    preview = _augment_preview(service, session_id, 90, price="0.29")
    assert preview["state"] == "previewed"

    result = service.augment(session_id, str(preview["preview_id"]), "lp-aug-key-1")

    assert result["state"] == "entry_open"
    assert str(result["augment_order_id"]) == "order-2"
    assert Decimal(str(result["augment_quantity"])) == Decimal("90")
    assert len(exchange.posts) == 2
    posted = exchange.posts[1]
    assert posted["side"] == "BUY"
    assert posted["post_only"] is True
    assert Decimal(str(posted["price"])) == Decimal("0.29")
    assert Decimal(str(posted["quantity"])) == Decimal("90")
    session = store.lp_session(session_id)
    assert session["augment_order_ids"] == ["order-2"]
    protection = session["queue_protection"]
    assert protection["version"] == 2
    assert set(protection["levels"]) == {"0.30", "0.29"}
    assert str(protection["levels"]["0.29"]["order_id"]) == "order-2"
    assert Decimal(str(protection["levels"]["0.29"]["baseline_front"])) == Decimal("100")
    active = store.lp_active_session()
    assert active is not None and str(active["session_id"]) == session_id
    (receipt,) = [
        action
        for action in store.lp_actions(session_id)
        if action["action_key"].endswith("augment-submit:order-2")
    ]
    assert receipt["state"] == "accepted"
    assert receipt["order_id"] == "order-2"
    assert receipt["role"] == "augment"


def test_augment_rejection_matrix(tmp_path) -> None:
    """T15: 无活动会话 / 买一变 / 预检过期 / 同 key 重试不重复挂单。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

    # a) 无活动会话：complete 会话与未知会话都拒绝。
    store_a, exchange_a, service_a, started_a = _augment_running_service(
        tmp_path / "a", now, key="lp-aug-t15-a"
    )
    session_a = str(started_a["session_id"])
    store_a.lp_update_session(session_a, state="complete")
    rejected_a = _augment_preview(service_a, session_a, 90)
    assert rejected_a == {"state": "rejected", "reason": "session_not_active"}
    assert _augment_preview(service_a, "missing-session", 90) == {
        "state": "rejected",
        "reason": "session_not_found",
    }
    assert len(exchange_a.posts) == 1  # 仅既有入场单，无加量单

    # b) 买一变化：augment 复核段拒绝，凭证未消费、无下单。
    cell_b = [now]
    store_b, exchange_b, service_b, started_b = _augment_running_service(
        tmp_path / "b", now, key="lp-aug-t15-b", clock_cell=cell_b
    )
    session_b = str(started_b["session_id"])
    exchange_b.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    preview_b = _augment_preview(service_b, session_b, 90, price="0.29")
    assert preview_b["state"] == "previewed"
    exchange_b.snapshot_value = _queue_book_moved_bid_snapshot(now, Decimal("8000"))
    moved = service_b.augment(session_b, str(preview_b["preview_id"]), "lp-aug-key-b")
    assert moved == {"state": "rejected", "reason": "best_bid_changed"}
    assert store_b.lp_preview(str(preview_b["preview_id"]))["consumed_at"] is None
    assert len(exchange_b.posts) == 1

    # c) 预检过期：TTL（10 秒）后确认 → preview_expired。
    cell_c = [now]
    store_c, exchange_c, service_c, started_c = _augment_running_service(
        tmp_path / "c", now, key="lp-aug-t15-c", clock_cell=cell_c
    )
    session_c = str(started_c["session_id"])
    exchange_c.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    preview_c = _augment_preview(service_c, session_c, 90, price="0.29")
    cell_c[0] = now + timedelta(seconds=30)
    expired = service_c.augment(session_c, str(preview_c["preview_id"]), "lp-aug-key-c")
    assert expired == {"state": "rejected", "reason": "preview_expired"}
    assert len(exchange_c.posts) == 1

    # d) 同 key 重试：返既有结果，且买一再变也不重复挂单。
    cell_d = [now]
    store_d, exchange_d, service_d, started_d = _augment_running_service(
        tmp_path / "d", now, key="lp-aug-t15-d", clock_cell=cell_d
    )
    session_d = str(started_d["session_id"])
    exchange_d.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    preview_d = _augment_preview(service_d, session_d, 90, price="0.29")
    first = service_d.augment(session_d, str(preview_d["preview_id"]), "lp-aug-key-d")
    assert first["state"] == "entry_open"
    assert str(first["augment_order_id"]) == "order-2"
    exchange_d.snapshot_value = _queue_book_moved_bid_snapshot(now, Decimal("9000"))
    retry = service_d.augment(session_d, "unknown-preview", "lp-aug-key-d")
    assert str(retry["augment_order_id"]) == "order-2"
    assert retry["state"] == "entry_open"
    assert len(exchange_d.posts) == 2


def test_stop_and_review_cancel_entry_and_augment_orders(tmp_path) -> None:
    """T16（#167 改写）：复核/stop 时入场单+新价位追加单一并请求撤；零成交平仓后会话 complete。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _augment_running_service(
        tmp_path, now, key="lp-aug-t16"
    )
    session_id = str(started["session_id"])
    exchange.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    preview = _augment_preview(service, session_id, 90, price="0.29")
    result = service.augment(session_id, str(preview["preview_id"]), "lp-aug-key-16")
    assert result["state"] == "entry_open"
    assert len(exchange.posts) == 2

    stopped = service.stop(session_id)
    assert stopped["state"] == "review"
    assert exchange.cancels == ["order-1", "order-2"]

    # 零成交：两张单均已撤、无持仓 → 复核收尾后会话 complete。
    zero_fill = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("0")
    )
    zero_fill["account"]["open_orders"] = []
    zero_fill["orders"] = [
        _queue_receipt("order-1", original="120", status="CANCELED"),
        _queue_receipt("order-2", price=Decimal("0.29"), original="90", status="CANCELED"),
    ]
    zero_fill["scoring"] = True
    exchange.snapshot_value = zero_fill

    final = service.tick()
    assert final["state"] == "complete"
    assert exchange.cancels == ["order-1", "order-2"]


def test_runtime_queue_position_includes_augment_order(tmp_path) -> None:
    """T17（#167 改写）：加量后逐桶监控——0.30 桶 A=0.80、0.29 桶 A=0.70，均不触发。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-aug-t17"
    )
    session_id = str(started["session_id"])
    # 追加预检快照：0.30 顶档 10000（自单 2000 在挂）、0.29 档他人量 224。
    augment_snapshot = _queue_book_snapshot(now, Decimal("10000"))
    augment_snapshot["book"]["bids"] = [
        {"price": Decimal("0.30"), "size": Decimal("10000")},
        {"price": Decimal("0.29"), "size": Decimal("224")},
    ]
    augment_snapshot["account"]["balance"] = Decimal("10000")
    augment_snapshot["account"]["allowance"] = Decimal("10000")
    augment_snapshot["account"]["open_orders"] = [
        _queue_receipt("order-1", original="2000")
    ]
    augment_snapshot["orders"] = [_queue_receipt("order-1", original="2000")]
    exchange.snapshot_value = augment_snapshot
    preview = _augment_preview(service, session_id, 90, price="0.29")
    result = service.augment(session_id, str(preview["preview_id"]), "lp-aug-key-17")
    assert result["state"] == "entry_open"

    merged = _queue_book_snapshot(now, Decimal("10000"))
    merged["book"]["bids"] = [
        {"price": Decimal("0.30"), "size": Decimal("10000")},
        {"price": Decimal("0.29"), "size": Decimal("320")},
    ]
    merged["account"]["open_orders"] = [
        _queue_receipt("order-1", original="2000"),
        _queue_receipt("order-2", price=Decimal("0.29"), original="90"),
    ]
    merged["orders"] = [
        _queue_receipt("order-1", original="2000"),
        _queue_receipt("order-2", price=Decimal("0.29"), original="90"),
    ]
    exchange.snapshot_value = merged

    ticked = service.tick()
    levels = ticked["queue_protection"]["levels"]
    bucket_30 = levels["0.30"]
    bucket_29 = levels["0.29"]
    assert bucket_30["state"] == "monitoring"
    assert Decimal(str(bucket_30["front_estimate"])) == Decimal("8000")
    assert Decimal(str(bucket_30["level_total"])) == Decimal("10000")
    assert Decimal(str(bucket_30["ratio"])) == Decimal("8000") / Decimal("10000")
    assert bucket_29["state"] == "monitoring"
    assert Decimal(str(bucket_29["front_estimate"])) == Decimal("224")
    assert Decimal(str(bucket_29["level_total"])) == Decimal("320")
    assert Decimal(str(bucket_29["ratio"])) == Decimal("224") / Decimal("320")
    assert exchange.cancels == []


# ---- Issue 152: 队列位置保护运行时闭环（Seam 3）与通知（Seam 6） ----


class _AccountReadExchange(_Exchange):
    def __init__(self) -> None:
        super().__init__()
        self.account_reads = 0
        self.account_open_orders: list[dict[str, object]] = []

    def lp_account_snapshot(self) -> dict[str, object]:
        self.account_reads += 1
        return {
            "authenticated": True,
            "open_orders": list(self.account_open_orders),
        }


def _queue_receipt(
    order_id: str,
    *,
    side: str = "BUY",
    status: str = "LIVE",
    price: object = Decimal("0.30"),
    original: object = Decimal("2000"),
    matched: object = Decimal("0"),
) -> dict[str, object]:
    original_d = Decimal(str(original))
    matched_d = Decimal(str(matched))
    return {
        "order_id": order_id,
        "token_id": "0x" + "1" * 64,
        "side": side,
        "status": status,
        "price": price,
        "original_size": original_d,
        "size_matched": matched_d,
        "remaining_size": original_d - matched_d,
    }


def _queue_runtime_snapshot(
    now: datetime,
    *,
    bid_size: object,
    orders: list[dict[str, object]] | None = None,
    open_orders: list[dict[str, object]] | None = None,
    trades: list[dict[str, object]] | None = None,
    positions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    snapshot = _queue_book_snapshot(now, Decimal(str(bid_size)))
    snapshot["account"].update(
        {
            "balance": Decimal("10000"),
            "allowance": Decimal("10000"),
            "positions": list(positions or []),
            "open_orders": list(open_orders or []),
        }
    )
    snapshot["orders"] = list(orders or [])
    snapshot["trades"] = list(trades or [])
    return snapshot


def _queue_running_service(
    tmp_path,
    now: datetime,
    *,
    key: str,
    guard: object = None,
    register_bid: object = Decimal("10000"),
):
    """Start one registered session with quantity 2000 at price 0.30."""

    exchange = _Exchange()
    exchange.snapshot_value = _queue_runtime_snapshot(now, bid_size=register_bid)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(
        store, exchange, clock=lambda: now, mutation_guard=guard
    )
    request = {**_request(now), "quantity": Decimal("2000")}
    preview = service.preview(request)
    started = service.start(str(preview["preview_id"]), key)
    assert started["state"] == "entry_open"
    return store, exchange, service, started


def test_candidate_reservations_cover_each_active_session(tmp_path) -> None:
    """Issue 165: 预留输出含活动会话条目（order_id/amount 与既有行为一致）；
    无活动会话时无 lp 预留条目。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-reservations"
    )
    session_id = str(started["session_id"])
    session = store.lp_session(session_id)
    assert session is not None
    entry_order_id = str(session["entry_order_id"])
    assert entry_order_id == "order-1"

    reservations = service._candidate_reservations()
    lp_entries = [
        entry
        for entry in reservations
        if str(entry["order_id"]) == entry_order_id
        or str(entry["order_id"]).startswith("lp-session:")
    ]
    assert lp_entries == [{"order_id": "order-1", "amount": Decimal("600.00")}]

    # 无活动会话（终态 complete）→ 无任何 lp 预留条目。
    store.lp_update_session(session_id, state="complete")
    after = service._candidate_reservations()
    assert [
        entry
        for entry in after
        if str(entry["order_id"]) == entry_order_id
        or str(entry["order_id"]).startswith("lp-session:")
    ] == []


def test_queue_protection_triggers_cancel_once_and_converges(tmp_path) -> None:
    """T13: 10000→4000 触发：撤单一次、action pending→回执、终态 canceled。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-t13"
    )
    session_id = str(started["session_id"])
    entry_id = "order-1"
    live_receipt = _queue_receipt(entry_id)
    exchange.snapshots = [
        _queue_runtime_snapshot(now, bid_size="10000", orders=[live_receipt]),
        _queue_runtime_snapshot(now, bid_size="4000", orders=[live_receipt]),
        _queue_runtime_snapshot(
            now, bid_size="4000", orders=[_queue_receipt(entry_id, status="CANCELED")]
        ),
    ]
    exchange.snapshot_calls = 0

    monitoring = service.tick()
    assert monitoring["queue_protection"]["state"] == "monitoring"
    assert Decimal(str(monitoring["queue_protection"]["ratio"])) == Decimal("0.80")
    assert exchange.cancels == []

    triggered = service.tick()
    assert exchange.cancels == [entry_id]
    protection = triggered["queue_protection"]
    assert protection["state"] == "canceling"
    assert protection["cancel_targets"] == [entry_id]
    assert Decimal(str(protection["ratio"])) == Decimal("0.50")
    assert store.lp_session(session_id)["entry_cancel_requested"] is True
    (cancel_action,) = [
        action
        for action in store.lp_actions(session_id)
        if action["action_key"].endswith(f"entry-protection-cancel:{entry_id}")
    ]
    assert cancel_action["state"] == "accepted"
    assert cancel_action["targets"] == [entry_id]
    assert cancel_action["reason"] == "queue_ahead_ratio"

    converged = service.tick()
    assert converged["queue_protection"]["state"] == "canceled"
    assert exchange.cancels == [entry_id]


def test_queue_protection_stable_level_never_cancels(tmp_path) -> None:
    """T14: 快照维持 10000：不撤，状态 monitoring。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-t14"
    )
    live_receipt = _queue_receipt("order-1")
    exchange.snapshots = [
        _queue_runtime_snapshot(now, bid_size="10000", orders=[live_receipt]),
        _queue_runtime_snapshot(now, bid_size="10000", orders=[live_receipt]),
    ]
    exchange.snapshot_calls = 0
    first = service.tick()
    second = service.tick()
    assert first["queue_protection"]["state"] == "monitoring"
    assert second["queue_protection"]["state"] == "monitoring"
    assert exchange.cancels == []


def test_queue_protection_trigger_is_idempotent_across_ticks(tmp_path) -> None:
    """T15: 触发后连续多个 tick：cancels 仍只一次。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-t15"
    )
    live_receipt = _queue_receipt("order-1")
    trigger = _queue_runtime_snapshot(now, bid_size="4000", orders=[live_receipt])
    exchange.snapshots = [trigger, trigger, trigger]
    exchange.snapshot_calls = 0
    service.tick()
    service.tick()
    service.tick()
    assert exchange.cancels == ["order-1"]


def test_queue_protection_failed_cancel_receipts_retry_next_tick(tmp_path) -> None:
    """T16: 撤单回执未确认 → canceling，下一 tick 重试，无重复买入。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-t16"
    )
    entry_id = "order-1"
    live_receipt = _queue_receipt(entry_id)
    trigger = _queue_runtime_snapshot(now, bid_size="4000", orders=[live_receipt])
    exchange.snapshots = [trigger, trigger, trigger]
    exchange.snapshot_calls = 0
    exchange.cancel_responses = [{"not_canceled": {entry_id: "venue_busy"}}]

    first = service.tick()
    protection = first["queue_protection"]
    assert protection["state"] == "canceling"
    assert protection["cancel_failed"] == [entry_id]
    assert protection["cancel_failure"]
    (cancel_action,) = [
        action
        for action in store.lp_actions(str(started["session_id"]))
        if action["action_key"].endswith(f"entry-protection-cancel:{entry_id}")
    ]
    assert cancel_action["state"] == "pending"
    assert exchange.cancels == [entry_id]
    assert len(exchange.posts) == 1

    second = service.tick()
    assert exchange.cancels == [entry_id, entry_id]
    assert second["queue_protection"]["cancel_failed"] == []
    assert len(exchange.posts) == 1


def test_queue_protection_partial_fill_during_cancel_episode(tmp_path) -> None:
    """T17: 撤单期间成交 500 → 部分成交、余量撤、不补买、无保护性 SELL。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    token_id = "0x" + "1" * 64
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-t17"
    )
    entry_id = "order-1"
    trigger = _queue_runtime_snapshot(
        now, bid_size="3000", orders=[_queue_receipt(entry_id)]
    )
    filling = _queue_runtime_snapshot(
        now,
        bid_size="3000",
        orders=[_queue_receipt(entry_id, matched="500")],
        trades=[
            {
                "trade_id": "t-1",
                "status": "CONFIRMED",
                "maker_orders": [
                    {
                        "order_id": entry_id,
                        "side": "BUY",
                        "token_id": token_id,
                        "matched_amount": Decimal("500"),
                        "price": Decimal("0.30"),
                    }
                ],
            }
        ],
        positions=[{"token_id": token_id, "size": Decimal("500")}],
    )
    cancelled = _queue_runtime_snapshot(
        now,
        bid_size="3000",
        orders=[_queue_receipt(entry_id, status="CANCELED", matched="500")],
        trades=filling["trades"],
        positions=[{"token_id": token_id, "size": Decimal("500")}],
    )
    exchange.snapshots = [trigger, filling, cancelled]
    exchange.snapshot_calls = 0

    assert service.tick()["queue_protection"]["state"] == "canceling"
    mid = service.tick()
    assert mid["buy_filled_quantity"] == Decimal("500")
    final = service.tick()
    assert final["queue_protection"]["state"] == "partially_filled"
    assert Decimal(
        str(final["queue_protection"]["partially_filled_quantity"])
    ) == Decimal("500")
    assert len([item for item in exchange.posts if item["side"] == "BUY"]) == 1
    assert exchange.protected_sells == []


def test_queue_protection_outage_conservative_cancel_after_ten_failures(
    tmp_path,
) -> None:
    """T18: 连续 9 次断联不触发；第 10 次保守撤同价 2 张。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _AccountReadExchange()
    exchange.snapshot_value = _queue_runtime_snapshot(now, bid_size="10000")
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {**_request(now), "quantity": Decimal("2000")}
    preview = service.preview(request)
    started = service.start(str(preview["preview_id"]), "lp-queue-t18")
    session_id = str(started["session_id"])
    exchange.account_open_orders = [
        _queue_receipt("order-1"),
        _queue_receipt("manual-2", original="1000"),
    ]
    exchange.snapshot_value = None
    exchange.snapshots = []
    exchange.snapshot_calls = 0

    for expected_failures in range(1, 10):
        result = service.tick()
        assert result["state"] == "needs_attention"
        protection = result["queue_protection"]
        assert Decimal(str(protection["data_failures"])) == expected_failures
        assert exchange.cancels == []
        assert protection["state"] == "registered"

    conservative = service.tick()
    protection = conservative["queue_protection"]
    assert exchange.cancels == ["order-1", "manual-2"]
    assert protection["cancel_reason"] == "book_unreliable"
    assert protection["state"] == "canceling"
    assert store.lp_session(session_id)["entry_cancel_requested"] is True
    assert exchange.account_reads >= 1


def _queue_outage_service(tmp_path, now: datetime, key: str):
    """Start one registered session (quantity 2000 @ 0.30) ready for outage ticks."""

    exchange = _AccountReadExchange()
    exchange.snapshot_value = _queue_runtime_snapshot(now, bid_size="10000")
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {**_request(now), "quantity": Decimal("2000")}
    preview = service.preview(request)
    started = service.start(str(preview["preview_id"]), key)
    assert started["state"] == "entry_open"
    return store, exchange, service, started


def test_queue_protection_outage_never_fires_after_entry_fill(tmp_path) -> None:
    """R1(F1): entry 已成交的会话，好坏 tick 交错累计 10 个断联 tick → 永不保守撤、计数保持 0。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    token_id = "0x" + "1" * 64
    store, exchange, service, started = _queue_outage_service(
        tmp_path, now, key="lp-queue-r1"
    )
    session_id = str(started["session_id"])

    filled = _queue_runtime_snapshot(
        now,
        bid_size="8000",
        orders=[_queue_receipt("order-1", status="MATCHED", matched="2000")],
        trades=[
            {
                "trade_id": "t-1",
                "status": "CONFIRMED",
                "maker_orders": [
                    {
                        "order_id": "order-1",
                        "side": "BUY",
                        "token_id": token_id,
                        "matched_amount": Decimal("2000"),
                        "price": Decimal("0.30"),
                    }
                ],
            }
        ],
        positions=[{"token_id": token_id, "size": Decimal("2000")}],
        open_orders=[_queue_receipt("manual-2", original="1000")],
    )
    exchange.snapshot_value = filled
    result = service.tick()
    assert result["buy_filled_quantity"] == Decimal("2000")

    # 手动同价 BUY 仍在挂；断联与成交后好 tick 交错，断联累计 10 次。
    exchange.account_open_orders = [_queue_receipt("manual-2", original="1000")]
    for index in range(20):
        exchange.snapshot_value = None if index % 2 == 0 else filled
        service.tick()
        # #167 改写：读投影视图（原始载荷落 v2 分桶形状，state 在桶内）。
        protection = service.status(session_id)["queue_protection"]
        assert exchange.cancels == []
        assert Decimal(str(protection["data_failures"])) == 0
        assert protection["state"] != "canceling"


def test_queue_protection_outage_never_fires_when_cancel_requested(tmp_path) -> None:
    """R2(F1): entry_cancel_requested=True 的会话，连续 10 个断联 tick → 不保守撤。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_outage_service(
        tmp_path, now, key="lp-queue-r2a"
    )
    session_id = str(started["session_id"])
    store.lp_update_session(session_id, patch={"entry_cancel_requested": True})

    exchange.snapshot_value = None
    for _ in range(10):
        result = service.tick()
        assert result["state"] == "needs_attention"
        assert exchange.cancels == []
        assert Decimal(str(result["queue_protection"]["data_failures"])) == 0


def test_queue_protection_outage_never_fires_when_entry_terminal(tmp_path) -> None:
    """R2(F1): entry 回执终态（已撤）的会话，连续 10 个断联 tick → 不保守撤。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_outage_service(
        tmp_path, now, key="lp-queue-r2b"
    )
    session_id = str(started["session_id"])
    store.lp_update_session(
        session_id,
        patch={"order_history": {"order-1": {"order_id": "order-1", "status": "CANCELED"}}},
    )

    exchange.snapshot_value = None
    for _ in range(10):
        result = service.tick()
        assert exchange.cancels == []
        assert Decimal(str(result["queue_protection"]["data_failures"])) == 0


def test_queue_protection_outage_never_fires_without_entry_order_id(tmp_path) -> None:
    """R3(F1): 提交失败无 entry_order_id 的 needs_attention 会话，断联 tick 不保守撤。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = None
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    store.lp_create_session(
        "lp-queue-r3",
        "lp-queue-r3-idempotency",
        state="needs_attention",
        payload={
            "market_id": "market-1",
            "condition_id": "0x" + "c" * 64,
            "token_id": "0x" + "1" * 64,
            "outcome": "YES",
            "question": "Will it happen?",
            "price": Decimal("0.30"),
            "quantity": Decimal("2000"),
            "review_at": now + timedelta(minutes=10),
            "resume_state": "entry_open",
            "queue_protection": {
                "state": "registered",
                "baseline_price": "0.30",
                "baseline_front": "0",
                "threshold": "0.5",
                "reason_codes": [],
                "data_time": now.isoformat(),
            },
        },
    )

    for _ in range(10):
        result = service.tick()
        assert result["state"] == "needs_attention"
        assert exchange.cancels == []
        assert Decimal(str(result["queue_protection"]["data_failures"])) == 0


def test_queue_protection_outage_streak_resets_on_successful_tick(tmp_path) -> None:
    """R4(F1/T18 语义): entry 存活时连续 10 次触发、第 9 次不触发、成功 tick 复位计数。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_outage_service(
        tmp_path, now, key="lp-queue-r4"
    )
    good = _queue_runtime_snapshot(
        now, bid_size="10000", orders=[_queue_receipt("order-1")]
    )

    # 断联与成功 tick 交错：每次成功 tick 把计数复位为 0，断联后最多为 1。
    for _ in range(5):
        exchange.snapshot_value = None
        outage = service.tick()
        assert Decimal(str(outage["queue_protection"]["data_failures"])) == 1
        assert exchange.cancels == []
        exchange.snapshot_value = good
        healthy = service.tick()
        assert Decimal(str(healthy["queue_protection"]["data_failures"])) == 0
        assert exchange.cancels == []

    # 连续 9 次断联：计数递增且不触发。
    exchange.snapshot_value = None
    for expected in range(1, 10):
        result = service.tick()
        assert Decimal(str(result["queue_protection"]["data_failures"])) == expected
        assert exchange.cancels == []

    # 第 10 次连续断联 → 保守撤（同价 2 张，走新账户读取）。
    exchange.account_open_orders = [
        _queue_receipt("order-1"),
        _queue_receipt("manual-2", original="1000"),
    ]
    conservative = service.tick()
    assert exchange.cancels == ["order-1", "manual-2"]
    assert conservative["queue_protection"]["cancel_reason"] == "book_unreliable"
    assert conservative["queue_protection"]["state"] == "canceling"


def test_queue_protection_mutation_breaker_blocks_cancel(tmp_path) -> None:
    """T19: 熔断返回 False：无撤单调用，blocked/mutation_blocked，受阻通知。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    flags = {"allowed": True}
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-t19", guard=lambda *args, **kwargs: flags["allowed"]
    )
    notifications: list[tuple[str, str, str]] = []
    service.set_protection_notifier(
        lambda title, message, xiaoai: notifications.append((title, message, xiaoai))
    )
    flags["allowed"] = False
    live_receipt = _queue_receipt("order-1")
    trigger = _queue_runtime_snapshot(now, bid_size="4000", orders=[live_receipt])
    exchange.snapshots = [trigger, trigger]
    exchange.snapshot_calls = 0
    service.tick()
    second = service.tick()

    assert exchange.cancels == []
    protection = second["queue_protection"]
    assert protection["state"] == "blocked"
    assert "mutation_blocked" in protection["reason_codes"]
    assert len(notifications) == 1
    title, message, xiaoai = notifications[0]
    assert title == "YES 0.3 位置保护撤单受阻"
    assert "撤单未成功" in message
    assert xiaoai == "YES 0.3 位置保护撤单受阻"


def test_queue_protection_restart_does_not_repeat_cancel(tmp_path) -> None:
    """T20: 撤单请求落库后新实例 tick：不重复 cancel，终态收敛。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-t20"
    )
    entry_id = "order-1"
    trigger = _queue_runtime_snapshot(
        now, bid_size="4000", orders=[_queue_receipt(entry_id)]
    )
    exchange.snapshots = [trigger]
    exchange.snapshot_calls = 0
    service.tick()
    assert exchange.cancels == [entry_id]

    restarted = PolymarketLPService(store, exchange, clock=lambda: now)
    exchange.snapshots = [
        _queue_runtime_snapshot(
            now, bid_size="4000", orders=[_queue_receipt(entry_id, status="CANCELED")]
        )
    ]
    exchange.snapshot_calls = 0
    recovered = restarted.tick()
    assert exchange.cancels == [entry_id]
    assert recovered["queue_protection"]["state"] == "canceled"


def test_queue_protection_identity_conflict_blocks_cancel(tmp_path) -> None:
    """T21: 回执 side=SELL → 不撤、blocked/identity_conflict。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-t21"
    )
    conflict_orders = [
        _queue_receipt("order-1", side="SELL"),
        _queue_receipt("manual-2", original="2000"),
    ]
    trigger = _queue_runtime_snapshot(
        now, bid_size="3000", open_orders=conflict_orders
    )
    exchange.snapshots = [trigger]
    exchange.snapshot_calls = 0
    result = service.tick()

    assert exchange.cancels == []
    protection = result["queue_protection"]
    assert protection["state"] == "blocked"
    assert "identity_conflict" in protection["reason_codes"]


def test_queue_protection_cancels_manual_same_price_buy_only(tmp_path) -> None:
    """T22: 手动同价第二张 BUY 一并撤；不同价位 BUY 不撤。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-t22"
    )
    open_orders = [
        _queue_receipt("order-1"),
        _queue_receipt("manual-2", original="1500"),
        _queue_receipt("manual-other-price", price=Decimal("0.29"), original="999"),
    ]
    trigger = _queue_runtime_snapshot(now, bid_size="6000", open_orders=open_orders)
    exchange.snapshots = [trigger]
    exchange.snapshot_calls = 0
    result = service.tick()

    assert exchange.cancels == ["order-1", "manual-2"]
    assert "manual-other-price" not in exchange.cancels
    protection = result["queue_protection"]
    assert protection["state"] == "canceling"
    assert protection["cancel_targets"] == ["order-1", "manual-2"]


def test_queue_protection_notification_success_template_once(tmp_path) -> None:
    """T26: 成功模板一次（N 张/M 手动/数字格式），置位后不重发；xiaoai 短句。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-t26"
    )
    notifications: list[tuple[str, str, str]] = []
    service.set_protection_notifier(
        lambda title, message, xiaoai: notifications.append((title, message, xiaoai))
    )
    entry_id = "order-1"
    open_orders = [
        _queue_receipt(entry_id),
        _queue_receipt("manual-2", original="1000"),
    ]
    trigger = _queue_runtime_snapshot(now, bid_size="4000", open_orders=open_orders)
    cancelled = _queue_runtime_snapshot(
        now,
        bid_size="4000",
        open_orders=open_orders,
        orders=[
            _queue_receipt(entry_id, status="CANCELED"),
            _queue_receipt("manual-2", status="CANCELED", original="1000"),
        ],
    )
    exchange.snapshots = [trigger, trigger, cancelled]
    exchange.snapshot_calls = 0
    service.tick()
    assert len(notifications) == 1
    title, message, xiaoai = notifications[0]
    assert title == "YES 0.3 位置保护已触发撤单"
    assert "市场：Will it happen?" in message
    assert "A 比例 25% ≤ 50%" in message
    assert "前方≈1000 / 同价位 4000 份" in message
    assert "已撤 2 张买单合计余量 3000 份 @ 0.3（含 1 张手动）" in message
    assert "数据时间：北京时间" in message
    assert xiaoai == "YES 0.3 位置保护已触发撤单，2 张买单已撤"

    restarted = PolymarketLPService(store, exchange, clock=lambda: now)
    restarted.set_protection_notifier(
        lambda title, message, xiaoai: notifications.append((title, message, xiaoai))
    )
    service.tick()
    restarted.tick()
    assert len(notifications) == 1


def test_queue_protection_retry_success_notifies_full_episode_once(tmp_path) -> None:
    """R5(F2): entry 撤成+manual 失败 → 重试成功后恰一条通知，按全量 2 张/合计余量 3000 报告；
    cancel_targets/canceled_order_ids 跨重试保持并集。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-r5"
    )
    notifications: list[tuple[str, str, str]] = []
    service.set_protection_notifier(
        lambda title, message, xiaoai: notifications.append((title, message, xiaoai))
    )
    entry_id = "order-1"
    open_orders = [
        _queue_receipt(entry_id),
        _queue_receipt("manual-2", original="1000"),
    ]
    trigger = _queue_runtime_snapshot(now, bid_size="4000", open_orders=open_orders)
    exchange.snapshots = [trigger, trigger, trigger]
    exchange.snapshot_calls = 0
    exchange.cancel_responses = [
        {"canceled": [entry_id], "status": "CANCELED"},
        {"not_canceled": {"manual-2": "venue_busy"}},
        {"canceled": ["manual-2"], "status": "CANCELED"},
    ]

    first = service.tick()
    protection = first["queue_protection"]
    assert protection["state"] == "canceling"
    assert protection["cancel_failed"] == ["manual-2"]
    assert protection["cancel_targets"] == [entry_id, "manual-2"]
    assert protection["canceled_order_ids"] == [entry_id]
    assert Decimal(str(protection["canceled_remaining"])) == Decimal("2000")
    assert notifications == []

    second = service.tick()
    # 首次尝试对 manual-2 发过一次被拒的撤单调用，重试再发一次。
    assert exchange.cancels == [entry_id, "manual-2", "manual-2"]
    protection = second["queue_protection"]
    assert protection["cancel_failed"] == []
    assert protection["cancel_targets"] == [entry_id, "manual-2"]
    assert protection["canceled_order_ids"] == [entry_id, "manual-2"]
    assert Decimal(str(protection["canceled_remaining"])) == Decimal("3000")
    assert len(notifications) == 1
    title, message, xiaoai = notifications[0]
    assert title == "YES 0.3 位置保护已触发撤单"
    assert "已撤 2 张买单合计余量 3000 份 @ 0.3（含 1 张手动）" in message
    assert xiaoai == "YES 0.3 位置保护已触发撤单，2 张买单已撤"


def test_queue_protection_converge_keeps_fill_and_cancel_remaining_apart(
    tmp_path,
) -> None:
    """R6(F2): 部分成交（filled=300）收敛：通知合计余量=各单撤单时点余量之和
    2000+1000=3000（review fix 口径：manual-2 回执撤但未获确认也按请求时点余量计），
    不把成交量 300 冒充撤单余量；持久化中 300（已成交）与 3000（已撤余量）分列。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    token_id = "0x" + "1" * 64
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-r6"
    )
    notifications: list[tuple[str, str, str]] = []
    service.set_protection_notifier(
        lambda title, message, xiaoai: notifications.append((title, message, xiaoai))
    )
    entry_id = "order-1"
    open_orders = [
        _queue_receipt(entry_id),
        _queue_receipt("manual-2", original="1000"),
    ]
    trigger = _queue_runtime_snapshot(now, bid_size="4000", open_orders=open_orders)
    exchange.snapshots = [trigger]
    exchange.snapshot_calls = 0
    exchange.cancel_responses = [
        {"canceled": [entry_id], "status": "CANCELED"},
        {"not_canceled": {"manual-2": "venue_busy"}},
    ]

    first = service.tick()
    protection = first["queue_protection"]
    assert protection["state"] == "canceling"
    assert protection["cancel_failed"] == ["manual-2"]
    assert Decimal(str(protection["canceled_remaining"])) == Decimal("2000")
    assert notifications == []

    # 下一 tick 回执显示两单都已终态：entry 撤前成交 300，manual 已撤。
    converged_snapshot = _queue_runtime_snapshot(
        now,
        bid_size="4000",
        orders=[
            _queue_receipt(entry_id, status="CANCELED", matched="300"),
            _queue_receipt("manual-2", status="CANCELED", original="1000"),
        ],
        trades=[
            {
                "trade_id": "t-1",
                "status": "CONFIRMED",
                "maker_orders": [
                    {
                        "order_id": entry_id,
                        "side": "BUY",
                        "token_id": token_id,
                        "matched_amount": Decimal("300"),
                        "price": Decimal("0.30"),
                    }
                ],
            }
        ],
        positions=[{"token_id": token_id, "size": Decimal("300")}],
    )
    exchange.snapshots = [converged_snapshot]
    exchange.snapshot_calls = 0
    final = service.tick()
    protection = final["queue_protection"]
    assert protection["state"] == "partially_filled"
    assert Decimal(str(protection["partially_filled_quantity"])) == Decimal("300")
    assert Decimal(str(protection["canceled_remaining"])) == Decimal("3000")
    assert len(notifications) == 1
    title, message, xiaoai = notifications[0]
    assert title == "YES 0.3 位置保护已触发撤单"
    assert "已撤 2 张买单合计余量 3000 份" in message
    assert "合计余量 300 份" not in message
    assert xiaoai == "YES 0.3 位置保护已触发撤单，2 张买单已撤"


def test_queue_protection_unknown_request_time_remaining_reports_unknown_total(
    tmp_path,
) -> None:
    """R10: 某 target 在请求时点取不到余量（rows 缺 remaining 字段）→ 持久化 null
    （UNKNOWN，不记 0）；撤单未获确认、下一 tick 回执收敛后通知
    「合计余量 UNKNOWN 份」，不出现「0 份」也不部分求和冒充全量。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_outage_service(
        tmp_path, now, key="lp-queue-r10"
    )
    notifications: list[tuple[str, str, str]] = []
    service.set_protection_notifier(
        lambda title, message, xiaoai: notifications.append((title, message, xiaoai))
    )
    entry_id = "order-1"
    token_id = "0x" + "1" * 64

    exchange.snapshot_value = None
    exchange.snapshots = []
    exchange.snapshot_calls = 0
    for expected in range(1, 10):
        result = service.tick()
        assert Decimal(str(result["queue_protection"]["data_failures"])) == expected
        assert exchange.cancels == []

    # 第 10 次断联 → 保守撤；新账户读取里 manual-2 的行缺任何 remaining 字段。
    exchange.account_open_orders = [
        _queue_receipt(entry_id),
        {
            "order_id": "manual-2",
            "token_id": token_id,
            "side": "BUY",
            "status": "LIVE",
            "price": Decimal("0.30"),
        },
    ]
    exchange.cancel_responses = [
        {"not_canceled": {entry_id: "venue_busy"}},
        {"not_canceled": {"manual-2": "venue_busy"}},
    ]
    conservative = service.tick()
    protection = conservative["queue_protection"]
    assert exchange.cancels == [entry_id, "manual-2"]
    assert protection["cancel_reason"] == "book_unreliable"
    assert protection["state"] == "canceling"
    assert notifications == []
    assert protection["cancel_target_remaining"] == {
        entry_id: "2000",
        "manual-2": None,
    }

    # 下一 tick 恢复读取：回执双双 CANCELED（零成交），episode 收敛。
    exchange.snapshot_value = _queue_runtime_snapshot(
        now,
        bid_size="4000",
        orders=[
            _queue_receipt(entry_id, status="CANCELED"),
            _queue_receipt("manual-2", status="CANCELED", original="1000"),
        ],
    )
    exchange.snapshot_calls = 0
    final = service.tick()
    protection = final["queue_protection"]
    assert protection["state"] == "canceled"
    assert protection["canceled_remaining"] is None
    assert len(notifications) == 1
    title, message, xiaoai = notifications[0]
    assert title == "YES 0.3 位置保护已触发撤单"
    assert "已撤 2 张买单合计余量 UNKNOWN 份 @ 0.3（含 1 张手动）" in message
    assert "合计余量 0 份" not in message
    assert "合计余量 2000 份" not in message
    assert xiaoai == "YES 0.3 位置保护已触发撤单，2 张买单已撤"


def test_queue_protection_unacknowledged_cancels_converge_with_request_time_remaining(
    tmp_path,
) -> None:
    """R9: 两张撤单请求均未获确认（not_canceled），下一 tick 回执双双 CANCELED 收敛：
    通知按请求时点持久化余量报「已撤 2 张买单合计余量 3000 份 @ 0.3（含 1 张手动）」，
    不再把回执已撤但未获确认的订单冒充余量 0；撤单时点余量映射含两单。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-queue-r9"
    )
    notifications: list[tuple[str, str, str]] = []
    service.set_protection_notifier(
        lambda title, message, xiaoai: notifications.append((title, message, xiaoai))
    )
    entry_id = "order-1"
    open_orders = [
        _queue_receipt(entry_id),
        _queue_receipt("manual-2", original="1000"),
    ]
    trigger = _queue_runtime_snapshot(now, bid_size="4000", open_orders=open_orders)
    exchange.snapshots = [trigger]
    exchange.snapshot_calls = 0
    # 两张撤单请求都被 venue 拒绝（未获确认），余量 2000+1000 仍在请求时点 rows 中。
    exchange.cancel_responses = [
        {"not_canceled": {entry_id: "venue_busy"}},
        {"not_canceled": {"manual-2": "venue_busy"}},
    ]

    first = service.tick()
    protection = first["queue_protection"]
    assert protection["state"] == "canceling"
    assert protection["cancel_failed"] == [entry_id, "manual-2"]
    assert protection["canceled_order_ids"] == []
    assert notifications == []
    # 请求（意图/pending）阶段即按 rows 持久化每张目标单的撤单时点余量。
    assert protection["cancel_target_remaining"] == {
        entry_id: "2000",
        "manual-2": "1000",
    }
    session_id = str(started["session_id"])
    (cancel_action,) = [
        action
        for action in store.lp_actions(session_id)
        if action["action_key"].endswith(f"entry-protection-cancel:{entry_id}")
    ]
    assert cancel_action["cancel_target_remaining"] == {
        entry_id: "2000",
        "manual-2": "1000",
    }

    # 下一 tick：回执双双 CANCELED（零成交），episode 收敛。
    converged_snapshot = _queue_runtime_snapshot(
        now,
        bid_size="4000",
        orders=[
            _queue_receipt(entry_id, status="CANCELED"),
            _queue_receipt("manual-2", status="CANCELED", original="1000"),
        ],
    )
    exchange.snapshots = [converged_snapshot]
    exchange.snapshot_calls = 0
    final = service.tick()
    protection = final["queue_protection"]
    assert protection["state"] == "canceled"
    assert protection["canceled_order_ids"] == [entry_id, "manual-2"]
    assert protection["cancel_target_remaining"] == {
        entry_id: "2000",
        "manual-2": "1000",
    }
    assert Decimal(str(protection["canceled_remaining"])) == Decimal("3000")
    assert len(notifications) == 1
    title, message, xiaoai = notifications[0]
    assert title == "YES 0.3 位置保护已触发撤单"
    assert "已撤 2 张买单合计余量 3000 份 @ 0.3（含 1 张手动）" in message
    assert "合计余量 0 份" not in message
    assert xiaoai == "YES 0.3 位置保护已触发撤单，2 张买单已撤"

def _iso_z(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


class _LPRollingPoolExchange(_LPCandidateQueryExchange):
    """Issue #157 rolling-pool fixture: three single-outcome markets quoting
    0.50, so the min-dominated target is 20.00 shares and capital is $10.00.

    With hourly reward = pool × 5% / 24, the estimated yield per hour in
    percent is pool / 48: M01 and M02 (pool 57.6) estimate exactly 1.2%/h,
    M03 (pool 38.4) exactly 0.8%/h.  `omit_conditions` drops a market's books
    from the response (read failure); `pool_override` swaps a market's daily
    pool for the next reward read (re-estimate); `flip_competition` inverts
    the official competition values.
    """

    def __init__(self, initial_now: datetime) -> None:
        super().__init__(
            initial_now,
            {
                "M01": Decimal("57.6"),
                "M02": Decimal("57.6"),
                "M03": Decimal("38.4"),
            },
            book_bid="0.50",
        )
        self.omit_conditions: frozenset[str] = frozenset()
        self.pool_override: dict[str, Decimal | None] = {}
        self.flip_competition = False
        self.string_rules_conditions: frozenset[str] = frozenset()

    def lp_market_metadata(self, condition_ids, *, stop_event=None):
        metadata = super().lp_market_metadata(
            condition_ids, stop_event=stop_event
        )
        if not self.string_rules_conditions:
            return metadata
        return {
            condition_id: (
                {**dict(row), "reward_min_size": "20"}
                if condition_id in self.string_rules_conditions
                else row
            )
            for condition_id, row in metadata.items()
        }

    def lp_order_books(self, token_ids, *, stop_event=None):
        books = super().lp_order_books(token_ids, stop_event=stop_event)
        return {
            token_id: book
            for token_id, book in books.items()
            if token_id.removeprefix("token-") not in self.omit_conditions
        }

    def lp_reward_catalog(self, *, condition_ids=None, stop_event=None):
        catalog = super().lp_reward_catalog(
            condition_ids=condition_ids, stop_event=stop_event
        )
        if not self.pool_override:
            return catalog
        markets = []
        for row in catalog["markets"]:
            suffix = str(row.get("condition_id") or "").removeprefix("condition-")
            markets.append(
                {
                    **dict(row),
                    "daily_pool_usd": self.pool_override.get(
                        f"condition-{suffix}", row.get("daily_pool_usd")
                    ),
                }
            )
        return {**dict(catalog), "markets": tuple(markets)}

    def lp_market_competitiveness(self, *, stop_event=None, previous=None):
        del stop_event, previous
        values = {
            "condition-M01": Decimal(1),
            "condition-M02": Decimal(9),
            "condition-M03": Decimal(5),
        }
        if self.flip_competition:
            values = {
                "condition-M01": Decimal(9),
                "condition-M02": Decimal(1),
                "condition-M03": Decimal(5),
            }
        return {
            "state": "known",
            "complete": True,
            "checked_at": self.now,
            "round_checked_at": self.now,
            "competitiveness": {
                key: (value, self.now) for key, value in values.items()
            },
            "not_updated": [],
        }


def test_candidate_pool_ranks_by_yield_then_updated_at(tmp_path) -> None:
    """Issue #157 A3: the rolling pool ranks by estimated yield descending,
    then estimate time descending, then stable identity — competition and
    actual capital leave the ranking key, so flipping competition values
    alone never reshuffles the displayed order."""
    now = datetime(2026, 9, 20, 8, tzinfo=UTC)
    current = {"now": now}
    exchange = _LPRollingPoolExchange(now)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert first["state"] == "ready"
    # 1.2 ties at the same estimate time fall to the stable identity.
    assert [row["market_id"] for row in first["candidates"]] == [
        "market-M01", "market-M02", "market-M03",
    ]
    assert first["recommendations"][0]["market_id"] == "market-M01"

    # M02 re-estimates at t1; M01/M03 reads fail and keep their old rows.
    exchange.omit_conditions = frozenset({"condition-M01", "condition-M03"})
    current["now"] = now + timedelta(seconds=65)
    exchange.now = current["now"]
    lp.refresh_candidate_recommendations()

    pool = lp.candidate_snapshot()
    assert [row["market_id"] for row in pool["candidates"]] == [
        "market-M02", "market-M01", "market-M03",
    ]
    assert pool["recommendations"][0]["market_id"] == "market-M02"

    # Competition values flip (M01 9, M02 1) and both re-estimate at t2:
    # equal yields at equal estimate times still fall to identity — never
    # to competition.
    exchange.omit_conditions = frozenset()
    exchange.flip_competition = True
    current["now"] = current["now"] + timedelta(seconds=65)
    exchange.now = current["now"]
    lp.refresh_candidate_recommendations()

    flipped = lp.candidate_snapshot()
    assert [row["market_id"] for row in flipped["candidates"]] == [
        "market-M01", "market-M02", "market-M03",
    ]
    assert flipped["recommendations"][0]["market_id"] == "market-M01"


def test_candidate_pool_replaces_dropped_estimate_and_unknown_last(tmp_path) -> None:
    """Issue #157 A4: a re-estimate replaces the stored value immediately —
    a market whose yield dropped re-ranks by its new value with no trace of
    the historical high — and an UNKNOWN estimate ranks after every known
    value instead of entering the order as zero."""
    now = datetime(2026, 9, 20, 9, tzinfo=UTC)
    current = {"now": now}
    exchange = _LPRollingPoolExchange(now)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert [row["market_id"] for row in first["candidates"]] == [
        "market-M01", "market-M02", "market-M03",
    ]

    # M01's pool drops 57.6 → 24.0: the new estimate is exactly 0.5%/h, so
    # the previously top-ranked market sinks below M03's 0.8.
    exchange.pool_override = {"condition-M01": Decimal("24.0")}
    current["now"] = now + timedelta(seconds=65)
    exchange.now = current["now"]
    lp.refresh_candidate_recommendations()

    dropped = lp.candidate_snapshot()
    assert [row["market_id"] for row in dropped["candidates"]] == [
        "market-M02", "market-M03", "market-M01",
    ]
    dropped_m01 = dropped["candidates"][2]
    assert Decimal(str(dropped_m01["estimated_yield_raw"])) == Decimal("0.5")

    # M02's refreshed metadata reports its reward minimum as a raw string:
    # the eligibility gate parses it, but the estimator requires Decimal
    # rules, so the re-estimate comes back UNKNOWN.  The row keeps its
    # eligibility and ranks after every known value — never as zero.
    exchange.pool_override = {"condition-M01": Decimal("24.0")}
    exchange.string_rules_conditions = frozenset({"condition-M02"})
    current["now"] = current["now"] + timedelta(seconds=65)
    exchange.now = current["now"]
    lp.refresh_candidate_recommendations()

    unknown = lp.candidate_snapshot()
    assert [row["market_id"] for row in unknown["candidates"]] == [
        "market-M03", "market-M01", "market-M02",
    ]
    unknown_m02 = unknown["candidates"][2]
    assert unknown_m02["estimate_state"] == "unknown"
    assert unknown_m02["estimated_yield_raw"] is None
    assert unknown_m02["state"] == "eligible"
    assert Decimal(str(unknown["candidates"][0]["estimated_yield_raw"])) == Decimal(
        "0.8"
    )


def test_candidate_pool_validity_boundary_and_autobackfill(tmp_path) -> None:
    """Issue #157 A5: an estimate stays valid through second 299 and leaves
    the published table at second 300 — by the clock alone, with no service
    calls in between — and the rank-eleven row automatically fills the
    displayed top ten as the expired rows vacate it.

    Queue order follows the optimistic bound (equal books: pool order), so
    batch one estimates the ten highest-yield markets at t0 and batch two
    the next ten at t0+50: the displayed top ten is exactly the batch-one
    set and expires first, while the younger rank-eleven+ rows survive."""
    now = datetime(2026, 9, 20, 10, tzinfo=UTC)
    current = {"now": now}
    pools = {
        f"M{index:02d}": Decimal(700 - (index - 1)) for index in range(1, 23)
    }
    exchange = _LPYieldBooksExchange(now, pools)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert [row["market_id"] for row in first["candidates"]] == [
        f"market-M{index:02d}" for index in range(1, 11)
    ]
    # Batch two: the next ten never-tried markets estimate at t0+50.
    current["now"] = now + timedelta(seconds=50)
    exchange.now = current["now"]
    second = lp.refresh_candidates(force=False)
    assert [row["market_id"] for row in second["candidates"]] == [
        f"market-M{index:02d}" for index in range(1, 11)
    ]
    assert second["candidate_valid_count"] == 20

    # Second 299: the displayed batch-one rows are one second from expiry
    # and still valid.
    current["now"] = now + timedelta(seconds=299)
    at_299 = lp.candidate_snapshot()
    assert [row["market_id"] for row in at_299["candidates"]] == [
        f"market-M{index:02d}" for index in range(1, 11)
    ]
    assert at_299["candidate_valid_count"] == 20

    # Second 300 — pure time advance, zero calls: the batch-one rows expire
    # and the batch-two rows fill the displayed table from rank one.
    current["now"] = now + timedelta(seconds=300)
    at_300 = lp.candidate_snapshot()
    assert [row["market_id"] for row in at_300["candidates"]] == [
        f"market-M{index:02d}" for index in range(11, 21)
    ]
    assert at_300["candidate_valid_count"] == 10
    assert at_300["recommendations"][0]["market_id"] == "market-M11"
    # Reading never refreshed the surviving rows' validity.
    assert at_300["candidates"][0]["updated_at"] == (
        (now + timedelta(seconds=50))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def test_candidate_pool_failed_refresh_keeps_row_until_original_expiry(
    tmp_path,
) -> None:
    """Issue #157 A6: a failed re-estimate at t=200s keeps the stored row
    until its original updated_at+300s expiry, marked refresh_failed, and a
    later successful re-estimate re-enters the table with fresh values."""
    now = datetime(2026, 9, 20, 11, tzinfo=UTC)
    current = {"now": now}
    pools = {"M01": Decimal(600), "M02": Decimal(590)}
    exchange = _LPYieldBooksExchange(now, pools)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert [row["market_id"] for row in first["candidates"]] == [
        "market-M01", "market-M02",
    ]
    original_updated = first["candidates"][0]["updated_at"]
    original_expires = first["candidates"][0]["expires_at"]

    # t=200s: every book read fails — the rows keep their values and are
    # marked refresh_failed with their timestamps untouched.
    exchange.omit_tokens = frozenset({
        "token-condition-M01-yes", "token-condition-M01-no",
        "token-condition-M02-yes", "token-condition-M02-no",
    })
    current["now"] = now + timedelta(seconds=200)
    exchange.now = current["now"]
    lp.refresh_candidate_recommendations()

    failed = lp.candidate_snapshot()
    assert [row["market_id"] for row in failed["candidates"]] == [
        "market-M01", "market-M02",
    ]
    assert all(row["refresh_failed"] is True for row in failed["candidates"])
    assert failed["candidate_failed_recent_count"] == 2
    assert failed["candidates"][0]["updated_at"] == original_updated
    assert failed["candidates"][0]["expires_at"] == original_expires

    # Second 299: still held by the original validity — a failed refresh
    # never extends it.
    current["now"] = now + timedelta(seconds=299)
    at_299 = lp.candidate_snapshot()
    assert [row["market_id"] for row in at_299["candidates"]] == [
        "market-M01", "market-M02",
    ]

    # Second 300: both rows age out exactly at their original expiry.
    current["now"] = now + timedelta(seconds=300)
    expired = lp.candidate_snapshot()
    assert expired["candidates"] == []
    assert expired["candidate_valid_count"] == 0
    assert expired["recommendations"] == []

    # After the per-market failure backoff the exploration loop re-estimates
    # both markets and they re-enter the table with fresh values and the
    # failure flag cleared.
    exchange.omit_tokens = frozenset()
    current["now"] = now + timedelta(seconds=350)
    exchange.now = current["now"]
    lp.refresh_candidates()

    recovered = lp.candidate_snapshot()
    assert [row["market_id"] for row in recovered["candidates"]] == [
        "market-M01", "market-M02",
    ]
    assert all(row["refresh_failed"] is False for row in recovered["candidates"])
    assert recovered["candidate_failed_recent_count"] == 0
    assert recovered["candidates"][0]["updated_at"] == (
        (now + timedelta(seconds=350))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def test_explore_batches_roll_through_queue_without_round_gates(tmp_path) -> None:
    """Issue #157 A1: with 60 processable markets, repeated refresh_candidates
    calls five seconds apart keep reading new markets — the 51st+ markets'
    tokens are read well inside the old 300-second window, every batch is one
    ≤20-token book read, no token is read twice, and the full catalog is
    never re-read (the base queues stay cached)."""
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    current = {"now": now}
    pools = {f"N{index:02d}": Decimal(600 - index) for index in range(1, 61)}

    class CountingExchange(_LPBatchQueryExchange):
        def __init__(self, initial_now: datetime) -> None:
            super().__init__(initial_now, pools)
            self.catalog_reads = 0

        def lp_reward_catalog(self, *, condition_ids=None, stop_event=None):
            self.catalog_reads += 1
            return super().lp_reward_catalog(
                condition_ids=condition_ids, stop_event=stop_event
            )

    exchange = CountingExchange(now)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    snapshot = None
    reads_after_first = None
    catalog_after_first = None
    for step in range(6):
        snapshot = lp.refresh_candidates(force=True)
        current["now"] = now + timedelta(seconds=5 * (step + 1))
        exchange.now = current["now"]
        if step == 0:
            reads_after_first = len(exchange.book_token_reads)
            catalog_after_first = exchange.catalog_reads

    assert snapshot is not None and snapshot["state"] == "ready"
    # Six batches of ten markets: one book read per call, 20 tokens each.
    assert len(exchange.book_token_reads) == reads_after_first + 5
    for batch in exchange.book_token_reads:
        assert len(batch) <= 20
    all_tokens = [
        token for batch in exchange.book_token_reads for token in batch
    ]
    assert len(all_tokens) == 120
    assert len(set(all_tokens)) == 120
    # The old 300-second round gate never stopped the roll: markets 51-60
    # were read by the sixth call, 25 seconds after the first.
    late_tokens = {
        token
        for batch in exchange.book_token_reads
        for token in batch
        if int(token.removeprefix("token-condition-N").split("-")[0]) >= 51
    }
    assert len(late_tokens) == 20
    # The full catalog was never re-read for later batches.
    assert exchange.catalog_reads == catalog_after_first
    funnel = snapshot["funnel"]
    assert funnel["checked"] == 60
    assert funnel["passed"] == 60
    assert funnel["batches"] == 6
    assert funnel["unchecked"] == 0
    assert snapshot["candidate_valid_count"] == 60
    assert snapshot["candidate_pending_count"] == 0


def test_explore_batch_failure_isolates_and_backs_off(tmp_path) -> None:
    """Issue #157 A2: a batch whose book read raises does not touch the
    batches already published — their rows stay readable with their original
    timestamps — and the failed batch's markets reschedule on the failure
    backoff without blocking other batches."""
    now = datetime(2026, 9, 20, 13, tzinfo=UTC)
    current = {"now": now}
    pools = {f"N{index:02d}": Decimal(600 - index) for index in range(1, 21)}

    class FailingBatchExchange(_LPBatchQueryExchange):
        def __init__(self, initial_now: datetime) -> None:
            super().__init__(initial_now, pools)
            self.fail_reads = False
            self.batch_requests: list[tuple[str, ...]] = []

        def lp_order_books(self, token_ids, *, stop_event=None):
            self.batch_requests.append(tuple(token_ids))
            if self.fail_reads:
                raise RuntimeError("book read failed")
            return super().lp_order_books(token_ids, stop_event=stop_event)

    exchange = FailingBatchExchange(now)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert [row["market_id"] for row in first["candidates"]] == [
        f"market-N{index:02d}" for index in range(1, 11)
    ]
    first_updated = first["candidates"][0]["updated_at"]

    # Batch two's book read raises: batch one's rows are untouched and the
    # batch-two markets enter the failure backoff instead of the pool.
    exchange.fail_reads = True
    current["now"] = now + timedelta(seconds=5)
    exchange.now = current["now"]
    failed = lp.refresh_candidates(force=True)

    assert failed["candidate_valid_count"] == 10
    assert [row["market_id"] for row in failed["candidates"]] == [
        f"market-N{index:02d}" for index in range(1, 11)
    ]
    assert failed["candidates"][0]["updated_at"] == first_updated
    assert failed["candidates"][0]["refresh_failed"] is False
    assert failed["funnel"]["unknown"] == 10

    # Before the backoff elapses the failed markets are skipped, but the
    # rolling rotation still re-estimates batch one — no blocking.
    exchange.fail_reads = False
    current["now"] = now + timedelta(seconds=30)
    exchange.now = current["now"]
    lp.refresh_candidates(force=True)

    def _batch_market_indexes(batch: tuple[str, ...]) -> set[int]:
        return {
            int(token.removeprefix("token-condition-N").split("-")[0])
            for token in batch
        }

    batch_two_requests = [
        batch
        for batch in exchange.batch_requests[2:]
        if any(index >= 11 for index in _batch_market_indexes(batch))
    ]
    assert batch_two_requests == []
    during = lp.candidate_snapshot()
    assert during["candidate_valid_count"] == 10
    assert during["candidates"][0]["updated_at"] != first_updated

    # Once the 60-second backoff has elapsed the failed markets are read
    # again and join the pool.
    current["now"] = now + timedelta(seconds=70)
    exchange.now = current["now"]
    recovered = lp.refresh_candidates(force=True)

    assert recovered["candidate_valid_count"] == 20
    assert recovered["candidate_pending_count"] == 0
    # The recovery batch re-read exactly the failed batch's markets.
    late_requests = [
        batch
        for batch in exchange.batch_requests[3:]
        if any(index >= 11 for index in _batch_market_indexes(batch))
    ]
    assert len(late_requests) == 1
    assert min(_batch_market_indexes(late_requests[0])) >= 11
    assert recovered["funnel"]["passed"] == 30


def test_candidate_pool_late_write_never_overwrites_newer_result(
    tmp_path,
) -> None:
    """Issue #157 A7: an evaluation judged before another writer's newer
    result never overwrites it — the one-line write protection rejects the
    late write — and each market keeps exactly one valid pool row."""
    now = datetime(2026, 9, 20, 14, tzinfo=UTC)
    current = {"now": now}
    pools = {"M01": Decimal(600), "M02": Decimal(590), "M03": Decimal(580)}
    exchange = _LPYieldBooksExchange(now, pools)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: current["now"])
    assert lp.refresh_price_history()["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert first["candidate_valid_count"] == 3

    # The reservations read is the last read before the exploration batch
    # publishes: latch it so the batch publishes only after the maintenance
    # has published a strictly newer result.  (Issue 165: the reservations
    # read goes through the plural lp_active_sessions selector.)
    latch = threading.Event()
    released = threading.Event()
    real_reader = store.lp_active_sessions

    def latched_reader():
        if latch.is_set() and not released.is_set():
            released.set()
            assert latch.wait(timeout=5)
        return real_reader()

    store.lp_active_sessions = latched_reader  # type: ignore[method-assign]

    current["now"] = now + timedelta(seconds=65)
    exchange.now = current["now"]
    latch.set()
    explore_result: dict[str, object] = {}

    def run_explore() -> None:
        explore_result["snapshot"] = lp.refresh_candidates(force=True)

    explore_thread = threading.Thread(target=run_explore)
    explore_thread.start()
    assert released.wait(timeout=5)

    # While the exploration batch is parked before its publication, the
    # maintenance reads a moved book (bid 0.30) and publishes a strictly
    # newer judgment for the same markets.
    moved_book = ([("0.30", "100"), ("0.29", "100")], [("0.32", "100")])
    exchange.books_by_token = {
        f"token-condition-{suffix}-{side}": moved_book
        for suffix in ("M01", "M02", "M03")
        for side in ("yes", "no")
    }
    current["now"] = now + timedelta(seconds=70)
    exchange.now = current["now"]
    maintained = lp.refresh_candidate_recommendations()
    assert maintained["candidates"][0]["updated_at"] == (
        (now + timedelta(seconds=70))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    assert Decimal(str(maintained["candidates"][0]["realtime_price"])) == Decimal(
        "0.30"
    )

    latch.set()
    explore_thread.join(timeout=5)
    assert not explore_thread.is_alive()

    # The late exploration write (judged at +65s) lost to the newer +70s
    # result: the pool keeps one row per market stamped by the maintenance.
    settled = lp.candidate_snapshot()
    assert settled["candidate_valid_count"] == 3
    assert len({
        row["condition_id"] for row in settled["candidates"]
    }) == 3
    for row in settled["candidates"]:
        assert row["updated_at"] == (
            (now + timedelta(seconds=70))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        # The maintenance's moved-book values survive: the exploration
        # batch's older-judged 0.34 write was rejected.
        assert Decimal(str(row["realtime_price"])) == Decimal("0.30")


def test_maintenance_renewals_do_not_stall_exploration(tmp_path) -> None:
    """Issue #157 A8: while the maintenance path keeps renewing the
    displayed rows between exploration calls, the exploration queue keeps
    advancing — every exploration call still reads new markets, and the
    maintenance cadence neither consumes the queue nor trips the
    exploration locks."""
    now = datetime(2026, 9, 20, 15, tzinfo=UTC)
    current = {"now": now}
    pools = {f"N{index:02d}": Decimal(630 - index) for index in range(1, 31)}
    exchange = _LPBatchQueryExchange(now, pools)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    def new_market_tokens(reads_before: int) -> set[str]:
        covered = {
            token
            for batch in exchange.book_token_reads[reads_before:]
            for token in batch
        }
        indexes = {
            int(token.removeprefix("token-condition-N").split("-")[0])
            for token in covered
        }
        return {f"market-N{index:02d}" for index in indexes}

    first = lp.refresh_candidates(force=True)
    assert len(first["candidates"]) == 10

    for step in range(1, 3):
        # The maintenance renews the displayed rows (one batch book read of
        # the same twenty tokens) and never touches the exploration queue.
        current["now"] = current["now"] + timedelta(seconds=65)
        exchange.now = current["now"]
        reads_before_maintenance = len(exchange.book_token_reads)
        maintained = lp.refresh_candidate_recommendations()
        maintenance_tokens = {
            token
            for batch in exchange.book_token_reads[reads_before_maintenance:]
            for token in batch
        }
        assert len(maintenance_tokens) == 20
        assert maintained["recommendations"]

        # The exploration call right after still reads the next batch of
        # never-tried markets.
        current["now"] = current["now"] + timedelta(seconds=2)
        exchange.now = current["now"]
        reads_before_explore = len(exchange.book_token_reads)
        explored = lp.refresh_candidates()
        newly_read = new_market_tokens(reads_before_explore)
        expected = {
            f"market-N{index:02d}"
            for index in range(10 * step + 1, 10 * step + 11)
        }
        assert newly_read == expected
        assert explored["funnel"]["batches"] == 1 + step
        assert explored["candidate_valid_count"] == 10 * (1 + step)


def test_hanging_competition_read_never_blocks_candidate_batches(
    tmp_path,
) -> None:
    """Issue #157 A9: a hanging or failing lp_market_competitiveness read
    never blocks the candidate batch path — batches publish yield results
    with unknown competition while the dedicated competition thread is the
    only caller of the reader."""
    now = datetime(2026, 9, 20, 16, tzinfo=UTC)
    current = {"now": now}
    pools = {"M01": Decimal(600), "M02": Decimal(590), "M03": Decimal(580)}
    exchange = _LPYieldBooksExchange(now, pools)

    latch = threading.Event()
    competition_calls = {"count": 0}

    def hanging_competition(*, stop_event=None, previous=None):
        competition_calls["count"] += 1
        assert latch.wait(timeout=5)
        raise RuntimeError("competition read failed")

    exchange.lp_market_competitiveness = hanging_competition  # type: ignore[method-assign]
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    # The batch path publishes although the competition reader would hang.
    snapshot = lp.refresh_candidates(force=True)

    assert competition_calls["count"] == 0
    assert snapshot["candidate_valid_count"] == 3
    assert [row["market_id"] for row in snapshot["candidates"]] == [
        "market-M01", "market-M02", "market-M03",
    ]
    assert snapshot["funnel"]["competition_state"] == "unknown"
    for row in snapshot["candidates"]:
        assert row["competition"]["state"] == "unknown"
        assert row["state"] == "eligible"

    # The dedicated cache refresh is the only competition caller: it blocks
    # on the hanging read, keeps the previous cache on failure, and never
    # propagates the error.
    competition_thread = threading.Thread(
        target=lp.refresh_competition_cache
    )
    competition_thread.start()
    deadline = time.monotonic() + 1.0
    while competition_calls["count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert competition_calls["count"] == 1
    assert competition_thread.is_alive()
    latch.set()
    competition_thread.join(timeout=5)
    assert not competition_thread.is_alive()
    assert lp.candidate_snapshot()["funnel"]["competition_state"] == "unknown"
    # The published rows are untouched by the failed competition round.
    assert lp.candidate_snapshot()["candidate_valid_count"] == 3


def test_candidate_pool_save_throttle_and_restart_restore(tmp_path) -> None:
    """Issue #157 A11: pool publications persist at most once every five
    seconds; a new service instance on the same store restores the pool
    rows with their original timestamps; the rows age out by the clock and
    reading never extends their validity."""
    now = datetime(2026, 9, 20, 17, tzinfo=UTC)
    current = {"now": now}
    pools = {"M01": Decimal(600), "M02": Decimal(590), "M03": Decimal(580)}
    exchange = _LPYieldBooksExchange(now, pools)
    store = PredictionArbitrageStore(tmp_path)

    class CountingStore:
        def __init__(self, inner: PredictionArbitrageStore) -> None:
            self._inner = inner
            self.save_count = 0

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

        def lp_save_screening_snapshot(self, payload):  # type: ignore[no-untyped-def]
            self.save_count += 1
            return self._inner.lp_save_screening_snapshot(payload)

    counting_store = CountingStore(store)
    lp = PolymarketLPService(
        counting_store, exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    lp.refresh_candidates(force=True)
    first_save_count = counting_store.save_count
    assert first_save_count == 1
    saved = store.lp_screening_snapshot()
    assert saved is not None and saved["pool"]
    assert set(saved["pool"]) == {
        "condition-M01", "condition-M02", "condition-M03",
    }
    assert saved["rotation"]["condition-M01"]["failures"] == 0

    # A publication one second later is throttled: nothing new persists.
    current["now"] = now + timedelta(seconds=1)
    exchange.now = current["now"]
    lp.refresh_candidates()
    assert counting_store.save_count == first_save_count
    assert store.lp_screening_snapshot()["pool"]["condition-M01"][
        "updated_at"
    ] == _iso_z(now)

    # After the five-second throttle the next publication persists.
    current["now"] = now + timedelta(seconds=6)
    exchange.now = current["now"]
    lp.refresh_candidates()
    assert counting_store.save_count == first_save_count + 1
    assert store.lp_screening_snapshot()["pool"]["condition-M01"][
        "updated_at"
    ] == _iso_z(now + timedelta(seconds=6))

    # A new instance on the same store restores the pool with the original
    # timestamps.
    current["now"] = now + timedelta(seconds=10)
    restored = PolymarketLPService(
        store, exchange, clock=lambda: current["now"]
    )
    projection = restored.candidate_snapshot()
    assert projection["candidate_valid_count"] == 3
    assert [row["market_id"] for row in projection["candidates"]] == [
        "market-M01", "market-M02", "market-M03",
    ]
    assert projection["candidates"][0]["updated_at"] == _iso_z(
        now + timedelta(seconds=6)
    )
    assert projection["candidates"][0]["expires_at"] == _iso_z(
        now + timedelta(seconds=306)
    )

    # The restored rows age out by the clock and reads never refresh them.
    current["now"] = now + timedelta(seconds=306)
    aged = restored.candidate_snapshot()
    assert aged["candidates"] == []
    assert aged["candidate_valid_count"] == 0
    assert counting_store.save_count == first_save_count + 1


def test_candidate_snapshot_has_no_whole_snapshot_expiry(tmp_path) -> None:
    """Issue #157 A10: reading the snapshot 61 seconds after publication
    keeps every row valid — no 60-second whole-snapshot downgrade, no
    candidate_source_stale / candidate_snapshot_stale reasons — and an
    empty pool reports the continuous status fields honestly."""
    now = datetime(2026, 9, 20, 18, tzinfo=UTC)
    current = {"now": now}
    pools = {"M01": Decimal(600), "M02": Decimal(590), "M03": Decimal(580)}
    exchange = _LPYieldBooksExchange(now, pools)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )

    # Zero candidates before anything ran: honest empty with the
    # continuous status fields.
    empty = lp.candidate_snapshot()
    assert empty["candidates"] == []
    assert empty["recommendations"] == []
    assert empty["candidate_valid_count"] == 0
    assert empty["candidate_pending_count"] == 0
    assert empty["candidate_failed_recent_count"] == 0

    assert lp.refresh_price_history()["state"] == "known"
    first = lp.refresh_candidates(force=True)
    assert first["candidate_valid_count"] == 3

    # 61 seconds after publication — well past the retired 60-second
    # whole-snapshot window — the rows are still current.
    current["now"] = now + timedelta(seconds=61)
    at_61 = lp.candidate_snapshot()
    assert [row["market_id"] for row in at_61["candidates"]] == [
        "market-M01", "market-M02", "market-M03",
    ]
    assert at_61["candidates"][0]["updated_at"] == _iso_z(now)
    assert all(row["state"] == "eligible" for row in at_61["candidates"])
    serialized = repr(at_61)
    assert "candidate_source_stale" not in serialized
    assert "candidate_snapshot_stale" not in serialized
    assert at_61["stale"] is False
    assert at_61["recommendations"][0]["market_id"] == "market-M01"


def test_refresh_candidates_mid_batch_failure_publishes_honestly(
    tmp_path,
) -> None:
    """Issue #157 review R3: a fake store read raising mid-batch must not
    leave the projection stuck on ``scanning`` — the failure publishes with
    the pool rows kept, ``retention_reason="candidate_refresh_failed"``,
    and the next batch recovers normally."""

    now = datetime(2026, 9, 21, 9, tzinfo=UTC)
    current = {"now": now}
    pools = {"M01": Decimal(600), "M02": Decimal(590), "M03": Decimal(580)}
    exchange = _LPCandidateQueryExchange(now, pools)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: current["now"])
    assert lp.refresh_price_history()["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert [row["market_id"] for row in first["candidates"]] == [
        "market-M01", "market-M02", "market-M03",
    ]
    original_updated = first["candidates"][0]["updated_at"]

    # The reservations read is the batch's last read before publication:
    # make it raise once, mid-batch, after the books were already read.
    # (Issue 165: the reservations read goes through the plural
    # lp_active_sessions selector.)
    real_reader = store.lp_active_sessions
    failure = {"on": False}

    def failing_reader():
        if failure["on"]:
            raise RuntimeError("reservations_read_failed")
        return real_reader()

    store.lp_active_sessions = failing_reader  # type: ignore[method-assign]

    failure["on"] = True
    current["now"] = now + timedelta(seconds=5)
    exchange.now = current["now"]
    failed = lp.refresh_candidates(force=True)

    assert failed["scanning"] is False
    assert failed["retention_reason"] == "candidate_refresh_failed"
    # The batch was not consumed and the pool rows are kept for display.
    assert failed["funnel"]["batches"] == 1
    assert [row["market_id"] for row in failed["candidates"]] == [
        "market-M01", "market-M02", "market-M03",
    ]
    assert failed["candidates"][0]["updated_at"] == original_updated

    # The next batch recovers normally.
    failure["on"] = False
    current["now"] = now + timedelta(seconds=10)
    exchange.now = current["now"]
    recovered = lp.refresh_candidates(force=True)

    assert recovered["scanning"] is False
    assert recovered["retention_reason"] is None
    assert recovered["funnel"]["batches"] == 2
    assert recovered["candidates"][0]["updated_at"] == _iso_z(
        now + timedelta(seconds=10)
    )


def test_maintenance_late_failure_never_mislabels_newer_row(tmp_path) -> None:
    """Issue #157 review R4: a whole-round maintenance failure judged before
    the stored row's ``updated_at`` is a late arrival — it must neither mark
    the newer row ``refresh_failed`` nor advance its failure ladder."""

    now = datetime(2026, 9, 21, 11, tzinfo=UTC)
    current = {"now": now}
    pools = {"M01": Decimal(600), "M02": Decimal(590)}
    exchange = _LPYieldBooksExchange(now, pools)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    assert lp.refresh_candidates(force=True)["candidate_valid_count"] == 2
    # A second exploration batch re-judges both markets at +40s; the queue
    # facts keep their build-era stamps, so maintenance stays lead-due.
    current["now"] = now + timedelta(seconds=40)
    exchange.now = current["now"]
    second = lp.refresh_candidates(force=True)
    newer_updated = second["candidates"][0]["updated_at"]

    # The books disappear for a maintenance round judged at +35s — before
    # the row's +40s judgment.  The late failure must not mislabel it.
    exchange.omit_tokens = frozenset({
        "token-condition-M01-yes", "token-condition-M01-no",
        "token-condition-M02-yes", "token-condition-M02-no",
    })
    current["now"] = now + timedelta(seconds=35)
    exchange.now = current["now"]
    lp.refresh_candidate_recommendations()

    snapshot = lp.candidate_snapshot()
    assert [row["market_id"] for row in snapshot["candidates"]] == [
        "market-M01", "market-M02",
    ]
    assert all(row["refresh_failed"] is False for row in snapshot["candidates"])
    assert snapshot["candidate_failed_recent_count"] == 0
    assert snapshot["candidates"][0]["updated_at"] == newer_updated
    # The round itself still counts as a maintenance failure.
    assert snapshot["maintenance_consecutive_failures"] == 1


def test_maintenance_late_rejection_never_evicts_newer_row(tmp_path) -> None:
    """Issue #157 review R4: a deterministic rejection judged before the
    stored row's ``updated_at`` is a late arrival — it must not evict the
    newer successful row from the pool."""

    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    current = {"now": now}
    pools = {"M01": Decimal(600), "M02": Decimal(590)}
    exchange = _LPCandidateQueryExchange(now, pools)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    assert lp.refresh_candidates(force=True)["candidate_valid_count"] == 2
    current["now"] = now + timedelta(seconds=40)
    exchange.now = current["now"]
    second = lp.refresh_candidates(force=True)
    newer_updated = second["candidates"][0]["updated_at"]

    # A maintenance round judged at +35s reads an off-tick book: its
    # rejection is older than the +40s rows and must not evict them.
    exchange.book_bid = Decimal("0.345")
    current["now"] = now + timedelta(seconds=35)
    exchange.now = current["now"]
    lp.refresh_candidate_recommendations()

    snapshot = lp.candidate_snapshot()
    assert [row["market_id"] for row in snapshot["candidates"]] == [
        "market-M01", "market-M02",
    ]
    assert all(row["state"] == "eligible" for row in snapshot["candidates"])
    assert all(row["refresh_failed"] is False for row in snapshot["candidates"])
    assert snapshot["candidates"][0]["updated_at"] == newer_updated


def test_maintenance_guard_rejected_success_is_not_a_failure(tmp_path) -> None:
    """Issue #157 review R4: a maintenance success rejected by the one-line
    write protection (a concurrent newer row already holds the line) is not
    a round failure — it must not advance the 60/120/300s backoff."""

    now = datetime(2026, 9, 21, 13, tzinfo=UTC)
    current = {"now": now}
    exchange = _LPCandidateQueryExchange(now, {"M01": Decimal(600)})
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    assert lp.refresh_candidates(force=True)["candidate_valid_count"] == 1
    current["now"] = now + timedelta(seconds=40)
    exchange.now = current["now"]
    second = lp.refresh_candidates(force=True)
    newer_updated = second["candidates"][0]["updated_at"]

    # A healthy maintenance round judged at +35s — before the +40s row.
    # Its write loses the one-line write protection, which is not a
    # failure: the backoff ladder stays at zero.
    current["now"] = now + timedelta(seconds=35)
    exchange.now = current["now"]
    lp.refresh_candidate_recommendations()

    snapshot = lp.candidate_snapshot()
    assert snapshot["candidates"][0]["updated_at"] == newer_updated
    assert snapshot["maintenance_consecutive_failures"] == 0
    assert snapshot["maintenance_next_attempt_at"] is None


def test_subsequent_publish_evicts_expired_rows_and_facts(tmp_path) -> None:
    """Issue #157 review R5: every publication first evicts the rows the
    current clock has expired together with their qualification facts — the
    pool, its persisted payload, and a restart restore cannot grow without
    bound or resurrect ghost rows."""

    now = datetime(2026, 9, 21, 14, tzinfo=UTC)
    current = {"now": now}
    pools = {f"M{index:02d}": Decimal(600 - index) for index in range(1, 61)}
    exchange = _LPYieldBooksExchange(now, pools)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: current["now"])
    assert lp.refresh_price_history()["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert [row["market_id"] for row in first["candidates"]] == [
        f"market-M{index:02d}" for index in range(1, 11)
    ]

    # Beyond the five-minute validity the next batch rolls to ten never-tried
    # markets; its publication evicts the ten expired rows and their facts.
    current["now"] = now + timedelta(seconds=310)
    exchange.now = current["now"]
    second = lp.refresh_candidates(force=True)
    assert [row["market_id"] for row in second["candidates"]] == [
        f"market-M{index:02d}" for index in range(11, 21)
    ]

    fresh_condition_ids = {f"condition-M{index:02d}" for index in range(11, 21)}
    saved_pool = store.lp_screening_snapshot()["pool"]
    assert set(saved_pool) == fresh_condition_ids
    # Qualification facts have no public projection; assert the service
    # state directly so the cleanup contract stays pinned.
    with lp._candidate_state_lock:
        assert set(lp._candidate_qualification_facts) == fresh_condition_ids

    # A restarted service restores the pruned pool — no expired ghost rows.
    restored = PolymarketLPService(store, exchange, clock=lambda: current["now"])
    with restored._candidate_state_lock:
        assert set(restored._candidate_pool) == fresh_condition_ids
    assert restored.candidate_snapshot()["candidate_valid_count"] == 10


def test_candidate_pending_count_counts_current_queue_members(tmp_path) -> None:
    """Issue #157 review R6: pending counts the current queue members that
    have never been tried — rotation stamps of markets that left the queue
    no longer mask untried ones."""

    now = datetime(2026, 9, 21, 15, tzinfo=UTC)
    current = {"now": now}
    pools = {f"M{index:02d}": Decimal(600 - index) for index in range(1, 31)}

    class ExitExchange(_LPCandidateQueryExchange):
        def __init__(self, initial_now: datetime) -> None:
            super().__init__(initial_now, pools)
            self.exit_suffixes: frozenset[str] = frozenset()

        def lp_reward_catalog(self, *, condition_ids=None, stop_event=None):
            catalog = super().lp_reward_catalog(
                condition_ids=condition_ids, stop_event=stop_event
            )
            rows = catalog.get("markets")
            adjusted = tuple(
                {**row, "reward_active": False}
                if str(row.get("condition_id") or "").removeprefix(
                    "condition-"
                ) in self.exit_suffixes
                else row
                for row in rows
            )
            return {**catalog, "markets": adjusted}

    exchange = ExitExchange(now)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"

    first = lp.refresh_candidates(force=True)
    assert first["funnel"]["checked"] == 10
    assert first["candidate_pending_count"] == 20

    # M09 and M10 leave the base queue (reward inactive); the rebuilt queue
    # keeps 28 members and their stale rotation stamps must not mask the
    # ten members still untried (M21-M30).
    exchange.exit_suffixes = frozenset({"M09", "M10"})
    current["now"] = now + timedelta(seconds=5)
    exchange.now = current["now"]
    assert lp.refresh_price_history()["state"] == "known"
    second = lp.refresh_candidates(force=True)

    assert second["funnel"]["queue_total"] == 28
    assert second["funnel"]["checked"] == 20
    assert second["candidate_pending_count"] == 10
    assert second["funnel"]["unchecked"] == 10


def test_healthy_batch_clears_stale_stop_reason(tmp_path) -> None:
    """Issue #157 review R7: the ``account_unavailable`` stop note reported
    by a failed batch is cleared by the next healthy batch — the projection
    never reports a stale stop reason after recovery."""

    now = datetime(2026, 9, 21, 16, tzinfo=UTC)
    current = {"now": now}
    pools = {f"N{index:02d}": Decimal(500 - index) for index in range(1, 4)}

    class Exchange(_LPBatchQueryExchange):
        def __init__(self, initial_now: datetime) -> None:
            super().__init__(initial_now, pools)
            self.account_mode = "valid"

        def lp_account_snapshot(self) -> dict[str, object]:
            if self.account_mode == "failure":
                raise RuntimeError("account_read_failed")
            return super().lp_account_snapshot()

    exchange = Exchange(now)
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: current["now"]
    )
    assert lp.refresh_price_history()["state"] == "known"
    assert lp.refresh_candidates(force=True)["state"] == "ready"

    exchange.account_mode = "failure"
    current["now"] = now + timedelta(seconds=65)
    exchange.now = current["now"]
    failed = lp.refresh_candidates(force=True)
    assert failed["funnel"]["stop_reason"] == "account_unavailable"

    # The account recovers: the next healthy batch publishes its own
    # semantics and no longer reports the stale stop note.
    exchange.account_mode = "valid"
    current["now"] = now + timedelta(seconds=66)
    exchange.now = current["now"]
    recovered = lp.refresh_candidates(force=True)
    assert recovered["state"] == "ready"
    assert recovered["funnel"]["stop_reason"] is None
    assert recovered["retention_reason"] is None


# ---- Issue 159: 首见基线兜底保护（评估层 E1-E7、终态 T） ----


class _FirstSeenExchange(_AccountReadExchange):
    """Issue 159 fake: scripted account open orders plus per-token books."""

    def __init__(self) -> None:
        super().__init__()
        self.books: dict[str, dict[str, object]] = {}
        self.book_calls: list[str] = []

    def set_book(
        self,
        token_id: str,
        *,
        level_total: object,
        now: datetime,
        condition_id: str = "0x" + "c" * 64,
        price: object = Decimal("0.30"),
    ) -> None:
        self.books[token_id] = {
            "condition_id": condition_id,
            "token_id": token_id,
            "received_at": now,
            "source_timestamp": "2026-09-21T11:59:59Z",
            "hash": "book-hash-first-seen",
            "bids": [{"price": Decimal(str(price)), "size": Decimal(str(level_total))}],
            "asks": [{"price": Decimal("0.31"), "size": Decimal("100")}],
        }

    def lp_order_books(
        self, token_ids: object, *, stop_event: object = None
    ) -> dict[str, dict[str, object]]:
        del stop_event
        result: dict[str, dict[str, object]] = {}
        for token_id in tuple(token_ids):  # type: ignore[arg-type]
            self.book_calls.append(str(token_id))
            book = self.books.get(str(token_id))
            if book is not None:
                result[str(token_id)] = book
        return result


_FIRST_SEEN_TOKEN = "0x" + "1" * 64
_FIRST_SEEN_CONDITION = "0x" + "c" * 64


def _first_seen_episode(
    store: PredictionArbitrageStore,
    episode_id: str,
    *,
    baseline_front: object = "8000",
    anchors: tuple[str, ...] = ("m-1",),
    token_id: str = _FIRST_SEEN_TOKEN,
    condition_id: str = _FIRST_SEEN_CONDITION,
    price: object = "0.30",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "token_id": token_id,
        "condition_id": condition_id,
        "anchor_order_ids": list(anchors),
        "anchor_price": str(price),
        "baseline_front": baseline_front,
        "baseline_price": str(price),
        "baseline_book_received_at": "2026-09-21T11:59:58Z",
        "baseline_book_hash": "book-hash-first-seen",
        "baseline_source": "first_observation",
        "baseline_version": 1,
        "threshold": Decimal("0.5"),
        "first_seen_at": "2026-09-21T11:59:57Z",
        "registration_delay": 1.0,
        "data_failures": 0,
        "state": "monitoring",
        "notification_sent": False,
        "blocked_notified": False,
        "cancel_scope": "own_buys_at_level",
        "cancel_targets": [],
        "canceled_order_ids": [],
        "cancel_target_remaining": {},
        "canceled_remaining": None,
        "partially_filled_quantity": None,
        "cancel_requested_at": None,
        "cancel_reason": None,
        "reason_codes": [],
    }
    return store.lp_create_first_seen_episode(
        episode_id,
        token_id=token_id,
        condition_id=condition_id,
        state="monitoring",
        payload=payload,
    )


def _first_seen_running_service(
    tmp_path,
    now: datetime,
    *,
    episode_id: str,
    level_total: object,
    open_orders: list[dict[str, object]],
    baseline_front: object = "8000",
    anchors: tuple[str, ...] = ("m-1",),
    notifier: object | None = None,
    guard: object = None,
):
    store = PredictionArbitrageStore(tmp_path)
    exchange = _FirstSeenExchange()
    exchange.set_book(
        _FIRST_SEEN_TOKEN, level_total=level_total, now=now
    )
    exchange.account_open_orders = list(open_orders)
    service = PolymarketLPService(
        store, exchange, clock=lambda: now, mutation_guard=guard
    )
    if notifier is not None:
        service.set_protection_notifier(notifier)
    _first_seen_episode(
        store,
        episode_id,
        baseline_front=baseline_front,
        anchors=anchors,
    )
    return store, exchange, service


def test_tick_without_active_session_runs_first_seen_protections(tmp_path) -> None:
    """Issue 165 护栏：无活动 LP 会话时 tick() 仍先跑 #159 首见保护，
    并返回 {"state": "none", "session_id": None}。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    anchor = _queue_receipt("m-1")
    store, exchange, service = _first_seen_running_service(
        tmp_path, now, episode_id="ep-none", level_total="4000",
        open_orders=[anchor],
    )
    result = service.tick()
    assert result == {"state": "none", "session_id": None}
    episode = store.lp_first_seen_episode("ep-none")
    assert episode is not None and episode["state"] == "canceling"
    assert exchange.cancels == ["m-1"]


def test_reconcile_session_terminal_guard_returns_status_without_writes(
    tmp_path,
) -> None:
    """Issue 165: 组级对账对终态会话短路——返回与 _status_payload 相等的
    载荷且不写库（updated_at 不变）。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp-reconcile-terminal"
    )
    session_id = str(started["session_id"])
    store.lp_update_session(session_id, state="complete")
    session = store.lp_session(session_id)
    assert session is not None
    updated_at_before = session["updated_at"]

    result = service._reconcile_session(session)

    assert result == service._status_payload(session)
    after = store.lp_session(session_id)
    assert after is not None
    assert after["updated_at"] == updated_at_before


def test_first_seen_examples_one_and_two_monitor_without_cancel(
    tmp_path,
) -> None:
    """E1: 总量 10,000 → 前方 8,000、A=80% 监控不撤；
    E2: 总量 6,000 → 前方 min(8000,4000)=4,000、A=66.67% 不撤。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    live = _queue_receipt("m-1")

    store, exchange, service = _first_seen_running_service(
        tmp_path, now, episode_id="ep-e1", level_total="10000",
        open_orders=[live],
    )
    service.tick()
    episode = store.lp_first_seen_episode("ep-e1")
    assert episode is not None and episode["state"] == "monitoring"
    assert Decimal(str(episode["front_estimate"])) == Decimal("8000")
    assert Decimal(str(episode["level_total"])) == Decimal("10000")
    assert Decimal(str(episode["ratio"])) == Decimal("0.80")
    assert exchange.cancels == []

    store2, exchange2, service2 = _first_seen_running_service(
        tmp_path / "e2", now, episode_id="ep-e2", level_total="6000",
        open_orders=[live],
    )
    service2.tick()
    episode2 = store2.lp_first_seen_episode("ep-e2")
    assert episode2 is not None and episode2["state"] == "monitoring"
    assert Decimal(str(episode2["front_estimate"])) == Decimal("4000")
    assert Decimal(str(episode2["level_total"])) == Decimal("6000")
    assert Decimal(str(episode2["ratio"])) == Decimal("4000") / Decimal("6000")
    assert exchange2.cancels == []


def test_first_seen_examples_three_and_four_trigger_at_half(
    tmp_path,
) -> None:
    """E3: 总量 4,000 → A=50% 触发；E4: 总量 16,000（后方新增 6,000）→
    前方仍 8,000、A=50% 触发，新增不进前方。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    live = _queue_receipt("m-1")

    store, exchange, service = _first_seen_running_service(
        tmp_path, now, episode_id="ep-e3", level_total="4000",
        open_orders=[live],
    )
    service.tick()
    episode = store.lp_first_seen_episode("ep-e3")
    assert episode is not None and episode["state"] == "canceling"
    assert Decimal(str(episode["ratio"])) == Decimal("0.50")
    assert exchange.cancels == ["m-1"]

    store2, exchange2, service2 = _first_seen_running_service(
        tmp_path / "e4", now, episode_id="ep-e4", level_total="16000",
        open_orders=[live],
    )
    service2.tick()
    episode2 = store2.lp_first_seen_episode("ep-e4")
    assert episode2 is not None and episode2["state"] == "canceling"
    assert Decimal(str(episode2["front_estimate"])) == Decimal("8000")
    assert Decimal(str(episode2["level_total"])) == Decimal("16000")
    assert Decimal(str(episode2["ratio"])) == Decimal("0.50")
    assert exchange2.cancels == ["m-1"]


def test_first_seen_cancel_scope_is_anchor_price_buys_only(tmp_path) -> None:
    """E5(范围): SELL、非锚价 BUY 不被撤；同锚价全部自己 BUY 进入撤单。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    anchor = _queue_receipt("m-1")
    manual_same = _queue_receipt("m-manual", original="1000")
    other_price = _queue_receipt("m-other-price", price="0.31")
    sell_row = _queue_receipt("m-sell", side="SELL", original="500")
    store, exchange, service = _first_seen_running_service(
        tmp_path, now, episode_id="ep-e5", level_total="4000",
        open_orders=[anchor, manual_same, other_price, sell_row],
        anchors=("m-1",),
    )
    service.tick()
    episode = store.lp_first_seen_episode("ep-e5")
    assert episode is not None and episode["state"] == "canceling"
    assert exchange.cancels == ["m-1", "m-manual"]
    assert episode["cancel_targets"] == ["m-1", "m-manual"]


def test_first_seen_conservative_cancel_after_ten_book_outages(
    tmp_path,
) -> None:
    """E5(保守撤): 行情连续失效 10 次 → 保守撤（book_unreliable）。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    anchor = _queue_receipt("m-1")
    manual_same = _queue_receipt("m-manual", original="1000")
    store, exchange, service = _first_seen_running_service(
        tmp_path, now, episode_id="ep-e5b", level_total="10000",
        open_orders=[anchor, manual_same],
    )
    exchange.books.clear()  # book read unavailable for every tick

    for expected in range(1, 10):
        service.tick()
        episode = store.lp_first_seen_episode("ep-e5b")
        assert episode is not None
        assert Decimal(str(episode["data_failures"])) == expected
        assert exchange.cancels == []

    service.tick()
    episode = store.lp_first_seen_episode("ep-e5b")
    assert episode is not None
    assert episode["state"] == "canceling"
    assert episode["cancel_reason"] == "book_unreliable"
    assert exchange.cancels == ["m-1", "m-manual"]


def test_first_seen_cancel_episode_closes_with_receipts_and_notice(
    tmp_path,
) -> None:
    """E6(闭环): 触发 → 每目标先 lp_actions pending 再撤；撤中再成交 →
    partially_filled 如实记账；重复 tick 不重复撤、不扩量；回执后一次性
    通知（文案含「首见基线」）。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    notes: list[tuple[str, str, str]] = []

    def collect(title: str, message: str, xiaoai_text: str) -> None:
        notes.append((title, message, xiaoai_text))

    anchor = _queue_receipt("m-1")
    manual_same = _queue_receipt("m-manual", original="1000")
    store, exchange, service = _first_seen_running_service(
        tmp_path, now, episode_id="ep-e6", level_total="4000",
        open_orders=[anchor, manual_same], anchors=("m-1",),
        notifier=collect,
    )
    service.tick()

    # 每目标先 pending（意图）再撤，回执 accepted。
    for order_id in ("m-1", "m-manual"):
        key = f"ep-e6:first-seen-protection-cancel:{order_id}"
        actions = [
            action for action in store.lp_actions(LP_RESERVED_MANUAL_SESSION_ID)
            if action["action_key"] == key
        ]
        assert len(actions) == 1
        assert actions[0]["state"] == "accepted"
        assert actions[0]["targets"] == [order_id]
    episode = store.lp_first_seen_episode("ep-e6")
    assert episode is not None
    assert episode["cancel_targets"] == ["m-1", "m-manual"]

    # 撤中再成交 500：回执 CANCELED 且 size_matched=500；m-manual 从所有
    # 读取路径消失 = canceled。
    filling = _queue_receipt("m-1", status="CANCELED", matched="500")
    exchange.account_open_orders = [filling]
    service.tick()
    episode = store.lp_first_seen_episode("ep-e6")
    assert episode is not None
    assert episode["state"] == "partially_filled"
    assert Decimal(str(episode["partially_filled_quantity"])) == Decimal("500")
    # 撤单调用仍只有一轮的两笔，不重复撤、不扩量。
    assert exchange.cancels == ["m-1", "m-manual"]
    assert len(exchange.posts) == 0

    # 一次性通知：恰一条成功通知，文案含「首见基线」。
    assert len(notes) == 1
    title, message, xiaoai_text = notes[0]
    assert "首见基线" in title
    assert "首见基线" in message


def test_first_seen_notice_identifies_orders_and_plain_language_lifetime(
    tmp_path,
) -> None:
    """The first-seen success notice explains shares, orders, and lifetime."""

    class NoticeExchange(_FirstSeenExchange):
        def __init__(
            self, acknowledged_at: datetime, clock_cell: list[datetime]
        ) -> None:
            super().__init__()
            self.acknowledged_at = acknowledged_at
            self.clock_cell = clock_cell

        def cancel_order(self, order_id: str) -> object:
            self.cancels.append(order_id)
            self.clock_cell[0] = self.acknowledged_at
            return {"canceled": [order_id], "status": "CANCELED"}

    token = _FIRST_SEEN_TOKEN
    condition = _FIRST_SEEN_CONDITION
    placed_anchor = datetime(2026, 9, 21, 10, 7, tzinfo=UTC)
    first_seen = datetime(2026, 9, 21, 10, 7, 19, tzinfo=UTC)
    placed_second = datetime(2026, 9, 21, 10, 20, tzinfo=UTC)
    placed_third = datetime(2026, 9, 21, 10, 30, tzinfo=UTC)
    trigger_at = datetime(2026, 9, 21, 11, 8, 10, tzinfo=UTC)
    acknowledged_at = datetime(2026, 9, 21, 11, 8, 11, tzinfo=UTC)

    def order(
        order_id: str,
        quantity: str,
        placed_at: datetime,
        *,
        order_token: str = token,
        price: str = "0.123",
        side: str = "BUY",
    ) -> dict[str, object]:
        return {
            "order_id": order_id,
            "condition_id": condition,
            "token_id": order_token,
            "side": side,
            "status": "LIVE",
            "price": price,
            "original_size": quantity,
            "size_matched": "0",
            "remaining_size": quantity,
            "remaining": quantity,
            "created_at": placed_at,
            "market_title": "Treasury yield below 4.20%?",
            "market_url": "https://polymarket.com/event/treasury-test",
            "outcome": "Yes",
        }

    anchor = order("notice-anchor", "50", placed_anchor)
    second = order("notice-second", "150", placed_second)
    third = order("notice-third", "50", placed_third)
    unrelated_token = order("notice-other-token", "25", placed_second, order_token="other-token")
    unrelated_price = order("notice-other-price", "25", placed_second, price="0.124")
    unrelated_sell = order("notice-sell", "25", placed_second, side="SELL")

    store = PredictionArbitrageStore(tmp_path)
    clock_cell = [trigger_at]
    exchange = NoticeExchange(acknowledged_at, clock_cell)
    exchange.set_book(token, level_total="284", now=first_seen, condition_id=condition, price="0.123")
    service = PolymarketLPService(store, exchange, clock=lambda: clock_cell[0])
    notes: list[tuple[str, str, str]] = []
    service.set_protection_notifier(lambda title, message, voice: notes.append((title, message, voice)))

    registered = service.register_first_seen_candidates(
        [anchor], now=first_seen
    )
    assert registered["state"] == "registered"

    exchange.set_book(token, level_total="384", now=trigger_at, condition_id=condition, price="0.123")
    exchange.account_open_orders = [
        anchor,
        second,
        third,
        unrelated_token,
        unrelated_price,
        unrelated_sell,
    ]
    service.tick()

    assert exchange.cancels == ["notice-anchor", "notice-second", "notice-third"]
    assert len(notes) == 1
    title, message, voice = notes[0]
    assert "Treasury yield below 4.20%?" in message
    assert "https://polymarket.com/event/treasury-test" in message
    assert "BUY YES" in message
    assert "0.123" in message
    assert "250" in message
    assert all(order_id in message for order_id in exchange.cancels)
    assert "50" in message and "150" in message
    assert "估计" in message
    assert "前约34.9%" in message
    assert "前50%" in message and "保护范围" in message
    assert "按份额" in message and "不是订单数" in message
    assert "首次挂单：北京时间 2026-09-21 18:07:00" in message
    assert "撤单完成：北京时间 2026-09-21 19:08:11" in message
    assert "挂单存续：1小时1分11秒" in message
    assert "首次观察订单时建立的盘口基线" in message
    assert "数据时间：北京时间 2026-09-21 19:08:10" in message
    assert "notice-other-token" not in message
    assert "notice-other-price" not in message
    assert "notice-sell" not in message
    assert "撤单" in title and "首见基线" in title
    assert voice.startswith(title)

    stored = store.lp_first_seen_episode(str(registered["episode_id"]))
    assert stored is not None
    assert stored["order_placement_times"] == {
        "notice-anchor": "2026-09-21T10:07:00.000000Z",
        "notice-second": "2026-09-21T10:20:00.000000Z",
        "notice-third": "2026-09-21T10:30:00.000000Z",
    }
    assert stored["order_cancel_confirmed_at"] == {
        "notice-anchor": "2026-09-21T11:08:11.000000Z",
        "notice-second": "2026-09-21T11:08:11.000000Z",
        "notice-third": "2026-09-21T11:08:11.000000Z",
    }

    restarted = PolymarketLPService(store, exchange, clock=lambda: clock_cell[0])
    restarted.set_protection_notifier(lambda title, message, voice: notes.append((title, message, voice)))
    restarted.tick()
    assert exchange.cancels == ["notice-anchor", "notice-second", "notice-third"]
    assert len(notes) == 1


def test_legacy_first_seen_notice_uses_local_identity_and_observed_lifetime(
    tmp_path,
) -> None:
    """An old episode uses only the non-expired local identity cache."""

    class LegacyExchange(_FirstSeenExchange):
        def __init__(self, acknowledged_at: datetime, clock_cell: list[datetime]) -> None:
            super().__init__()
            self.acknowledged_at = acknowledged_at
            self.clock_cell = clock_cell
            self.external_metadata_calls = 0

        def cancel_order(self, order_id: str) -> object:
            self.cancels.append(order_id)
            self.clock_cell[0] = self.acknowledged_at
            return {"canceled": [order_id], "status": "CANCELED"}

        def lp_market_metadata(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            self.external_metadata_calls += 1
            raise AssertionError("protection must not fetch external metadata")

    token = _FIRST_SEEN_TOKEN
    condition = _FIRST_SEEN_CONDITION
    first_seen = datetime(2026, 9, 21, 10, 7, 19, tzinfo=UTC)
    trigger_at = datetime(2026, 9, 21, 11, 8, 10, tzinfo=UTC)
    acknowledged_at = datetime(2026, 9, 21, 11, 8, 11, tzinfo=UTC)
    clock_cell = [trigger_at]
    store = PredictionArbitrageStore(tmp_path)
    exchange = LegacyExchange(acknowledged_at, clock_cell)
    exchange.set_book(token, level_total="4000", now=trigger_at, condition_id=condition)
    exchange.account_open_orders = [_queue_receipt("m-1")]
    service = PolymarketLPService(store, exchange, clock=lambda: clock_cell[0])
    notes: list[tuple[str, str, str]] = []
    service.set_protection_notifier(
        lambda title, message, voice: notes.append((title, message, voice))
    )
    _first_seen_episode(store, "ep-legacy", baseline_front="8000")
    store.lp_update_first_seen_episode(
        "ep-legacy", patch={"first_seen_at": first_seen.isoformat()}
    )
    store.lp_metadata_cache_store_entries(
        {
            condition: (
                (trigger_at + timedelta(hours=1)).timestamp(),
                {
                    "market_title": "Treasury yield below 4.20%?",
                    "market_url": "https://polymarket.com/event/treasury-test",
                    "outcomes": {"yes": {"token_id": token, "label": "Yes"}},
                },
            )
        }
    )

    service.tick()

    assert exchange.cancels == ["m-1"]
    assert exchange.external_metadata_calls == 0
    assert len(notes) == 1
    _, message, _ = notes[0]
    assert "Treasury yield below 4.20%?" in message
    assert "https://polymarket.com/event/treasury-test" in message
    assert "BUY YES" in message
    assert "首次挂单：未知" in message
    assert "首次观察：北京时间 2026-09-21 18:07:19" in message
    assert "观察到的存续时长：1小时52秒" in message
    assert "2026-09-21 18:07:19" not in message.split("首次挂单：未知", 1)[0]


@pytest.mark.parametrize("placement_kind", ("missing", "invalid", "future"))
def test_protection_notice_missing_facts_stays_explicit(
    tmp_path, placement_kind: str
) -> None:
    """Receipt convergence keeps missing identity, quantity, and time explicit."""

    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    order_id = "legacy-case6-order"
    condition = _FIRST_SEEN_CONDITION
    token = _FIRST_SEEN_TOKEN
    first_seen = now - timedelta(seconds=3)

    class LegacyReceiptExchange(_FirstSeenExchange):
        def __init__(self) -> None:
            super().__init__()
            self.external_metadata_calls = 0

        def lp_market_metadata(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            self.external_metadata_calls += 1
            raise AssertionError("protection must not fetch external metadata")

    live = {
        "order_id": order_id,
        "token_id": token,
        "side": "BUY",
        "status": "LIVE",
        "price": Decimal("0.30"),
    }
    canceled = {**live, "status": "CANCELED"}
    exchange = LegacyReceiptExchange()
    exchange.set_book(token, level_total="4000", now=now, condition_id=condition)
    exchange.account_open_orders = [live]
    exchange.cancel_responses = [{"not_canceled": {order_id: "venue_busy"}}]
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    notes: list[tuple[str, str, str]] = []
    service.set_protection_notifier(
        lambda title, message, voice: notes.append((title, message, voice))
    )
    _first_seen_episode(
        store, "ep-case6", baseline_front="8000", anchors=(order_id,)
    )
    placement_values = {
        "missing": {},
        "invalid": {order_id: "not-a-timestamp"},
        "future": {order_id: "2026-09-21T13:00:00Z"},
    }
    store.lp_update_first_seen_episode(
        "ep-case6",
        patch={
            "first_seen_at": first_seen.isoformat(),
            "order_placement_times": placement_values[placement_kind],
        },
    )

    # Existing outage protection reaches the same cancel seam without using
    # the unavailable per-order remaining field.
    exchange.books.clear()
    for _ in range(9):
        service.tick()
    episode = store.lp_first_seen_episode("ep-case6")
    assert episode is not None
    assert episode["state"] == "monitoring"
    service.tick()
    episode = store.lp_first_seen_episode("ep-case6")
    assert episode is not None
    assert episode["state"] == "canceling"
    assert exchange.cancels == [order_id]
    assert notes == []

    exchange.account_open_orders = [canceled]
    service.tick()

    assert exchange.external_metadata_calls == 0
    assert len(notes) == 1
    _, message, _ = notes[0]
    assert f"condition_id={condition}" in message
    assert f"token_id={token}" in message
    assert "选项未知" in message
    assert "撤单订单（撤单时余量）：legacy-case6-order=UNKNOWN份" in message
    assert "合计余量 UNKNOWN 份" in message
    assert "首次挂单：未知（未取得有效的实际交易所下单时间）" in message
    assert "撤单完成：北京时间 2026-09-21 20:00:00" in message
    assert "首次观察：北京时间 2026-09-21 19:59:57" in message
    assert "观察到的存续时长：3秒" in message
    assert "挂单存续：" not in message
    assert "挂单存续：-" not in message
    assert "A 比例" not in message
    assert "%" not in message


def test_registered_notice_retry_uses_last_confirmation_and_persisted_order_times(
    tmp_path,
) -> None:
    """A retry keeps the first placement and latest successful acknowledgement."""

    setup_at = datetime(2026, 9, 20, 9, 59, tzinfo=UTC)
    first_ack = datetime(2026, 9, 21, 11, 0, tzinfo=UTC)
    final_ack = datetime(2026, 9, 21, 11, 8, 11, tzinfo=UTC)
    clock_cell = [setup_at]

    class RetryExchange(_Exchange):
        def __init__(self) -> None:
            super().__init__()
            self.acknowledgements = [first_ack, final_ack]

        def cancel_order(self, order_id: str) -> object:
            self.cancels.append(order_id)
            response = self.cancel_responses.pop(0)
            if isinstance(response, dict) and response.get("canceled"):
                clock_cell[0] = self.acknowledgements.pop(0)
            return response

    condition = "0x" + "c" * 64
    token = "0x" + "1" * 64
    title = "Treasury yield below 4.20%?"
    url = "https://polymarket.com/event/treasury-test"

    def receipt(
        order_id: str,
        quantity: str,
        placed_at: datetime,
        *,
        status: str = "LIVE",
    ) -> dict[str, object]:
        row = _queue_receipt(order_id, original=quantity, status=status)
        row.update(
            {
                "condition_id": condition,
                "token_id": token,
                "created_at": placed_at,
                "market_title": title,
                "market_url": url,
                "outcome": "YES",
            }
        )
        return row

    exchange = RetryExchange()
    exchange.snapshot_value = _queue_runtime_snapshot(setup_at, bid_size="10000")
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(
        store, exchange, clock=lambda: clock_cell[0]
    )
    request = {
        **_request(setup_at),
        "market_title": title,
        "market_url": url,
        "outcome": "YES",
        "quantity": Decimal("2000"),
        "review_at": final_ack + timedelta(hours=1),
    }
    preview = service.preview(request)
    started = service.start(str(preview["preview_id"]), "lp-notice-retry")
    assert started["state"] == "entry_open"
    entry_id = str(started["entry_order_id"])
    manual_id = "manual-notice"
    entry = receipt(entry_id, "2000", datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
    manual = receipt(manual_id, "1000", datetime(2026, 9, 21, 10, 30, tzinfo=UTC))
    trigger_at = datetime(2026, 9, 21, 11, 0, tzinfo=UTC)
    exchange.snapshot_value = _queue_runtime_snapshot(
        trigger_at,
        bid_size="4000",
        orders=[entry, manual],
        open_orders=[entry, manual],
    )
    exchange.snapshot_value["scoring"] = True
    exchange.cancel_responses = [
        {"canceled": [entry_id], "status": "CANCELED"},
        {"not_canceled": {manual_id: "venue_busy"}},
        {"canceled": [manual_id], "status": "CANCELED"},
    ]
    clock_cell[0] = trigger_at
    notes: list[tuple[str, str, str]] = []
    service.set_protection_notifier(
        lambda notice_title, message, voice: notes.append(
            (notice_title, message, voice)
        )
    )

    first = service.tick()
    protection = first["queue_protection"]
    assert protection["state"] == "canceling"
    assert protection["cancel_failed"] == [manual_id]
    assert protection["order_placement_times"] == {
        entry_id: "2026-09-20T10:00:00.000000Z",
        manual_id: "2026-09-21T10:30:00.000000Z",
    }
    assert protection["order_cancel_confirmed_at"] == {
        entry_id: "2026-09-21T11:00:00.000000Z",
    }
    assert notes == []

    # The entry has disappeared from open orders; its terminal receipt lets
    # the restarted service converge the completed target and retry manual.
    clock_cell[0] = final_ack
    exchange.snapshot_value = _queue_runtime_snapshot(
        final_ack,
        bid_size="4000",
        orders=[receipt(entry_id, "2000", datetime(2026, 9, 20, 10, 0, tzinfo=UTC), status="CANCELED"), manual],
        open_orders=[manual],
    )
    exchange.snapshot_value["scoring"] = True
    restarted = PolymarketLPService(
        store, exchange, clock=lambda: clock_cell[0]
    )
    restarted.set_protection_notifier(
        lambda notice_title, message, voice: notes.append(
            (notice_title, message, voice)
        )
    )
    final = restarted.tick()
    assert exchange.cancels == [entry_id, manual_id, manual_id]
    assert final["queue_protection"]["state"] == "canceling"
    assert final["queue_protection"]["notification_sent"] is True
    assert final["queue_protection"]["order_cancel_confirmed_at"] == {
        entry_id: "2026-09-21T11:00:00.000000Z",
        manual_id: "2026-09-21T11:08:11.000000Z",
    }
    assert len(notes) == 1
    _, message, _ = notes[0]
    assert title in message and url in message and "BUY YES" in message
    assert "首次挂单：北京时间 2026-09-20 18:00:00" in message
    assert "撤单完成：北京时间 2026-09-21 19:08:11" in message
    assert "挂单存续：25小时8分11秒" in message
    assert "提交时基线" in message
    assert "已撤 2 张买单合计余量 3000 份" in message
    assert entry_id in message and manual_id in message

    exchange.snapshot_value = _queue_runtime_snapshot(
        final_ack,
        bid_size="4000",
        orders=[
            receipt(
                entry_id,
                "2000",
                datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
                status="CANCELED",
            ),
            receipt(
                manual_id,
                "1000",
                datetime(2026, 9, 21, 10, 30, tzinfo=UTC),
                status="CANCELED",
            ),
        ],
        open_orders=[],
    )
    restarted.tick()
    assert exchange.cancels == [entry_id, manual_id, manual_id]
    assert len(notes) == 1


@pytest.mark.parametrize("path", ("first_seen", "submit"))
def test_protection_notice_does_not_report_filled_targets_as_canceled(
    tmp_path, path: str
) -> None:
    """A filled target after an unacknowledged cancel is not reported as canceled."""

    now = datetime(2026, 9, 21, 11, 8, 10, tzinfo=UTC)
    token = _FIRST_SEEN_TOKEN
    condition = _FIRST_SEEN_CONDITION
    order_id = "filled-first" if path == "first_seen" else "order-1"
    notes: list[tuple[str, str, str]] = []

    if path == "first_seen":
        store = PredictionArbitrageStore(tmp_path)
        exchange = _FirstSeenExchange()
        exchange.set_book(token, level_total="10000", now=now, condition_id=condition)
        live = _queue_receipt(order_id, original="2000")
        live.update(
            {
                "condition_id": condition,
                "token_id": token,
                "created_at": now - timedelta(minutes=1),
                "market_title": "Treasury yield below 4.20%?",
                "market_url": "https://polymarket.com/event/treasury-test",
                "outcome": "Yes",
            }
        )
        exchange.account_open_orders = [live]
        service = PolymarketLPService(
            store,
            exchange,
            clock=lambda: now,
            mutation_guard=lambda *args, **kwargs: True,
        )
        service.set_protection_notifier(
            lambda title, message, voice: notes.append((title, message, voice))
        )
        registered = service.register_first_seen_candidates(
            [
                {
                    "order_id": order_id,
                    "condition_id": condition,
                    "token_id": token,
                    "price": Decimal("0.30"),
                    "remaining": Decimal("2000"),
                    "created_at": live["created_at"],
                    "first_seen_at": now,
                    "market_title": live["market_title"],
                    "market_url": live["market_url"],
                    "outcome": live["outcome"],
                }
            ],
            now=now,
        )
        assert registered["state"] == "registered"
        exchange.set_book(token, level_total="4000", now=now, condition_id=condition)
        episode_id = str(registered["episode_id"])
    else:
        store = PredictionArbitrageStore(tmp_path)
        exchange = _Exchange()
        exchange.snapshot_value = _queue_runtime_snapshot(now, bid_size="10000")
        service = PolymarketLPService(store, exchange, clock=lambda: now)
        service.set_protection_notifier(
            lambda title, message, voice: notes.append((title, message, voice))
        )
        request = {
            **_request(now),
            "market_title": "Treasury yield below 4.20%?",
            "market_url": "https://polymarket.com/event/treasury-test",
            "review_at": now + timedelta(hours=1),
            "quantity": Decimal("2000"),
        }
        preview = service.preview(request)
        started = service.start(str(preview["preview_id"]), "lp-filled-target")
        assert started["state"] == "entry_open"
        entry_id = str(started["entry_order_id"])
        live = _queue_receipt(entry_id, original="2000")
        live.update(
            {
                "condition_id": condition,
                "token_id": token,
                "created_at": now - timedelta(minutes=1),
            }
        )
        trigger = _queue_runtime_snapshot(
            now, bid_size="4000", orders=[live], open_orders=[live]
        )
        trigger["scoring"] = True
        exchange.snapshot_value = trigger
        episode_id = str(started["session_id"])

    exchange.cancel_responses = [
        {"not_canceled": {order_id: "venue_busy"}},
    ]
    first = service.tick()
    if path == "first_seen":
        episode = store.lp_first_seen_episode(episode_id)
        assert episode is not None
        assert episode["state"] == "canceling"
        assert episode["canceled_order_ids"] == []
    else:
        protection = first["queue_protection"]
        assert protection["state"] == "canceling"
        assert protection["canceled_order_ids"] == []
    assert exchange.cancels == [order_id]
    assert notes == []

    filled = _queue_receipt(order_id, status="FILLED", matched="2000")
    if path == "first_seen":
        exchange.account_open_orders = [filled]
    else:
        token_id = str(started["token_id"])
        exchange.snapshot_value = _queue_runtime_snapshot(
            now,
            bid_size="4000",
            orders=[filled],
            open_orders=[],
            trades=[
                {
                    "trade_id": "filled-target-trade",
                    "status": "CONFIRMED",
                    "maker_orders": [
                        {
                            "order_id": order_id,
                            "side": "BUY",
                            "token_id": token_id,
                            "matched_amount": Decimal("2000"),
                            "price": Decimal("0.30"),
                        }
                    ],
                }
            ],
            positions=[{"token_id": token_id, "size": Decimal("2000")}],
        )
        exchange.snapshot_value["scoring"] = True

    converged = service.tick()
    if path == "first_seen":
        episode = store.lp_first_seen_episode(episode_id)
        assert episode is not None
        assert episode["state"] == "partially_filled"
        assert Decimal(str(episode["partially_filled_quantity"])) == Decimal("2000")
        assert episode["canceled_order_ids"] == []
    else:
        protection = converged["queue_protection"]
        assert protection["state"] == "partially_filled"
        assert Decimal(str(protection["partially_filled_quantity"])) == Decimal("2000")
        assert protection["canceled_order_ids"] == []
    assert notes == []

    service.tick()
    assert exchange.cancels == [order_id]
    assert notes == []


@pytest.mark.parametrize("path", ("first_seen", "submit"))
def test_protection_blocked_notice_identifies_order_and_reason(
    tmp_path, path: str
) -> None:
    """Blocked notices identify the protected quote without claiming completion."""

    now = datetime(2026, 9, 21, 11, 8, 10, tzinfo=UTC)
    condition = _FIRST_SEEN_CONDITION
    token = _FIRST_SEEN_TOKEN
    title = "Treasury yield below 4.20%?"
    url = "https://polymarket.com/event/treasury-test"
    flags = {"allowed": True}
    notes: list[tuple[str, str, str]] = []

    if path == "first_seen":
        store = PredictionArbitrageStore(tmp_path)
        exchange = _FirstSeenExchange()
        # Register from the healthy first observation (front=8000 after our
        # own 2000 shares), then move the book into the trigger state.  This
        # lets the recovery tick exercise the existing first-seen re-arm
        # behavior instead of changing the estimator's baseline.
        exchange.set_book(token, level_total="10000", now=now, condition_id=condition)
        row = _queue_receipt("blocked-first", original="2000")
        row.update(
            {
                "condition_id": condition,
                "token_id": token,
                "created_at": now - timedelta(minutes=1),
                "market_title": title,
                "market_url": url,
                "outcome": "Yes",
            }
        )
        exchange.account_open_orders = [row]
        service = PolymarketLPService(
            store,
            exchange,
            clock=lambda: now,
            mutation_guard=lambda *args, **kwargs: flags["allowed"],
        )
        service.register_first_seen_candidates([{
            "order_id": "blocked-first",
            "condition_id": condition,
            "token_id": token,
            "price": Decimal("0.30"),
            "remaining": Decimal("2000"),
            "created_at": row["created_at"],
            "first_seen_at": now,
            "market_title": title,
            "market_url": url,
            "outcome": "Yes",
        }], now=now)
        exchange.set_book(token, level_total="4000", now=now, condition_id=condition)
    else:
        store = PredictionArbitrageStore(tmp_path)
        exchange = _Exchange()
        exchange.snapshot_value = _queue_runtime_snapshot(now, bid_size="10000")
        service = PolymarketLPService(
            store,
            exchange,
            clock=lambda: now,
            mutation_guard=lambda *args, **kwargs: flags["allowed"],
        )
        request = {
            **_request(now),
            "market_title": title,
            "market_url": url,
            "review_at": now + timedelta(hours=1),
            "quantity": Decimal("2000"),
        }
        preview = service.preview(request)
        started = service.start(str(preview["preview_id"]), "lp-blocked-notice")
        assert started["state"] == "entry_open"
        entry_id = str(started["entry_order_id"])
        row = _queue_receipt(entry_id, original="2000")
        row.update(
            {
                "condition_id": condition,
                "token_id": token,
                "created_at": now - timedelta(minutes=1),
                "market_title": title,
                "market_url": url,
                "outcome": "YES",
            }
        )
        exchange.snapshot_value = _queue_runtime_snapshot(
            now, bid_size="4000", orders=[row], open_orders=[row]
        )
        exchange.snapshot_value["scoring"] = True

    service.set_protection_notifier(
        lambda notice_title, message, voice: notes.append(
            (notice_title, message, voice)
        )
    )
    flags["allowed"] = False
    first = service.tick()
    second = service.tick()

    assert exchange.cancels == []
    assert first is not None
    assert second is not None
    assert len(notes) == 1
    if path == "first_seen":
        flags["allowed"] = True
        exchange.set_book(
            token, level_total="10000", now=now, condition_id=condition
        )
        healthy = service.tick()
        assert healthy is not None
        assert exchange.cancels == []
        assert len(notes) == 1
        flags["allowed"] = False
        exchange.set_book(
            token, level_total="4000", now=now, condition_id=condition
        )
        recovered_block = service.tick()
        assert recovered_block is not None
        assert len(notes) == 2
    assert exchange.cancels == []
    notice_title, message, _ = notes[0]
    assert "受阻" in notice_title
    assert title in message and url in message
    assert "BUY YES" in message and "0.3" in message
    assert "按份额" in message and "保护范围" in message
    assert "撤单未成功" in message
    assert "残余 2000 份待处理" in message
    assert "UNKNOWN" not in message
    assert "已撤" not in message
    assert "挂单存续" not in message
    expected_source = (
        "首次观察订单时建立的盘口基线"
        if path == "first_seen"
        else "提交时基线"
    )
    assert expected_source in message


def test_first_seen_guard_and_identity_conflict_block_cancel(
    tmp_path,
) -> None:
    """E7: 熔断开 → blocked、零写调用、一次性受阻通知；身份冲突 → blocked。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    notes: list[tuple[str, str, str]] = []

    def collect(title: str, message: str, xiaoai_text: str) -> None:
        notes.append((title, message, xiaoai_text))

    anchor = _queue_receipt("m-1")
    store, exchange, service = _first_seen_running_service(
        tmp_path, now, episode_id="ep-e7", level_total="4000",
        open_orders=[anchor], notifier=collect,
        guard=lambda action: False,
    )
    service.tick()
    episode = store.lp_first_seen_episode("ep-e7")
    assert episode is not None
    assert episode["state"] == "blocked"
    assert "mutation_blocked" in episode["reason_codes"]
    assert exchange.cancels == []
    assert exchange.posts == []
    assert exchange.protected_sells == []
    assert len(notes) == 1
    assert "首见基线" in notes[0][0]

    # blocked → 恢复（估算已知）→ 再次受阻：重通知一次。
    service.set_mutation_guard(lambda action: True)
    service.tick()
    episode = store.lp_first_seen_episode("ep-e7")
    assert episode is not None
    assert episode["state"] == "canceling"
    assert exchange.cancels == ["m-1"]

    store2, exchange2, service2 = _first_seen_running_service(
        tmp_path / "identity", now, episode_id="ep-e7b", level_total="4000",
        open_orders=[
            _queue_receipt("m-1", side="SELL"),
            _queue_receipt("m-manual", original="2000"),
        ],
        anchors=("m-1",),
    )
    service2.tick()
    episode2 = store2.lp_first_seen_episode("ep-e7b")
    assert episode2 is not None
    assert episode2["state"] == "blocked"
    assert "identity_conflict" in episode2["reason_codes"]
    assert exchange2.cancels == []


def test_first_seen_anchors_terminal_ends_episode_and_frees_token(
    tmp_path,
) -> None:
    """T: 锚单全部终态 → 该段终态、token 释放；同 token 新 diff 可开新段。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    anchor = _queue_receipt("m-1")
    store, exchange, service = _first_seen_running_service(
        tmp_path, now, episode_id="ep-t", level_total="10000",
        open_orders=[anchor],
    )
    # 锚单从所有读取路径消失（网页手动撤掉）。
    exchange.account_open_orders = []
    service.tick()
    episode = store.lp_first_seen_episode("ep-t")
    assert episode is not None
    assert episode["state"] == "terminal"
    assert store.lp_active_first_seen_episodes() == []
    assert exchange.cancels == []

    # episode 终态后该 token 新出现的 BUY 可开新段（不接锚、不链式）。
    result = service.register_first_seen_candidates(
        [
            {
                "order_id": "m-new",
                "token_id": _FIRST_SEEN_TOKEN,
                "condition_id": _FIRST_SEEN_CONDITION,
                "price": Decimal("0.30"),
                "remaining": Decimal("2000"),
                "created_at": None,
                "first_seen_at": now,
            }
        ],
        now=now,
    )
    assert result["state"] == "registered"
    episodes = store.lp_active_first_seen_episodes()
    assert len(episodes) == 1
    assert episodes[0]["anchor_order_ids"] == ["m-new"]
    assert episodes[0]["episode_id"] != "ep-t"


def test_first_seen_anchor_is_earliest_first_seen_buy(tmp_path) -> None:
    """R2: 锚=最早首见 BUY。候选 B 的 venue created_at 更早但 first_seen_at
    更晚，不得夺锚；锚必须是 A（anchor_price=0.30），并列才看 created_at。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    exchange = _FirstSeenExchange()
    exchange.set_book(_FIRST_SEEN_TOKEN, level_total="10000", now=now)
    exchange.books[_FIRST_SEEN_TOKEN]["bids"].append(
        {"price": Decimal("0.40"), "size": Decimal("1000")}
    )
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    result = service.register_first_seen_candidates(
        [
            {
                "order_id": "m-b",
                "token_id": _FIRST_SEEN_TOKEN,
                "condition_id": _FIRST_SEEN_CONDITION,
                "price": Decimal("0.40"),
                "remaining": Decimal("500"),
                "created_at": datetime(2026, 9, 21, 11, 59, 0, tzinfo=UTC),
                "first_seen_at": datetime(2026, 9, 21, 12, 0, 10, tzinfo=UTC),
            },
            {
                "order_id": "m-a",
                "token_id": _FIRST_SEEN_TOKEN,
                "condition_id": _FIRST_SEEN_CONDITION,
                "price": Decimal("0.30"),
                "remaining": Decimal("2000"),
                "created_at": datetime(2026, 9, 21, 11, 59, 30, tzinfo=UTC),
                "first_seen_at": datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC),
            },
        ],
        now=now,
    )
    assert result["state"] == "registered"
    episodes = store.lp_active_first_seen_episodes()
    assert len(episodes) == 1
    episode = episodes[0]
    assert Decimal(str(episode["anchor_price"])) == Decimal("0.30")
    assert episode["anchor_order_ids"] == ["m-a"]
    assert Decimal(str(episode["baseline_front"])) == Decimal("8000")


def test_first_seen_anchor_session_refusal_blocks_cancel_and_tick_survives(
    tmp_path,
) -> None:
    """R1: n-leg 批次活动且锚 session 行不存在 → 连续 tick 不抛异常，episode
    转 blocked 且一次性受阻通知（含原因）、fake exchange 零撤单，同 tick
    #152 会话保护管线照常评估。"""
    import sqlite3

    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    notes: list[tuple[str, str, str]] = []

    def collect(title: str, message: str, xiaoai_text: str) -> None:
        notes.append((title, message, xiaoai_text))

    anchor = _queue_receipt("m-1")
    store, exchange, service = _first_seen_running_service(
        tmp_path, now, episode_id="ep-r1", level_total="4000",
        open_orders=[anchor], notifier=collect,
    )
    # 同一服务上并开一段 #152 会话，验证首见评估受阻之后同 tick 的会话
    # 管线照常执行（运行时快照与首见账户读是两条独立夹具通道）。
    exchange.snapshot_value = _queue_runtime_snapshot(
        now, bid_size="10000", orders=[_queue_receipt("order-1")]
    )
    request = {**_request(now), "quantity": Decimal("2000")}
    preview = service.preview(request)
    started = service.start(str(preview["preview_id"]), "lp-first-seen-r1")
    assert started["state"] == "entry_open"

    # n-leg 批次活动；manual 锚 session 行从未创建（登记走 episode 表）。
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO n_leg_controls(singleton, mode, breaker_open,"
            " active_batch_id, total_unsettled_capital_units, updated_at)"
            " VALUES (1, 'MANUAL', 0, 'batch-r1', 0, '2026-09-21T12:00:00Z')"
            " ON CONFLICT(singleton) DO UPDATE SET active_batch_id='batch-r1'"
        )

    first = service.tick()
    episode = store.lp_first_seen_episode("ep-r1")
    assert episode is not None
    assert episode["state"] == "blocked"
    assert "anchor_session_failed" in episode["reason_codes"]
    assert episode["blocked_notified"] is True
    assert exchange.cancels == []
    assert len(notes) == 1
    assert "首见基线" in notes[0][0]
    assert "锚点审计会话创建失败" in notes[0][1]
    assert "active_n_leg_batch" in notes[0][1]
    # 同 tick：排在首见保护之后的 #152 会话保护管线照常执行。
    assert first["queue_protection"]["state"] == "monitoring"

    # 连续 tick：不抛异常、不撤单、blocked 期间不重复通知。
    exchange.books[_FIRST_SEEN_TOKEN]["bids"][0]["size"] = Decimal("10000")
    for _ in range(2):
        again = service.tick()
        assert again["queue_protection"]["state"] == "monitoring"
        assert store.lp_first_seen_episode("ep-r1") is not None
        assert exchange.cancels == []
        assert len(notes) == 1


# ---- Issue 163: LP 单次确认提交（submit_entry / submit_augment） ----


def _lp163_book(now: datetime, best_bid: Decimal) -> dict[str, object]:
    """Book whose best bid is exactly ``best_bid`` (ask fixed at 0.31)."""

    base = _snapshot(now)
    base["book"] = {
        "timestamp": now,
        "received_at": now,
        "source_timestamp": "2026-09-21T06:00:00Z",
        "hash": "book-hash-lp163",
        "asks": [{"price": Decimal("0.31"), "size": Decimal("100")}],
        "bids": [{"price": best_bid, "size": Decimal("100")}],
    }
    return base


def test_lp163_submit_entry_single_shot_accepted(tmp_path) -> None:
    """S1: 一次提交=一次新鲜事实+一次完整校验+恰好一张 post-only GTD BUY。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _lp163_book(now, Decimal("0.29"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {
        **_request(now),
        "price": Decimal("0.29"),
        "quantity": Decimal("20"),
        "candidate_policy": "best_bid_minimum",
        "estimated_target_quantity": "21",
    }

    result = service.submit_entry(request, "lp163-s1")

    assert result["state"] == "entry_open"
    assert len(exchange.posts) == 1
    posted = exchange.posts[0]
    assert posted["side"] == "BUY"
    assert posted["post_only"] is True
    assert posted.get("expiration") or exchange.limit_orders[0].get("expiration")
    session = store.lp_session(str(result["session_id"]))
    assert session is not None
    assert len(session["owned_order_ids"]) == 1
    assert session["queue_protection"]["state"] == "registered"
    assert Decimal(str(session["queue_protection"]["baseline_front"])) == Decimal("100")
    assert Decimal(str(session["estimated_target_quantity"])) == Decimal("21")
    assert exchange.snapshot_calls == 1


def test_lp163_submit_entry_trial_price_mismatch_rejects(tmp_path) -> None:
    """S2: 试挂确认价 0.29 ≠ 提交时新鲜买一 0.30 → best_bid_changed；不挂单、无会话行。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _lp163_book(now, Decimal("0.30"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {
        **_request(now),
        "price": Decimal("0.29"),
        "quantity": Decimal("20"),
        "candidate_policy": "best_bid_minimum",
    }

    result = service.submit_entry(request, "lp163-s2")

    assert result == {"state": "rejected", "reason": "best_bid_changed"}
    assert exchange.posts == []
    assert store.lp_session_by_idempotency("lp163-s2") is None
    assert store.lp_active_session() is None
    assert exchange.snapshot_calls == 1


def test_lp163_submit_entry_custom_price_allows_offset(tmp_path) -> None:
    """S3: 5%/自定义模式不锚定买一——0.25 vs 买一 0.29 其余绿 → entry_open。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _lp163_book(now, Decimal("0.29"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {
        **_request(now),
        "price": Decimal("0.25"),
        "quantity": Decimal("20"),
    }

    result = service.submit_entry(request, "lp163-s3")

    assert result["state"] == "entry_open"
    assert len(exchange.posts) == 1


def test_lp163_submit_entry_same_key_replay(tmp_path) -> None:
    """S4: 同键二次提交 → 同会话、仍一单、两次结果一致。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _lp163_book(now, Decimal("0.29"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {
        **_request(now),
        "price": Decimal("0.29"),
        "quantity": Decimal("20"),
        "candidate_policy": "best_bid_minimum",
    }

    first = service.submit_entry(request, "lp163-s4")
    second = service.submit_entry(request, "lp163-s4")

    assert first["state"] == second["state"] == "entry_open"
    assert str(second["session_id"]) == str(first["session_id"])
    assert second["entry_order_id"] == first["entry_order_id"]
    assert len(exchange.posts) == 1


def test_lp163_submit_entry_exchange_rejection_persists_and_replays(tmp_path) -> None:
    """S5: 交易所拒单 → entry_rejected 落库带键；同键重放同拒绝、不二次挂单（#158 契约）。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.reject_entry = True
    exchange.snapshot_value = _lp163_book(now, Decimal("0.29"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {
        **_request(now),
        "price": Decimal("0.29"),
        "quantity": Decimal("20"),
        "candidate_policy": "best_bid_minimum",
    }

    first = service.submit_entry(request, "lp163-s5")
    assert first["state"] == "entry_rejected"
    stored = store.lp_session_by_idempotency("lp163-s5")
    assert stored is not None
    assert stored["state"] == "entry_rejected"
    assert len(exchange.posts) == 1

    second = service.submit_entry(request, "lp163-s5")

    assert second["state"] == "entry_rejected"
    assert str(second["session_id"]) == str(first["session_id"])
    assert len(exchange.posts) == 1


def test_lp163_submit_entry_unknown_needs_attention_no_resubmit(tmp_path) -> None:
    """S6: 提交回执异常 → needs_attention + submit_status unknown；同键重放不重发。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.post_failures = [RuntimeError("post timeout")]
    exchange.snapshot_value = _lp163_book(now, Decimal("0.29"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {
        **_request(now),
        "price": Decimal("0.29"),
        "quantity": Decimal("20"),
        "candidate_policy": "best_bid_minimum",
    }

    first = service.submit_entry(request, "lp163-s6")
    assert first["state"] == "needs_attention"
    assert first["submit_status"] == "unknown"
    assert len(exchange.posts) == 1

    second = service.submit_entry(request, "lp163-s6")

    assert second["state"] == "needs_attention"
    assert second["submit_status"] == "unknown"
    assert len(exchange.posts) == 1


def test_lp163_submit_entry_validation_rejection_no_session_row(tmp_path) -> None:
    """S7: 余额不足快照 → rejected/balance_insufficient；无会话行、无挂单。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    snapshot = _lp163_book(now, Decimal("0.29"))
    snapshot["account"]["balance"] = Decimal("1")
    snapshot["account"]["allowance"] = Decimal("1")
    exchange.snapshot_value = snapshot
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {
        **_request(now),
        "price": Decimal("0.29"),
        "quantity": Decimal("20"),
        "candidate_policy": "best_bid_minimum",
    }

    result = service.submit_entry(request, "lp163-s7")

    assert result == {"state": "rejected", "reason": "balance_insufficient"}
    assert store.lp_session_by_idempotency("lp163-s7") is None
    assert exchange.posts == []
    assert exchange.snapshot_calls == 1


def test_lp163_submit_entry_busy_when_active_session(tmp_path) -> None:
    """S8: 同标的已有活动组 → busy/lp_session_market_active；零快照读、零挂单。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _augment_running_service(
        tmp_path, now, key="lp163-s8-entry"
    )
    request = {
        **_request(now),
        "price": Decimal("0.29"),
        "quantity": Decimal("20"),
        "candidate_policy": "best_bid_minimum",
    }
    before_calls = exchange.snapshot_calls

    result = service.submit_entry(request, "lp163-s8")

    assert result["state"] == "busy"
    assert result["reason"] == "lp_session_market_active"
    assert str(result["session_id"]) == str(started["session_id"])
    assert exchange.snapshot_calls == before_calls
    assert len(exchange.posts) == 1  # 仅既有入场单


def _lp166_market_identity(index: int) -> dict[str, object]:
    """Issue 166: a distinct market identity (condition/token/market)."""

    return {
        "market_id": f"market-{index}",
        "condition_id": "0x" + format(index, "x") * 64,
        "token_id": "0x" + format(index + 16, "x") * 64,
        "outcome": "YES",
    }


def _lp166_book(now: datetime, identity: dict[str, object]) -> dict[str, object]:
    """Fresh book whose market block carries ``identity``."""

    book = _lp163_book(now, Decimal("0.29"))
    market = dict(book["market"])  # type: ignore[arg-type]
    market.update(identity)
    book["market"] = market
    return book


def test_lp166_multi_market_entry_coexistence(tmp_path) -> None:
    """A: 组 A（标的甲）在仓后，标的乙 submit_entry 成功且两行均非终态；
    对标的甲同 (condition_id, outcome) 再次提交 → busy/lp_session_market_active
    且载荷含组 A 的 session_id；同幂等键重放返回原行不报冲突。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    market_a = _lp166_market_identity(1)
    market_b = _lp166_market_identity(2)
    exchange.snapshot_value = _lp166_book(now, market_a)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)

    # 组 A 经老两段式入口建仓。
    preview_a = service.preview(
        {**market_a, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)}
    )
    assert preview_a["state"] == "previewed"
    started_a = service.start(str(preview_a["preview_id"]), "lp166-a")
    assert started_a["state"] == "entry_open"
    session_a = str(started_a["session_id"])

    # 标的乙提交成功，两行均非终态。
    exchange.snapshot_value = _lp166_book(now, market_b)
    entry_b = service.submit_entry(
        {**market_b, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-b",
    )
    assert entry_b["state"] == "entry_open"
    session_b = str(entry_b["session_id"])
    assert {row["session_id"] for row in store.lp_active_sessions()} == {
        session_a,
        session_b,
    }

    # 对标的甲同 (condition_id, outcome) 再次提交 → busy，指向组 A，零快照读。
    exchange.snapshot_value = _lp166_book(now, market_a)
    before_calls = exchange.snapshot_calls
    conflict = service.submit_entry(
        {**market_a, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-a-conflict",
    )
    assert conflict == {
        "state": "busy",
        "reason": "lp_session_market_active",
        "session_id": session_a,
    }
    assert exchange.snapshot_calls == before_calls
    assert len(exchange.posts) == 2  # 仅组 A、组 B 的入场单

    # 同幂等键重放 → 返回原行（组 B），不报冲突、不加单。
    replay = service.submit_entry(
        {**market_b, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-b",
    )
    assert replay["state"] == "entry_open"
    assert str(replay["session_id"]) == session_b
    assert len(exchange.posts) == 2


def test_lp166_start_same_market_store_fallback_rejected(tmp_path) -> None:
    """A(老两段式): 同标的+方向经 start() 绕过服务门禁时由数据库兜底映射
    lp_session_market_active；不同标的照常开组。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    market_a = _lp166_market_identity(1)
    market_c = _lp166_market_identity(3)
    exchange.snapshot_value = _lp166_book(now, market_a)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)

    preview_a = service.preview(
        {**market_a, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)}
    )
    started_a = service.start(str(preview_a["preview_id"]), "lp166-fb-a")
    assert started_a["state"] == "entry_open"
    session_a = str(started_a["session_id"])
    posts_before = len(exchange.posts)

    # 同标的+方向、新幂等键 → store 兜底映射 lp_session_market_active，无新单。
    preview_conflict = service.preview(
        {**market_a, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)}
    )
    rejected = service.start(str(preview_conflict["preview_id"]), "lp166-fb-conflict")
    assert rejected == {"state": "rejected", "reason": "lp_session_market_active"}
    assert len(exchange.posts) == posts_before
    assert store.lp_session_by_idempotency("lp166-fb-conflict") is None

    # 不同标的照常开组。
    exchange.snapshot_value = _lp166_book(now, market_c)
    preview_c = service.preview(
        {**market_c, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)}
    )
    started_c = service.start(str(preview_c["preview_id"]), "lp166-fb-c")
    assert started_c["state"] == "entry_open"
    assert {row["session_id"] for row in store.lp_active_sessions()} == {
        session_a,
        str(started_c["session_id"]),
    }


def _lp166_held_position_book(
    now: datetime,
    identity: dict[str, object],
    position: dict[str, object],
) -> dict[str, object]:
    """Fresh book for ``identity`` whose account holds one position (no orders)."""

    book = _lp166_book(now, identity)
    account = dict(book["account"])  # type: ignore[arg-type]
    account["open_orders"] = []
    account["positions"] = [position]
    book["account"] = account
    return book


def test_lp166_submit_entry_rejects_opposite_direction_when_participating(tmp_path) -> None:
    """B(R1): YES 组入场单全部成交（账户无挂单、持仓在 YES token、组仍活动）后，
    同 condition 的 NO 方向 submit_entry → rejected/market_already_participating；
    无新单、无新会话行、组 A 不受影响。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    market_yes = _lp166_market_identity(1)
    exchange.snapshot_value = _lp166_book(now, market_yes)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    started = service.submit_entry(
        {**market_yes, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-r1-a",
    )
    assert started["state"] == "entry_open"
    session_a = str(started["session_id"])
    assert len(exchange.posts) == 1

    # 入场单全部成交：账户事实无挂单、仅 YES token 持仓。
    market_no = {
        **market_yes,
        "token_id": "0x" + format(24, "x") * 64,
        "outcome": "NO",
    }
    exchange.snapshot_value = _lp166_held_position_book(
        now,
        market_no,
        {
            "condition_id": market_yes["condition_id"],
            "market_id": market_yes["market_id"],
            "token_id": market_yes["token_id"],
            "outcome": "YES",
            "size": Decimal("20"),
        },
    )
    posts_before = len(exchange.posts)

    result = service.submit_entry(
        {**market_no, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-r1-no",
    )

    assert result == {"state": "rejected", "reason": "market_already_participating"}
    assert len(exchange.posts) == posts_before
    assert store.lp_session_by_idempotency("lp166-r1-no") is None
    assert {row["session_id"] for row in store.lp_active_sessions()} == {session_a}


def test_lp166_submit_entry_participation_check_spares_other_markets(tmp_path) -> None:
    """B(R1): 参与检查只命中同 condition——YES 持仓在场时，不同标的提交照常 entry_open。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    market_a = _lp166_market_identity(1)
    exchange.snapshot_value = _lp166_book(now, market_a)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    started = service.submit_entry(
        {**market_a, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-r1-b-a",
    )
    assert started["state"] == "entry_open"

    market_b = _lp166_market_identity(2)
    exchange.snapshot_value = _lp166_held_position_book(
        now,
        market_b,
        {
            "condition_id": market_a["condition_id"],
            "market_id": market_a["market_id"],
            "token_id": market_a["token_id"],
            "outcome": "YES",
            "size": Decimal("20"),
        },
    )

    result = service.submit_entry(
        {**market_b, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-r1-b-b",
    )

    assert result["state"] == "entry_open"


def test_lp166_submit_augment_not_blocked_by_participation_check(tmp_path) -> None:
    """B(R1)（#167 改写）：参与检查严禁误伤追加——本组挂单在场（参与检查必命中）时
    submit_augment 到新价位照常追加，不加新组。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _augment_running_service(
        tmp_path, now, key="lp166-r1-c"
    )
    session_id = str(started["session_id"])
    exchange.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    posts_before = len(exchange.posts)

    result = service.submit_augment(
        session_id, "20", "lp166-r1-c-aug", price="0.29"
    )

    assert result["state"] == "entry_open"
    assert str(result["augment_order_id"]) == "order-2"
    assert len(exchange.posts) == posts_before + 1
    assert Decimal(str(exchange.posts[1]["price"])) == Decimal("0.29")
    assert store.lp_session(session_id)["augment_order_ids"] == ["order-2"]


def test_lp166_start_confirm_rejects_participating_market(tmp_path) -> None:
    """B(R1): 老 start confirm 路径同口径——preview 时市场干净、confirm 新鲜事实
    出现同 condition 持仓 → rejected/market_already_participating；无新单、无会话行、
    preview 未消费。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    market_a = _lp166_market_identity(1)
    market_b = _lp166_market_identity(2)
    exchange.snapshot_value = _lp166_book(now, market_b)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    # 组 B 先经 submit_entry 在标的乙建仓并成交（持仓在乙）。
    started_b = service.submit_entry(
        {**market_b, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-r1-d-b",
    )
    assert started_b["state"] == "entry_open"

    # 市场甲 preview 时干净。
    exchange.snapshot_value = _lp166_book(now, market_a)
    preview_a = service.preview(
        {**market_a, "price": Decimal("0.29"), "quantity": Decimal("20"),
         "review_at": now + timedelta(minutes=10)}
    )
    assert preview_a["state"] == "previewed"
    # confirm 时新鲜事实显示甲上已有同 condition 持仓（组 B 的乙不相关）。
    exchange.snapshot_value = _lp166_held_position_book(
        now,
        market_a,
        {
            "condition_id": market_a["condition_id"],
            "market_id": market_a["market_id"],
            "token_id": "0x" + format(31, "x") * 64,
            "outcome": "YES",
            "size": Decimal("20"),
        },
    )
    posts_before = len(exchange.posts)

    result = service.start(str(preview_a["preview_id"]), "lp166-r1-d-a")

    assert result == {"state": "rejected", "reason": "market_already_participating"}
    assert len(exchange.posts) == posts_before
    assert store.lp_session_by_idempotency("lp166-r1-d-a") is None
    assert store.lp_preview(str(preview_a["preview_id"])) is not None


def test_lp163_submit_entry_review_too_soon(tmp_path) -> None:
    """S10: review_at 过近 → rejected/review_at_too_soon（expiration_for_review 语义），无挂单。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _lp163_book(now, Decimal("0.29"))
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {
        **_request(now),
        "price": Decimal("0.29"),
        "quantity": Decimal("20"),
        "candidate_policy": "best_bid_minimum",
        "review_at": now + timedelta(seconds=30),
    }

    result = service.submit_entry(request, "lp163-s10")

    assert result == {"state": "rejected", "reason": "review_at_too_soon"}
    assert exchange.posts == []
    assert store.lp_session_by_idempotency("lp163-s10") is None


def test_lp163_submit_entry_thin_top_book_anchor_consistent(tmp_path) -> None:
    """S15: 薄顶档盘口锚定口径一致——顶档 0.60×5 < reward_min 100 ≤ 累计 205，
    奖励资格买一=0.58 ≠ 顶档买一 0.60。
    a) 确认价=顶档 0.60 → entry_open、恰 1 post、恰 1 次快照读；
    b) 同盘口确认价=奖励资格档 0.58 → rejected/best_bid_changed、0 post、无会话行。
    """
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    snapshot = _lp163_book(now, Decimal("0.60"))
    snapshot["book"]["asks"] = [{"price": Decimal("0.62"), "size": Decimal("100")}]
    snapshot["book"]["bids"] = [
        {"price": Decimal("0.60"), "size": Decimal("5")},
        {"price": Decimal("0.58"), "size": Decimal("200")},
    ]
    snapshot["market"]["reward_min_size"] = Decimal("100")

    # a) 确认价=盘口顶档买一（候选行 guidance 价即顶档）→ 必须放行。
    exchange = _Exchange()
    exchange.snapshot_value = snapshot
    store = PredictionArbitrageStore(tmp_path / "a")
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    request = {
        **_request(now),
        "price": Decimal("0.60"),
        "quantity": Decimal("100"),
        "candidate_policy": "best_bid_minimum",
    }

    accepted = service.submit_entry(request, "lp163-s15a")

    assert accepted["state"] == "entry_open"
    assert len(exchange.posts) == 1
    assert exchange.snapshot_calls == 1

    # b) 同盘口，确认价=奖励资格买一 0.58 ≠ 顶档 0.60 → 以 best_bid_changed 拒绝。
    exchange_b = _Exchange()
    exchange_b.snapshot_value = snapshot
    store_b = PredictionArbitrageStore(tmp_path / "b")
    service_b = PolymarketLPService(store_b, exchange_b, clock=lambda: now)
    request_b = {
        **_request(now),
        "price": Decimal("0.58"),
        "quantity": Decimal("100"),
        "candidate_policy": "best_bid_minimum",
    }

    rejected = service_b.submit_entry(request_b, "lp163-s15b")

    assert rejected == {"state": "rejected", "reason": "best_bid_changed"}
    assert exchange_b.posts == []
    assert store_b.lp_session_by_idempotency("lp163-s15b") is None
    assert store_b.lp_active_session() is None


def test_lp163_submit_augment_appends_and_keeps_deadline(tmp_path) -> None:
    """S11（#167 改写）：点名会话追加新价位成功——单追加、新价位桶基线=该价档位深度、
    复核截止沿用首单不重算。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _augment_running_service(
        tmp_path, now, key="lp163-s11"
    )
    session_id = str(started["session_id"])
    first_history = dict(store.lp_session(session_id)["order_history"])
    exchange.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    before_calls = exchange.snapshot_calls

    result = service.submit_augment(session_id, "20", "lp163-s11-aug", price="0.29")

    assert result["state"] == "entry_open"
    assert str(result["augment_order_id"]) == "order-2"
    assert exchange.snapshot_calls - before_calls == 1
    assert len(exchange.posts) == 2
    posted = exchange.posts[1]
    assert posted["side"] == "BUY"
    assert posted["post_only"] is True
    assert Decimal(str(posted["price"])) == Decimal("0.29")
    assert Decimal(str(posted["quantity"])) == Decimal("20")
    session = store.lp_session(session_id)
    # 原单不变，新单追加为新条目。
    assert session["augment_order_ids"] == ["order-2"]
    history = session["order_history"]
    assert set(history) == {"order-1", "order-2"}
    assert history["order-1"] == first_history["order-1"]
    # 新价位桶：基线 = 该价档位深度 100（0.29 档），原 0.30 桶基线不动。
    protection = session["queue_protection"]
    assert protection["version"] == 2
    assert Decimal(
        str(protection["levels"]["0.29"]["baseline_front"])
    ) == Decimal("100")
    assert Decimal(
        str(protection["levels"]["0.30"]["baseline_front"])
    ) == Decimal("120")
    # 复核截止沿用首单：新单 expiration 与首单相同（未重算）。
    assert Decimal(str(history["order-2"]["expiration"])) == Decimal(
        str(history["order-1"]["expiration"])
    )
    assert exchange.limit_orders[1]["expiration"] == exchange.limit_orders[0][
        "expiration"
    ]


def test_lp163_submit_augment_binds_named_session(tmp_path) -> None:
    """S12: 已完成会话 A 与活动会话 B 并存时点名 A → 拒且 reason 指向 A；绝不写 B、无新单。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    store, exchange, service, started_a = _augment_running_service(
        tmp_path, now, key="lp163-s12-a"
    )
    session_a = str(started_a["session_id"])
    store.lp_update_session(session_a, state="complete")
    request = {**_request(now), "quantity": Decimal("30")}
    preview = service.preview(request)
    started_b = service.start(str(preview["preview_id"]), "lp163-s12-b")
    assert started_b["state"] == "entry_open"
    session_b = str(started_b["session_id"])
    exchange.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    posts_before = len(exchange.posts)
    calls_before = exchange.snapshot_calls

    result = service.submit_augment(session_a, "20", "lp163-s12")

    assert result["state"] == "rejected"
    assert result["reason"] == "session_not_active"
    assert exchange.snapshot_calls == calls_before
    assert len(exchange.posts) == posts_before
    untouched = store.lp_session(session_b)
    assert not untouched.get("augment_order_ids")
    assert store.lp_active_session()["session_id"] == session_b


def test_lp163_submit_augment_blocked_states_matrix(tmp_path) -> None:
    """S13: 退出中/止损中/待人工核对/已完成/不存在 → 各自真实原因；零快照、零挂单。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    cases = [
        ("review", "session_review_exit"),
        ("stop_loss_exit", "session_stop_loss_exit"),
        ("needs_attention", "session_needs_attention"),
        ("complete", "session_not_active"),
    ]
    for index, (state, expected_reason) in enumerate(cases):
        store, exchange, service, started = _augment_running_service(
            tmp_path / f"s13-{index}", now, key=f"lp163-s13-{index}"
        )
        session_id = str(started["session_id"])
        store.lp_update_session(session_id, state=state)
        calls_before = exchange.snapshot_calls
        posts_before = len(exchange.posts)

        result = service.submit_augment(session_id, "20", f"lp163-s13-{index}")

        assert result == {"state": "rejected", "reason": expected_reason}
        assert exchange.snapshot_calls == calls_before
        assert len(exchange.posts) == posts_before

    store, exchange, service, _started = _augment_running_service(
        tmp_path / "s13-missing", now, key="lp163-s13-missing"
    )
    calls_before = exchange.snapshot_calls
    missing = service.submit_augment("missing-session", "20", "lp163-s13-x")
    assert missing == {"state": "rejected", "reason": "session_not_found"}
    assert exchange.snapshot_calls == calls_before


def test_lp163_submit_augment_idempotency_and_unknown(tmp_path) -> None:
    """S14（#167 改写）：同键重放 → 同 augment_order_id 无新单；提交异常 → 未知挂起，重放不重发。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _augment_running_service(
        tmp_path / "s14-a", now, key="lp163-s14"
    )
    session_id = str(started["session_id"])
    exchange.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    first = service.submit_augment(session_id, "20", "lp163-s14-k", price="0.29")
    assert first["state"] == "entry_open"
    assert str(first["augment_order_id"]) == "order-2"
    # 幂等重放连新鲜盘口都不再读。
    calls_after_first = exchange.snapshot_calls
    exchange.snapshot_value = _queue_book_moved_bid_snapshot(now, Decimal("9000"))

    replay = service.submit_augment(session_id, "20", "lp163-s14-k")

    assert str(replay["augment_order_id"]) == "order-2"
    assert replay["state"] == "entry_open"
    assert len(exchange.posts) == 2
    assert exchange.snapshot_calls == calls_after_first

    store_b, exchange_b, service_b, started_b = _augment_running_service(
        tmp_path / "s14-b", now, key="lp163-s14b"
    )
    session_b = str(started_b["session_id"])
    exchange_b.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    exchange_b.post_failures = [RuntimeError("post timeout")]
    unknown = service_b.submit_augment(session_b, "20", "lp163-s14-unknown", price="0.29")
    assert unknown["state"] == "needs_attention"
    assert unknown["reason"] == "augment_submit_unknown"
    # fake 的 post_order 先记账再抛错：1 张入场单 + 1 次失败的加量尝试。
    posts_after_unknown = len(exchange_b.posts)
    assert posts_after_unknown == 2

    replay_unknown = service_b.submit_augment(session_b, "20", "lp163-s14-unknown")

    assert replay_unknown["state"] == "needs_attention"
    assert replay_unknown["reason"] == "augment_submit_unknown"
    assert len(exchange_b.posts) == posts_after_unknown  # 重放不重发


def test_lp166_entry_funds_check_nets_all_group_reservations(tmp_path) -> None:
    """B: 组 A 挂 $50 买单（余额 $120、授权 $200）后，标的乙 $100 提交被拒
    balance_insufficient（120−50=70＜100）；同场景 $60 通过（70≥60）；
    无任何活动组时同样的 $100 可以通过（扣占用口径的反事实对照）。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    market_a = _lp166_market_identity(1)
    market_b = _lp166_market_identity(2)

    def funded_book(identity: dict[str, object]) -> dict[str, object]:
        book = _lp166_book(now, identity)
        account = dict(book["account"])  # type: ignore[arg-type]
        account["balance"] = Decimal("120")
        account["allowance"] = Decimal("200")
        book["account"] = account
        # 0.50/0.52 盘口：0.50 挂单距中点 0.01 ≤ reward_max_spread 0.10。
        book["book"] = {
            **book["book"],  # type: ignore[arg-type]
            "asks": [{"price": Decimal("0.52"), "size": Decimal("1000")}],
            "bids": [{"price": Decimal("0.50"), "size": Decimal("1000")}],
        }
        return book

    # 反事实对照：零活动组时 $100 提交通过。
    exchange = _Exchange()
    exchange.snapshot_value = funded_book(market_b)
    store = PredictionArbitrageStore(tmp_path / "counterfactual")
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    alone = service.submit_entry(
        {**market_b, "price": Decimal("0.50"), "quantity": Decimal("200"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-b-alone",
    )
    assert alone["state"] == "entry_open"

    # 组 A：$50 买单（0.50 × 100）在仓，占用 $50。
    exchange = _Exchange()
    exchange.snapshot_value = funded_book(market_a)
    store = PredictionArbitrageStore(tmp_path / "main")
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    group_a = service.submit_entry(
        {**market_a, "price": Decimal("0.50"), "quantity": Decimal("100"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-b-a",
    )
    assert group_a["state"] == "entry_open"

    # 标的乙 $100（0.50 × 200）：120−50=70＜100 → balance_insufficient。
    exchange.snapshot_value = funded_book(market_b)
    posts_before = len(exchange.posts)
    rejected = service.submit_entry(
        {**market_b, "price": Decimal("0.50"), "quantity": Decimal("200"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-b-reject",
    )
    assert rejected == {"state": "rejected", "reason": "balance_insufficient"}
    assert len(exchange.posts) == posts_before
    assert store.lp_session_by_idempotency("lp166-b-reject") is None

    # 同场景 $60（0.50 × 120）：70≥60 → 放行，两行均非终态。
    accepted = service.submit_entry(
        {**market_b, "price": Decimal("0.50"), "quantity": Decimal("120"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-b-accept",
    )
    assert accepted["state"] == "entry_open"
    assert {row["session_id"] for row in store.lp_active_sessions()} == {
        str(group_a["session_id"]),
        str(accepted["session_id"]),
    }


def test_lp166_entry_funds_unknown_reservations_rejected(tmp_path) -> None:
    """B(未知): 活动组占用金额不可得（缺 price 的存量行）→
    account_facts_unknown 拒绝，不用未扣占用的余额放行。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    market_a = _lp166_market_identity(1)
    market_b = _lp166_market_identity(2)
    exchange = _Exchange()
    book = _lp166_book(now, market_b)
    account = dict(book["account"])  # type: ignore[arg-type]
    account["balance"] = Decimal("120")
    account["allowance"] = Decimal("200")
    book["account"] = account
    book["book"] = {
        **book["book"],  # type: ignore[arg-type]
        "asks": [{"price": Decimal("0.52"), "size": Decimal("1000")}],
        "bids": [{"price": Decimal("0.50"), "size": Decimal("1000")}],
    }
    exchange.snapshot_value = book
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    # 直接落一行缺 price 的活动组 → 占用金额不可计算。
    store.lp_create_session(
        "lp166-unknown-occupier",
        "lp166-unknown-key",
        state="entry_open",
        payload={"condition_id": market_a["condition_id"], "outcome": "YES"},
    )

    result = service.submit_entry(
        {**market_b, "price": Decimal("0.50"), "quantity": Decimal("200"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-b-unknown",
    )
    assert result == {"state": "rejected", "reason": "account_facts_unknown"}
    assert exchange.posts == []


def test_lp166_submit_augment_blocks_passive_exit_and_entry_pending(tmp_path) -> None:
    """D: 加量只许 entry_open——passive_exit 拒 session_passive_exit、
    entry_submit_pending 拒 session_entry_pending；零快照、零挂单。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    cases = [
        ("passive_exit", "session_passive_exit"),
        ("entry_submit_pending", "session_entry_pending"),
    ]
    for index, (state, expected_reason) in enumerate(cases):
        store, exchange, service, started = _augment_running_service(
            tmp_path / f"s166-{index}", now, key=f"lp166-s166-{index}"
        )
        session_id = str(started["session_id"])
        store.lp_update_session(session_id, state=state)
        calls_before = exchange.snapshot_calls
        posts_before = len(exchange.posts)

        result = service.submit_augment(session_id, "20", f"lp166-s166-{index}")

        assert result == {"state": "rejected", "reason": expected_reason}
        assert exchange.snapshot_calls == calls_before
        assert len(exchange.posts) == posts_before

    # entry_open 放行（同一服务下作正对照；新价位 0.29）。
    store, exchange, service, started = _augment_running_service(
        tmp_path / "s166-open", now, key="lp166-s166-open"
    )
    session_id = str(started["session_id"])
    exchange.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    ok = service.submit_augment(
        session_id, "20", "lp166-s166-open-aug", price="0.29"
    )
    assert ok["state"] == "entry_open"
    assert len(exchange.posts) == 2


def test_lp166_two_phase_augment_blocks_same_states(tmp_path) -> None:
    """D: 老两段式加量与 submit_augment 共用同一张拦截表——
    augment_preview 对 passive_exit / entry_submit_pending / review 三态各拒
    真实理由；confirm 侧（先出凭证后翻状态）同样被 _augment_session 拦下。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    cases = [
        ("passive_exit", "session_passive_exit"),
        ("entry_submit_pending", "session_entry_pending"),
        ("review", "session_review_exit"),
    ]
    for index, (state, expected_reason) in enumerate(cases):
        store, exchange, service, started = _augment_running_service(
            tmp_path / f"two-{index}", now, key=f"lp166-two-{index}"
        )
        session_id = str(started["session_id"])
        store.lp_update_session(session_id, state=state)
        posts_before = len(exchange.posts)

        preview = _augment_preview(service, session_id, 90)

        assert preview == {"state": "rejected", "reason": expected_reason}
        assert len(exchange.posts) == posts_before

    # confirm 侧：entry_open 时先取凭证，翻到 passive_exit 后 confirm 被拦。
    store, exchange, service, started = _augment_running_service(
        tmp_path / "two-confirm", now, key="lp166-two-confirm"
    )
    session_id = str(started["session_id"])
    exchange.snapshot_value = _augment_preview_snapshot(
        now, level=Decimal("380"), own_original=Decimal("120")
    )
    preview = _augment_preview(service, session_id, 90, price="0.29")
    assert preview["state"] == "previewed"
    store.lp_update_session(session_id, state="passive_exit")
    posts_before = len(exchange.posts)

    result = service.augment(session_id, str(preview["preview_id"]), "lp166-two-c")

    assert result == {"state": "rejected", "reason": "session_passive_exit"}
    assert len(exchange.posts) == posts_before


class _PerTokenExchange(_Exchange):
    """Issue 166: each group reconciles against its own token's snapshot."""

    def __init__(self) -> None:
        super().__init__()
        self.by_token: dict[str, dict[str, object]] = {}

    def lp_snapshot(self, request: dict[str, object]) -> dict[str, object]:
        self.snapshot_calls += 1
        token = str(request.get("token_id") or "")
        snapshot = self.by_token.get(token)
        if snapshot is None:
            raise RuntimeError("snapshot unavailable")
        return snapshot


def _lp166_two_groups(now: datetime, tmp_path, exchange) -> tuple:
    """Start group A (market-1/token-1) and group B (market-2/token-2)."""

    market_a = _lp166_market_identity(1)
    market_b = _lp166_market_identity(2)
    exchange.by_token.setdefault(
        str(market_a["token_id"]), _lp166_book(now, market_a)
    )
    exchange.by_token.setdefault(
        str(market_b["token_id"]), _lp166_book(now, market_b)
    )
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    entry_a = service.submit_entry(
        {**market_a, "price": Decimal("0.30"), "quantity": Decimal("10"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-tick-a",
    )
    assert entry_a["state"] == "entry_open"
    entry_b = service.submit_entry(
        {**market_b, "price": Decimal("0.22"), "quantity": Decimal("10"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-tick-b",
    )
    assert entry_b["state"] == "entry_open", entry_b
    return store, service, str(entry_a["session_id"]), str(entry_b["session_id"])


def test_lp166_tick_zero_groups_payload_unchanged(tmp_path) -> None:
    """E(零组): 无活动组时 tick 的 none 载荷与现契约逐键一致。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, _Exchange(), clock=lambda: now)
    assert service.tick() == {"state": "none", "session_id": None}


def test_lp166_tick_single_group_keeps_payload_shape(tmp_path) -> None:
    """E(单组): 顶层保持今天单组载荷形状并附加 sessions 键。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _PerTokenExchange()
    exchange.by_token[_lp166_market_identity(1)["token_id"]] = _lp166_book(
        now, _lp166_market_identity(1)
    )
    store, service, session_a, _session_b = _lp166_two_groups(
        now, tmp_path / "single", exchange
    )
    # 单组场景：把 B 终态化，只剩 A 活动。
    store.lp_update_session(_session_b, state="complete")

    result = service.tick()

    assert result["state"] == "entry_open"
    assert str(result["session_id"]) == session_a
    sessions = result["sessions"]
    assert isinstance(sessions, list) and len(sessions) == 1
    without_key = {k: v for k, v in result.items() if k != "sessions"}
    assert sessions[0] == without_key


def test_lp166_tick_two_groups_aggregate_oldest_stamps(tmp_path) -> None:
    """E(两组): sessions 含两组完整载荷；顶层时间戳取两组最旧；
    state 取最差态（两组均正常 → ok）；session_id 为 None。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _PerTokenExchange()
    token_a = str(_lp166_market_identity(1)["token_id"])
    token_b = str(_lp166_market_identity(2)["token_id"])
    # 两组各自带不同的核对时间戳：account_checked_at 可由账户快照自带；
    # book_checked_at 取盘口 received_at（须在 10 秒新鲜度内：A 比 B 旧 5 秒）。
    book_a = _lp166_book(now, _lp166_market_identity(1))
    book_b = _lp166_book(now, _lp166_market_identity(2))
    book_b["account_checked_at"] = now
    exchange.by_token[token_a] = book_a
    exchange.by_token[token_b] = book_b
    store, service, session_a, session_b = _lp166_two_groups(
        now, tmp_path, exchange
    )
    book_a["account_checked_at"] = now - timedelta(seconds=60)
    book_a["book"] = {
        **book_a["book"],  # type: ignore[arg-type]
        "received_at": now - timedelta(seconds=5),
    }

    result = service.tick()

    sessions = result["sessions"]
    assert {str(row["session_id"]) for row in sessions} == {session_a, session_b}
    assert all(row["state"] == "entry_open" for row in sessions)
    assert result["state"] == "ok"
    assert result["session_id"] is None
    stamps = {str(row["session_id"]): row["account_checked_at"] for row in sessions}
    assert stamps[session_a] != stamps[session_b]
    assert result["account_checked_at"] == min(stamps.values())
    book_stamps = {
        str(row["session_id"]): row["book_checked_at"] for row in sessions
    }
    assert result["book_checked_at"] == min(book_stamps.values())


def test_lp166_tick_two_groups_aggregate_prefers_needs_attention(tmp_path) -> None:
    """E(聚合优先级): 一组 needs_attention（组 A 快照断联 → 对账置
    needs_attention，一次故障不触发保守撤）+ 一组正常 → 顶层聚合
    state == "needs_attention"；sessions 仍含两组完整载荷，顶层 session_id
    为 None。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _PerTokenExchange()
    token_a = str(_lp166_market_identity(1)["token_id"])
    token_b = str(_lp166_market_identity(2)["token_id"])
    exchange.by_token[token_a] = _lp166_book(now, _lp166_market_identity(1))
    exchange.by_token[token_b] = _lp166_book(now, _lp166_market_identity(2))
    store, service, session_a, session_b = _lp166_two_groups(
        now, tmp_path, exchange
    )
    # 两组开仓完成后，组 A 的快照读断联 → 对账判 external_snapshot_unknown
    # → 组 A needs_attention（单次故障未达保守撤阈值）。
    del exchange.by_token[token_a]

    result = service.tick()

    assert result["state"] == "needs_attention"
    assert result["session_id"] is None
    sessions = result["sessions"]
    assert isinstance(sessions, list) and len(sessions) == 2
    states = {str(row["session_id"]): str(row["state"]) for row in sessions}
    assert states[session_a] == "needs_attention"
    assert states[session_b] == "entry_open"
    assert exchange.cancels == []


def test_lp166_stop_loss_isolated_per_group(tmp_path) -> None:
    """C(隔离): 组 A 亏至 −$5 触发止损后，组 B 载荷逐字段与触发前一致
    （订单/仓位/状态）。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _PerTokenExchange()
    token_a = str(_lp166_market_identity(1)["token_id"])
    token_b = str(_lp166_market_identity(2)["token_id"])
    market_a = _lp166_market_identity(1)
    market_b = _lp166_market_identity(2)

    # A 的持有期盘口：0.30 × 100（front=100 → 保护不撤）；B 的持有期盘口：
    # 0.22 × 1000（B 挂单价 0.22，front 充足不撤）。
    hold_a = _lp166_book(now, market_a)
    hold_a["book"] = {
        **hold_a["book"],  # type: ignore[arg-type]
        "asks": [{"price": Decimal("0.31"), "size": Decimal("100")}],
        "bids": [{"price": Decimal("0.30"), "size": Decimal("100")}],
    }
    hold_b = _lp166_book(now, market_b)
    hold_b["book"] = {
        **hold_b["book"],  # type: ignore[arg-type]
        "asks": [{"price": Decimal("0.23"), "size": Decimal("1000")}],
        "bids": [{"price": Decimal("0.22"), "size": Decimal("1000")}],
    }
    exchange.by_token[token_a] = hold_a
    exchange.by_token[token_b] = hold_b

    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    entry_a = service.submit_entry(
        {**market_a, "price": Decimal("0.30"), "quantity": Decimal("100"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-iso-a",
    )
    assert entry_a["state"] == "entry_open", entry_a
    entry_b = service.submit_entry(
        {**market_b, "price": Decimal("0.22"), "quantity": Decimal("10"),
         "review_at": now + timedelta(minutes=10)},
        "lp166-iso-b",
    )
    assert entry_b["state"] == "entry_open"
    session_a = str(entry_a["session_id"])
    session_b = str(entry_b["session_id"])
    fields = (
        "state", "entry_order_id", "owned_order_ids", "buy_filled_quantity",
        "buy_cost", "residual_quantity", "review_at", "stop_loss_latched",
    )
    before_b = {k: store.lp_session(session_b)[k] for k in fields}
    # 保护基线锚不得被组 A 的止损重锚；逐 tick 的估计字段（front/ratio 等）
    # 是 B 自己读数的正常更新，不在此列。
    anchor_keys = (
        "baseline_front", "baseline_price", "baseline_source",
        "baseline_book_received_at", "baseline_source_timestamp",
        "baseline_book_hash", "baseline_version", "threshold",
    )
    # #167 改写：锚字段读投影视图（原始载荷首次写后自然落 v2 分桶形状）。
    before_anchor = {
        k: service.status(session_b)["queue_protection"][k] for k in anchor_keys
    }

    # A 成交 100@0.30，随后盘口跌至亏损 ≥ $5 触发止损；B 全程只读自己的盘口。
    buy_trade = {
        "trade_id": "buy-a",
        "token_id": token_a,
        "side": "BUY",
        "status": "CONFIRMED",
        "maker_orders": [{
            "order_id": str(entry_a["entry_order_id"]),
            "token_id": token_a,
            "side": "BUY",
            "matched_amount": Decimal("100"),
            "price": Decimal("0.30"),
            "fee": Decimal("0"),
        }],
    }
    filled_a = {
        **hold_a,
        "account": {
            **hold_a["account"],  # type: ignore[arg-type]
            "positions": [{"token_id": token_a, "size": Decimal("100")}],
        },
        "book": {
            "timestamp": now,
            "received_at": now,
            "asks": [{"price": Decimal("0.31"), "size": Decimal("100")}],
            "bids": [{"price": Decimal("0.30"), "size": Decimal("100")}],
        },
        "trades": [buy_trade],
        "orders": [{
            "order_id": str(entry_a["entry_order_id"]), "token_id": token_a,
            "side": "BUY", "status": "FILLED", "price": Decimal("0.30"),
            "original_size": Decimal("100"), "size_matched": Decimal("100"),
        }],
    }
    trigger_a = {
        **filled_a,
        "book": {
            "timestamp": now,
            "received_at": now,
            "asks": [{"price": Decimal("0.26"), "size": Decimal("100")}],
            "bids": [{"price": Decimal("0.25"), "size": Decimal("100")}],
        },
    }
    exchange.by_token[token_a] = filled_a
    first = service.tick()
    exchange.by_token[token_a] = trigger_a
    second = service.tick()

    after_a = store.lp_session(session_a)
    assert after_a["state"] == "stop_loss_exit"
    assert after_a["stop_loss_latched"] is True
    # first/second 是聚合报文：两组在场时顶层 session_id 为 None。
    assert first["session_id"] is None
    assert second["session_id"] is None

    after_b = store.lp_session(session_b)
    assert {k: after_b[k] for k in fields} == before_b
    assert {
        k: service.status(session_b)["queue_protection"][k] for k in anchor_keys
    } == before_anchor
    assert after_b["state"] == "entry_open"
    assert Decimal(str(after_b["buy_filled_quantity"])) == Decimal("0")


def test_lp166_tick_group_exception_does_not_block_others(tmp_path) -> None:
    """C(隔离): 注入组 A _reconcile_session 抛错，组 B 照常核对且
    account_checked_at / book_checked_at 非空。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _PerTokenExchange()
    token_a = str(_lp166_market_identity(1)["token_id"])
    token_b = str(_lp166_market_identity(2)["token_id"])
    exchange.by_token[token_a] = _lp166_book(now, _lp166_market_identity(1))
    exchange.by_token[token_b] = _lp166_book(now, _lp166_market_identity(2))
    store, service, session_a, session_b = _lp166_two_groups(
        now, tmp_path, exchange
    )

    original = service._reconcile_session

    def flaky(session):
        if str(session.get("session_id")) == session_a:
            raise RuntimeError("injected")
        return original(session)

    service._reconcile_session = flaky  # type: ignore[method-assign]

    result = service.tick()

    sessions = {str(row["session_id"]): row for row in result["sessions"]}
    assert sessions[session_a]["state"] == "error"
    assert sessions[session_b]["state"] == "entry_open"
    assert sessions[session_b]["account_checked_at"] is not None
    assert sessions[session_b]["book_checked_at"] is not None


def test_lp166_stop_and_status_semantics_with_two_groups(tmp_path) -> None:
    """F: 两组时 stop(None) 拒 session_ambiguous 且列全部活动组 id；
    stop(A) 精准停 A、B 不动；单组 stop(None) 照停；status(None) 回最新活动组；
    未知显式 id 返回 none 载荷。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    exchange = _PerTokenExchange()
    exchange.by_token[_lp166_market_identity(1)["token_id"]] = _lp166_book(
        now, _lp166_market_identity(1)
    )
    exchange.by_token[_lp166_market_identity(2)["token_id"]] = _lp166_book(
        now, _lp166_market_identity(2)
    )
    store, service, session_a, session_b = _lp166_two_groups(
        now, tmp_path, exchange
    )

    # 两组时 stop(None) → rejected/session_ambiguous，载荷列全部活动组 id
    #（顺序未定，服务端按最新在前）。
    ambiguous = service.stop(None)
    assert ambiguous["state"] == "rejected"
    assert ambiguous["reason"] == "session_ambiguous"
    assert len(ambiguous["session_ids"]) == 2
    assert set(ambiguous["session_ids"]) == {session_a, session_b}
    assert service.status(session_a)["state"] == "entry_open"
    assert service.status(session_b)["state"] == "entry_open"

    # stop(A) 精准停 A；B 状态不变。
    stopped = service.stop(session_a)
    assert stopped["state"] == "review"
    assert str(stopped["session_id"]) == session_a
    assert service.status(session_a)["state"] == "review"
    assert service.status(session_b)["state"] == "entry_open"
    assert not store.lp_session(session_b).get("stop_requested")

    # status(None) 回最新活动组（B 比 A 新建）。
    latest = service.status(None)
    assert str(latest["session_id"]) == session_b

    # 未知显式 id → none 载荷。
    assert service.status("missing-session") == {
        "state": "none",
        "session_id": None,
    }

    # B 终态化后单组场景：stop(None) 照停剩余活动组。
    store.lp_update_session(session_b, state="complete")
    exchange.by_token[_lp166_market_identity(1)["token_id"]] = _lp166_book(
        now, _lp166_market_identity(1)
    )
    store.lp_update_session(session_a, state="entry_open", patch={
        "stop_requested": None, "review_status": None, "reconciliation": None,
    })
    single = service.stop(None)
    assert single["state"] == "review"
    assert str(single["session_id"]) == session_a

    # 零组照旧：none 载荷。
    store.lp_update_session(session_a, state="complete")
    assert service.stop(None) == {"state": "none", "session_id": None}


# ---- Issue 167: 同组多价位追加与分价位位置保护 ----


def _lp167_book(
    now: datetime,
    *,
    level_42: object = 150,
    level_40: object | None = None,
    balance: object = 10000,
    open_orders: list[dict[str, object]] | None = None,
    orders: list[dict[str, object]] | None = None,
    trades: list[dict[str, object]] | None = None,
    positions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Two-level book: best bid 0.42 with an optional deeper 0.40 level."""

    base = _snapshot(now)
    base["account"].update(
        {
            "balance": Decimal(str(balance)),
            "allowance": Decimal(str(balance)),
            "open_orders": list(open_orders or []),
            "positions": list(positions or []),
        }
    )
    bids: list[dict[str, object]] = [
        {"price": Decimal("0.42"), "size": Decimal(str(level_42))}
    ]
    if level_40 is not None:
        bids.append({"price": Decimal("0.40"), "size": Decimal(str(level_40))})
    base["book"] = {
        "timestamp": now,
        "received_at": now,
        "source_timestamp": "2026-09-14T11:59:59Z",
        "hash": "book-hash-lp167",
        "asks": [{"price": Decimal("0.43"), "size": Decimal("100")}],
        "bids": bids,
    }
    base["orders"] = list(orders or [])
    base["trades"] = list(trades or [])
    return base


def _lp167_request(now: datetime) -> dict[str, object]:
    return {
        "market_id": "market-1",
        "condition_id": "0x" + "c" * 64,
        "token_id": "0x" + "1" * 64,
        "outcome": "YES",
        "question": "Will it happen?",
        "price": Decimal("0.42"),
        "quantity": Decimal("150"),
        "review_at": now + timedelta(minutes=10),
    }


def _lp167_running_service(tmp_path, now: datetime, *, key: str):
    """Start one registered entry session: 150 shares at 0.42 (level 150)."""

    exchange = _Exchange()
    exchange.snapshot_value = _lp167_book(now, level_42=150)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview(_lp167_request(now))
    started = service.start(str(preview["preview_id"]), key)
    assert started["state"] == "entry_open"
    return store, exchange, service, started


def test_lp167_s3_augment_price_above_best_bid_rejected(tmp_path) -> None:
    """S3: 追加价高于同快照顶档买一 → price_above_best_bid；非法价 → price_invalid；无 POST。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _lp167_running_service(
        tmp_path, now, key="lp167-s3"
    )
    session_id = str(started["session_id"])

    result = service.submit_augment(session_id, "100", "lp167-s3-key-1", price="0.50")
    assert result == {"state": "rejected", "reason": "price_above_best_bid"}
    assert service.submit_augment(session_id, "100", "lp167-s3-key-2", price="0") == {
        "state": "rejected",
        "reason": "price_invalid",
    }
    assert len(exchange.posts) == 1  # 仅既有入场单
    assert [
        action
        for action in store.lp_actions(session_id)
        if "augment-submit" in str(action["action_key"])
    ] == []


def test_lp167_s6_per_bucket_trigger_cancels_only_that_level(tmp_path) -> None:
    """S6: 分桶评估与触发——0.42 桶 front=130/310≈42%≤50% 触发只撤 0.42（含同价手工单）；
    0.40 桶 front=min(224,320-100)=220、A≈69% 监控不动。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _lp167_running_service(
        tmp_path, now, key="lp167-s6"
    )
    session_id = str(started["session_id"])
    exchange.snapshot_value = _lp167_book(
        now,
        level_42=380,
        level_40=224,
        open_orders=[_queue_receipt("order-1", price=Decimal("0.42"), original="150")],
    )
    result = service.submit_augment(session_id, "100", "lp167-s6-aug", price="0.40")
    assert result["state"] == "entry_open"

    manual = _queue_receipt(
        "manual-m", price=Decimal("0.42"), original="30"
    )
    exchange.snapshot_value = _lp167_book(
        now,
        level_42=310,
        level_40=320,
        open_orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100"),
            manual,
        ],
    )

    ticked = service.tick()

    assert exchange.cancels == ["order-1", "manual-m"]
    assert "order-2" not in exchange.cancels
    levels = ticked["queue_protection"]["levels"]
    bucket_42 = levels["0.42"]
    bucket_40 = levels["0.40"]
    assert bucket_42["state"] == "canceling"
    assert bucket_42["cancel_targets"] == ["order-1", "manual-m"]
    assert Decimal(str(bucket_42["front_estimate"])) == Decimal("130")
    assert Decimal(str(bucket_42["level_total"])) == Decimal("310")
    assert Decimal(str(bucket_42["ratio"])) == Decimal("130") / Decimal("310")
    assert bucket_40["state"] == "monitoring"
    assert Decimal(str(bucket_40["front_estimate"])) == Decimal("220")
    assert Decimal(str(bucket_40["level_total"])) == Decimal("320")
    assert Decimal(str(bucket_40["ratio"])) == Decimal("220") / Decimal("320")
    assert store.lp_session(session_id)["entry_cancel_requested"] is True


def _lp167_two_level_group(tmp_path, now: datetime, *, key: str):
    """One group with two resting levels: 150 @ 0.42 (order-1) + 100 @ 0.40
    (order-2), group_buy_quantity 250."""

    store, exchange, service, started = _lp167_running_service(
        tmp_path, now, key=key
    )
    session_id = str(started["session_id"])
    exchange.snapshot_value = _lp167_book(
        now,
        level_42=380,
        level_40=224,
        open_orders=[_queue_receipt("order-1", price=Decimal("0.42"), original="150")],
    )
    result = service.submit_augment(session_id, "100", f"{key}-aug", price="0.40")
    assert result["state"] == "entry_open"
    return store, exchange, service, session_id


def _lp167_maker_trade(
    trade_id: str,
    order_id: str,
    *,
    amount: object,
    price: object,
    token_id: str = "0x" + "1" * 64,
) -> dict[str, object]:
    return {
        "trade_id": trade_id,
        "status": "CONFIRMED",
        "maker_orders": [
            {
                "order_id": order_id,
                "side": "BUY",
                "token_id": token_id,
                "matched_amount": Decimal(str(amount)),
                "price": Decimal(str(price)),
            }
        ],
    }


def test_lp167_s7_any_level_fill_collects_whole_group(tmp_path) -> None:
    """S7: 任一价位成交 → 立即撤组内全部自有 BUY；0.40 桶随组收单落 canceled；
    不补买。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, session_id = _lp167_two_level_group(
        tmp_path, now, key="lp167-s7"
    )

    filling = _lp167_book(
        now,
        level_42=310,
        level_40=320,
        open_orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150", matched="60"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100"),
        ],
        orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150", matched="60"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100"),
        ],
        trades=[_lp167_maker_trade("t-1", "order-1", amount="60", price="0.42")],
        positions=[{"token_id": "0x" + "1" * 64, "size": Decimal("60")}],
    )
    exchange.snapshot_value = filling
    first = service.tick()
    # 0.42 桶成交 60 → 组内两张 BUY 全撤（跨价位）。
    assert exchange.cancels == ["order-1", "order-2"]
    levels = first["queue_protection"]["levels"]
    assert levels["0.42"]["state"] == "canceling"
    assert levels["0.42"]["cancel_reason"] == "group_fill_collect"
    assert levels["0.40"]["state"] == "canceling"
    assert levels["0.40"]["cancel_reason"] == "group_fill_collect"

    cancelled = _lp167_book(
        now,
        level_42=310,
        level_40=320,
        open_orders=[],
        orders=[
            _queue_receipt(
                "order-1", price=Decimal("0.42"), original="150",
                matched="60", status="CANCELED",
            ),
            _queue_receipt(
                "order-2", price=Decimal("0.40"), original="100",
                status="CANCELED",
            ),
        ],
        trades=[_lp167_maker_trade("t-1", "order-1", amount="60", price="0.42")],
        positions=[{"token_id": "0x" + "1" * 64, "size": Decimal("60")}],
    )
    exchange.snapshot_value = cancelled
    second = service.tick()

    assert Decimal(str(second["buy_filled_quantity"])) == Decimal("60")
    levels = second["queue_protection"]["levels"]
    assert levels["0.42"]["state"] == "partially_filled"
    assert Decimal(str(levels["0.42"]["partially_filled_quantity"])) == Decimal("60")
    assert levels["0.40"]["state"] == "canceled"
    assert levels["0.40"]["cancel_reason"] == "group_fill_collect"
    # 不补买：BUY 始终只有最初两张。
    assert len([item for item in exchange.posts if item["side"] == "BUY"]) == 2


def test_lp167_s8_cross_level_cost_and_single_stop_loss_latch(tmp_path) -> None:
    """S8: 两价位各成交一部分 → 组级合并成本 60×0.42+40×0.40=41.20；
    残值跌价 → 开仓亏损 ≥ $5 一次 latch（triggered_at/loss 不随后续 tick 改写）。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, session_id = _lp167_two_level_group(
        tmp_path, now, key="lp167-s8"
    )

    both_filled = _lp167_book(
        now,
        level_42=310,
        level_40=320,
        open_orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150", matched="60"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100", matched="40"),
        ],
        orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150", matched="60"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100", matched="40"),
        ],
        trades=[
            _lp167_maker_trade("t-1", "order-1", amount="60", price="0.42"),
            _lp167_maker_trade("t-2", "order-2", amount="40", price="0.40"),
        ],
        positions=[{"token_id": "0x" + "1" * 64, "size": Decimal("100")}],
    )
    exchange.snapshot_value = both_filled
    assert service.tick()["state"] in {"entry_open", "stop_loss_exit"}

    # 两张单终态：持仓 100， bids 只够 100 份 × ~0.295 → 亏损 ≥ $5 触发止损。
    cancelled = _lp167_book(
        now,
        level_42=310,
        level_40=320,
        open_orders=[],
        orders=[
            _queue_receipt(
                "order-1", price=Decimal("0.42"), original="150",
                matched="60", status="CANCELED",
            ),
            _queue_receipt(
                "order-2", price=Decimal("0.40"), original="100",
                matched="40", status="CANCELED",
            ),
        ],
        trades=[
            _lp167_maker_trade("t-1", "order-1", amount="60", price="0.42"),
            _lp167_maker_trade("t-2", "order-2", amount="40", price="0.40"),
        ],
        positions=[{"token_id": "0x" + "1" * 64, "size": Decimal("100")}],
    )
    cancelled["book"]["bids"] = [
        {"price": Decimal("0.30"), "size": Decimal("50")},
        {"price": Decimal("0.29"), "size": Decimal("50")},
    ]
    exchange.snapshot_value = cancelled
    stop = service.tick()

    assert store.lp_session(session_id)["state"] == "stop_loss_exit"
    status = service.status(session_id)
    assert status["stop_loss_latched"] is True
    assert Decimal(str(status["buy_cost"])) == Decimal("41.20")
    assert Decimal(str(status["buy_filled_quantity"])) == Decimal("100")
    latched_loss = Decimal(str(status["stop_loss_triggered_loss"]))
    assert latched_loss == Decimal("41.20") - Decimal("29.50")
    assert status["stop_loss_triggered_at"] is not None
    latched_at = status["stop_loss_triggered_at"]

    # 后续 tick 亏损更大也只保留第一次 latch 证据。
    deeper = _lp167_book(
        now,
        level_42=310,
        level_40=320,
        open_orders=[],
        orders=cancelled["orders"],
        trades=cancelled["trades"],
        positions=[{"token_id": "0x" + "1" * 64, "size": Decimal("100")}],
    )
    deeper["book"]["bids"] = [{"price": Decimal("0.10"), "size": Decimal("100")}]
    exchange.snapshot_value = deeper
    again = service.tick()
    assert store.lp_session(session_id)["state"] == "stop_loss_exit"
    status = service.status(session_id)
    assert Decimal(str(status["stop_loss_triggered_loss"])) == latched_loss
    assert status["stop_loss_triggered_at"] == latched_at


def test_lp167_s10_outage_conservative_cancel_covers_both_levels(tmp_path) -> None:
    """S10: 组级 data_failures 满 10 → 保守撤组内全部自有 BUY（两价位 + 0.42 手工单）。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _AccountReadExchange()
    exchange.snapshot_value = _lp167_book(now, level_42=150)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview(_lp167_request(now))
    started = service.start(str(preview["preview_id"]), "lp167-s10")
    session_id = str(started["session_id"])
    exchange.snapshot_value = _lp167_book(
        now,
        level_42=380,
        level_40=224,
        open_orders=[_queue_receipt("order-1", price=Decimal("0.42"), original="150")],
    )
    assert service.submit_augment(session_id, "100", "lp167-s10-aug", price="0.40")[
        "state"
    ] == "entry_open"

    exchange.account_open_orders = [
        _queue_receipt("order-1", price=Decimal("0.42"), original="150"),
        _queue_receipt("order-2", price=Decimal("0.40"), original="100"),
        _queue_receipt("manual-m", price=Decimal("0.42"), original="30"),
    ]
    exchange.snapshot_value = None
    exchange.snapshots = []
    exchange.snapshot_calls = 0

    for expected_failures in range(1, 10):
        result = service.tick()
        assert result["state"] == "needs_attention"
        protection = result["queue_protection"]
        assert Decimal(str(protection["data_failures"])) == expected_failures
        assert exchange.cancels == []

    conservative = service.tick()
    protection = conservative["queue_protection"]
    # levels 键经 store sort_keys 落盘 → 桶迭代按价格字典序（0.40 先于 0.42）。
    assert exchange.cancels == ["order-2", "order-1", "manual-m"]
    assert store.lp_session(session_id)["entry_cancel_requested"] is True
    levels = protection["levels"]
    assert levels["0.40"]["state"] == "canceling"
    assert levels["0.40"]["cancel_reason"] == "book_unreliable"
    assert levels["0.40"]["cancel_targets"] == ["order-2"]
    assert levels["0.42"]["state"] == "canceling"
    assert levels["0.42"]["cancel_reason"] == "book_unreliable"
    assert levels["0.42"]["cancel_targets"] == ["order-1", "manual-m"]


def test_lp167_s9_review_deadline_collects_both_levels(tmp_path) -> None:
    """S9: 复核截止（首单 review_at）——一次收掉两价位自有 BUY，桶落 canceling→canceled，
    会话进 awaiting_reconciliation。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _queue_running_service(
        tmp_path, now, key="lp167-s9"
    )
    session_id = str(started["session_id"])
    augment_snapshot = _queue_book_snapshot(now, Decimal("10000"))
    augment_snapshot["book"]["bids"] = [
        {"price": Decimal("0.30"), "size": Decimal("10000")},
        {"price": Decimal("0.29"), "size": Decimal("224")},
    ]
    augment_snapshot["account"]["balance"] = Decimal("10000")
    augment_snapshot["account"]["allowance"] = Decimal("10000")
    augment_snapshot["account"]["open_orders"] = [
        _queue_receipt("order-1", original="2000")
    ]
    augment_snapshot["orders"] = [_queue_receipt("order-1", original="2000")]
    exchange.snapshot_value = augment_snapshot
    preview = _augment_preview(service, session_id, 90, price="0.29")
    assert preview["state"] == "previewed"
    result = service.augment(session_id, str(preview["preview_id"]), "lp167-s9-aug")
    assert result["state"] == "entry_open"

    class _Clock:
        current = now

    service2 = PolymarketLPService(store, exchange, clock=lambda: _Clock.current)
    _Clock.current = now + timedelta(minutes=11)
    # 盘口时间戳跟随当前时钟，避免新鲜度门先行拦截。
    live = _queue_book_snapshot(_Clock.current, Decimal("10000"))
    live["book"]["bids"] = [
        {"price": Decimal("0.30"), "size": Decimal("10000")},
        {"price": Decimal("0.29"), "size": Decimal("320")},
    ]
    live["account"]["open_orders"] = [
        _queue_receipt("order-1", original="2000"),
        _queue_receipt("order-2", price=Decimal("0.29"), original="90"),
    ]
    live["orders"] = [
        _queue_receipt("order-1", original="2000"),
        _queue_receipt("order-2", price=Decimal("0.29"), original="90"),
    ]
    exchange.snapshot_value = live
    deadline = service2.tick()

    assert exchange.cancels == ["order-1", "order-2"]
    assert deadline["state"] == "review"
    assert deadline["review_status"] == "awaiting_reconciliation"
    levels = deadline["queue_protection"]["levels"]
    assert levels["0.30"]["state"] == "canceling"
    assert levels["0.30"]["cancel_reason"] == "review_deadline"
    assert levels["0.29"]["state"] == "canceling"
    assert levels["0.29"]["cancel_reason"] == "review_deadline"

    _Clock.current = now + timedelta(minutes=11, seconds=5)
    cancelled = _queue_book_snapshot(_Clock.current, Decimal("10000"))
    cancelled["book"]["bids"] = [
        {"price": Decimal("0.30"), "size": Decimal("10000")},
        {"price": Decimal("0.29"), "size": Decimal("320")},
    ]
    cancelled["account"]["open_orders"] = []
    cancelled["orders"] = [
        _queue_receipt("order-1", original="2000", status="CANCELED"),
        _queue_receipt(
            "order-2", price=Decimal("0.29"), original="90", status="CANCELED"
        ),
    ]
    exchange.snapshot_value = cancelled
    service2.tick()

    levels = store.lp_session(session_id)["queue_protection"]["levels"]
    assert levels["0.30"]["state"] == "canceled"
    assert levels["0.29"]["state"] == "canceled"
    assert store.lp_session(session_id)["review_status"] == "awaiting_reconciliation"


def test_lp167_s5_augment_state_gate_six_states_with_price(tmp_path) -> None:
    """S5: 状态门禁只放行 entry_open——其余五态各回真实原因（显式 price 也不越过门禁）。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    cases = [
        ("review", "session_review_exit"),
        ("stop_loss_exit", "session_stop_loss_exit"),
        ("needs_attention", "session_needs_attention"),
        ("passive_exit", "session_passive_exit"),
        ("entry_submit_pending", "session_entry_pending"),
        ("complete", "session_not_active"),
    ]
    for index, (state, expected_reason) in enumerate(cases):
        store, exchange, service, started = _lp167_running_service(
            tmp_path / f"s5-{index}", now, key=f"lp167-s5-{index}"
        )
        session_id = str(started["session_id"])
        store.lp_update_session(session_id, state=state)
        posts_before = len(exchange.posts)

        result = service.submit_augment(
            session_id, "100", f"lp167-s5-{index}-k", price="0.40"
        )

        assert result == {"state": "rejected", "reason": expected_reason}
        assert len(exchange.posts) == posts_before


def test_lp167_s2_same_key_replay_produces_single_order(tmp_path) -> None:
    """S2: 同键重放只产一单——两级价位组上重放返既有结果，买一再变也不重发。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, session_id = _lp167_two_level_group(
        tmp_path, now, key="lp167-s2"
    )
    calls_after_submit = exchange.snapshot_calls
    exchange.snapshot_value = _queue_book_moved_bid_snapshot(now, Decimal("9000"))

    replay = service.submit_augment(session_id, "100", "lp167-s2-aug")

    assert replay["state"] == "entry_open"
    assert str(replay["augment_order_id"]) == "order-2"
    assert len(exchange.posts) == 2
    assert exchange.snapshot_calls == calls_after_submit  # 重放不再读快照


def test_lp167_s12_own_orders_tolerated_foreign_order_blocks(tmp_path) -> None:
    """S12: 组内自有单（两价位）不算外部冲突；真外部单（同 token 未管理价位）
    仍令 tick 进 needs_attention/unowned_target_order。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, session_id = _lp167_two_level_group(
        tmp_path, now, key="lp167-s12"
    )

    # 自有两价位在挂：tick 照常 entry_open（自有单豁免）。
    own = _lp167_book(
        now,
        level_42=10000,
        level_40=320,
        open_orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100"),
        ],
        orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100"),
        ],
    )
    exchange.snapshot_value = own
    assert service.tick()["state"] == "entry_open"

    # 真外部单：同 token SELL（按 #152 设计同 token BUY 属保护伞豁免，SELL 不豁免）。
    foreign = _lp167_book(
        now,
        level_42=10000,
        level_40=320,
        open_orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100"),
            _queue_receipt("stranger-sell", side="SELL", price=Decimal("0.43"), original="500"),
        ],
        orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100"),
            _queue_receipt("stranger-sell", side="SELL", price=Decimal("0.43"), original="500"),
        ],
    )
    exchange.snapshot_value = foreign
    result = service.tick()
    assert result["state"] == "needs_attention"
    assert result["reconciliation"] == "unowned_target_order"


def test_lp167_s11_legacy_and_broken_payload_compat(tmp_path) -> None:
    """S11: 三档兼容兜底——无键不启用保护；老标量读时包成单桶继续跑（写落新形状）；
    残缺桶仅置 unknown、不影响其他桶与组级核算。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

    # a) 无 queue_protection 键：视为未启用保护，tick 照常、不抛错、不落键。
    exchange_a = _Exchange()
    exchange_a.snapshot_value = _queue_runtime_snapshot(
        now, bid_size="10000", orders=[_queue_receipt("order-1")]
    )
    store_a = PredictionArbitrageStore(tmp_path / "a")
    store_a.lp_create_session(
        "lp167-s11-a",
        "lp167-s11-a-key",
        state="entry_open",
        payload={
            "market_id": "market-1",
            "condition_id": "0x" + "c" * 64,
            "token_id": "0x" + "1" * 64,
            "outcome": "YES",
            "question": "Will it happen?",
            "price": Decimal("0.30"),
            "quantity": Decimal("2000"),
            "review_at": now + timedelta(minutes=10),
            "entry_order_id": "order-1",
            "order_history": {
                "order-1": {"order_id": "order-1", "side": "BUY", "status": "LIVE"}
            },
        },
    )
    service_a = PolymarketLPService(store_a, exchange_a, clock=lambda: now)
    ticked_a = service_a.tick()
    assert "queue_protection" not in ticked_a
    assert exchange_a.cancels == []

    # b) 老标量形状（有 baseline_price 无 levels）：读时包成单价位桶继续跑；
    # 触发撤单照常，写回载荷自然落 v2 形状。
    exchange_b = _Exchange()
    exchange_b.snapshot_value = _queue_runtime_snapshot(
        now, bid_size="4000", orders=[_queue_receipt("order-1")]
    )
    store_b = PredictionArbitrageStore(tmp_path / "b")
    store_b.lp_create_session(
        "lp167-s11-b",
        "lp167-s11-b-key",
        state="entry_open",
        payload={
            "market_id": "market-1",
            "condition_id": "0x" + "c" * 64,
            "token_id": "0x" + "1" * 64,
            "outcome": "YES",
            "question": "Will it happen?",
            "price": Decimal("0.30"),
            "quantity": Decimal("2000"),
            "review_at": now + timedelta(minutes=10),
            "entry_order_id": "order-1",
            "order_history": {
                "order-1": {"order_id": "order-1", "side": "BUY", "status": "LIVE"}
            },
            "queue_protection": {
                "baseline_price": "0.30",
                "baseline_front": "10000",
                "baseline_source": "submit",
                "threshold": "0.5",
                "data_failures": 0,
                "state": "registered",
                "notification_sent": False,
                "cancel_scope": "own_buys_at_level",
                "reason_codes": [],
            },
        },
    )
    service_b = PolymarketLPService(store_b, exchange_b, clock=lambda: now)
    ticked_b = service_b.tick()
    assert exchange_b.cancels == ["order-1"]
    assert ticked_b["queue_protection"]["state"] == "canceling"
    stored = store_b.lp_session("lp167-s11-b")["queue_protection"]
    assert stored["version"] == 2
    assert set(stored["levels"]) == {"0.30"}
    assert stored["levels"]["0.30"]["state"] == "canceling"

    # c) v2 某桶字段残缺：仅该桶置 unknown，好桶照常评估，组级核算不动。
    exchange_c = _Exchange()
    exchange_c.snapshot_value = _queue_runtime_snapshot(
        now, bid_size="10000", orders=[_queue_receipt("order-1")]
    )
    store_c = PredictionArbitrageStore(tmp_path / "c")
    store_c.lp_create_session(
        "lp167-s11-c",
        "lp167-s11-c-key",
        state="entry_open",
        payload={
            "market_id": "market-1",
            "condition_id": "0x" + "c" * 64,
            "token_id": "0x" + "1" * 64,
            "outcome": "YES",
            "question": "Will it happen?",
            "price": Decimal("0.30"),
            "quantity": Decimal("2000"),
            "review_at": now + timedelta(minutes=10),
            "entry_order_id": "order-1",
            "order_history": {
                "order-1": {"order_id": "order-1", "side": "BUY", "status": "LIVE"}
            },
            "queue_protection": {
                "version": 2,
                "data_failures": 0,
                "levels": {
                    "0.30": {
                        "order_id": "order-1",
                        "baseline_price": "0.30",
                        "baseline_front": "10000",
                        "threshold": "0.5",
                        "state": "registered",
                        "notification_sent": False,
                        "reason_codes": [],
                    },
                    "broken": {"state": "registered"},
                },
            },
        },
    )
    service_c = PolymarketLPService(store_c, exchange_c, clock=lambda: now)
    ticked_c = service_c.tick()
    levels = ticked_c["queue_protection"]["levels"]
    assert levels["broken"]["state"] == "unknown"
    assert levels["0.30"]["state"] == "monitoring"
    assert Decimal(str(levels["0.30"]["ratio"])) == Decimal("0.80")
    assert exchange_c.cancels == []


def test_lp167_s1_augment_new_price_level_registers_bucket(tmp_path) -> None:
    """S1: 追加 0.40 成功：新单号、两桶、review_at 不变、order_history 两键。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _lp167_running_service(
        tmp_path, now, key="lp167-s1"
    )
    session_id = str(started["session_id"])
    review_at_before = store.lp_session(session_id)["review_at"]
    exchange.snapshot_value = _lp167_book(
        now,
        level_42=380,
        level_40=224,
        open_orders=[_queue_receipt("order-1", price=Decimal("0.42"), original="150")],
    )

    result = service.submit_augment(session_id, "100", "lp167-s1-key-1", price="0.40")

    assert result["state"] == "entry_open"
    assert str(result["augment_order_id"]) == "order-2"
    posted = exchange.posts[1]
    assert posted["side"] == "BUY"
    assert posted["post_only"] is True
    assert Decimal(str(posted["price"])) == Decimal("0.40")
    assert Decimal(str(posted["quantity"])) == Decimal("100")

    after = store.lp_session(session_id)
    assert after["review_at"] == review_at_before
    assert after["augment_order_ids"] == ["order-2"]
    assert set(after["order_history"]) == {"order-1", "order-2"}
    protection = after["queue_protection"]
    assert protection["version"] == 2
    assert set(protection["levels"]) == {"0.42", "0.40"}
    bucket_42 = protection["levels"]["0.42"]
    bucket_40 = protection["levels"]["0.40"]
    assert str(bucket_42["order_id"]) == "order-1"
    assert Decimal(str(bucket_42["baseline_price"])) == Decimal("0.42")
    assert Decimal(str(bucket_42["baseline_front"])) == Decimal("150")
    assert bucket_42["state"] == "registered"
    assert str(bucket_40["order_id"]) == "order-2"
    assert Decimal(str(bucket_40["baseline_price"])) == Decimal("0.40")
    assert Decimal(str(bucket_40["baseline_front"])) == Decimal("224")
    assert bucket_40["state"] == "registered"
    assert Decimal(str(bucket_40["threshold"])) == Decimal("0.5")
    assert protection["data_failures"] == 0


def test_lp167_s4_augment_price_level_active_rejected(tmp_path) -> None:
    """S4: 目标价位仍有非终态自有单（同价补量、缺省组价同因）→ price_level_active。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    store, exchange, service, started = _lp167_running_service(
        tmp_path, now, key="lp167-s4"
    )
    session_id = str(started["session_id"])
    exchange.snapshot_value = _lp167_book(
        now,
        level_42=380,
        open_orders=[_queue_receipt("order-1", price=Decimal("0.42"), original="150")],
    )

    same_price = service.submit_augment(
        session_id, "90", "lp167-s4-key-1", price="0.42"
    )
    assert same_price == {"state": "rejected", "reason": "price_level_active"}
    # 缺省价 = 组价 = 0.42：入场单仍在挂，同因拒绝。
    default_price = service.submit_augment(session_id, "90", "lp167-s4-key-2")
    assert default_price == {"state": "rejected", "reason": "price_level_active"}
    assert len(exchange.posts) == 1
    assert [
        action
        for action in store.lp_actions(session_id)
        if "augment-submit" in str(action["action_key"])
    ] == []


def test_lp167_r1_same_tick_augment_bucket_cancels_merge_union(tmp_path) -> None:
    """R1（issue 167 修复环评审）：同一 tick 两个追加价位桶都触发撤单时，
    会话级 ``augment_cancel_requested`` 必须按并集合并。评审复现：三价位组
    （入场 150@0.42 + 追加 100@0.40 + 追加 100@0.38），0.40/0.38 两桶同
    tick 触发，逐桶 ``session_patch.update(patch)`` 后写覆盖前写，持久化
    只剩一单；该单随后被误判「未请求撤单」而被重复撤单。断言：并集落盘
    （不丢单）、场所端每单恰一次撤单（无重复）、两桶各自 canceling 且
    targets 各归各桶。"""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    exchange = _Exchange()
    # 起始 0.42 档 310 → 入场桶基线 front=310，触发 tick 里 230/380>50% 不触发。
    exchange.snapshot_value = _lp167_book(now, level_42=310)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    preview = service.preview(_lp167_request(now))
    started = service.start(str(preview["preview_id"]), "lp167-r1")
    session_id = str(started["session_id"])

    # 追加 100 @ 0.40：提交时 0.40 档 224 → 桶基线 front=224。
    exchange.snapshot_value = _lp167_book(
        now,
        level_42=380,
        level_40=224,
        open_orders=[_queue_receipt("order-1", price=Decimal("0.42"), original="150")],
    )
    assert service.submit_augment(session_id, "100", "lp167-r1-aug-40", price="0.40")[
        "state"
    ] == "entry_open"

    # 追加 100 @ 0.38：提交时 0.38 档 224 → 桶基线 front=224。
    augment_book = _lp167_book(
        now,
        level_42=380,
        level_40=320,
        open_orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100"),
        ],
    )
    augment_book["book"]["bids"].append(
        {"price": Decimal("0.38"), "size": Decimal("224")}
    )
    exchange.snapshot_value = augment_book
    assert service.submit_augment(session_id, "100", "lp167-r1-aug-38", price="0.38")[
        "state"
    ] == "entry_open"

    # 触发 tick：0.42 桶 front=min(310,380-150)=230、A=230/380>50% 监控不动；
    # 0.40 桶 front=min(224,200-100)=100、A=100/200=50% 触发；
    # 0.38 桶 front=min(224,180-100)=80、A=80/180≈44% 触发——两个追加桶
    # 在同一 tick 各自触发撤单。
    trigger_book = _lp167_book(
        now,
        level_42=380,
        level_40=200,
        open_orders=[
            _queue_receipt("order-1", price=Decimal("0.42"), original="150"),
            _queue_receipt("order-2", price=Decimal("0.40"), original="100"),
            _queue_receipt("order-3", price=Decimal("0.38"), original="100"),
        ],
    )
    trigger_book["book"]["bids"].append(
        {"price": Decimal("0.38"), "size": Decimal("180")}
    )
    exchange.snapshot_value = trigger_book

    ticked = service.tick()

    # 1. 会话级并集落盘：两张追加单都在（修复前只剩后写桶的一单）。
    persisted = store.lp_session(session_id)["augment_cancel_requested"]
    assert persisted == ["order-2", "order-3"]
    # 2. 场所端每单恰一次撤单请求（无重复）；入场桶不动，order-1 不撤。
    assert sorted(exchange.cancels) == ["order-2", "order-3"]
    assert len(exchange.cancels) == len(set(exchange.cancels))
    assert "order-1" not in exchange.cancels
    # 3. 两桶各自 state=canceling、targets 各归各桶；入场桶保持 monitoring。
    levels = ticked["queue_protection"]["levels"]
    assert levels["0.40"]["state"] == "canceling"
    assert levels["0.40"]["cancel_targets"] == ["order-2"]
    assert levels["0.38"]["state"] == "canceling"
    assert levels["0.38"]["cancel_targets"] == ["order-3"]
    assert levels["0.42"]["state"] == "monitoring"
