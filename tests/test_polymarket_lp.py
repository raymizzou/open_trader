from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import threading

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
    # so a round whose only market was rejected reports 0, matching the
    # empty table and the progress line.
    assert funnel["trial"] == 0
    assert funnel["stop_reason"] == "queue_exhausted"
    assert funnel["checked"] == 1
    assert funnel["passed"] == 0
    assert funnel["rejected"] == 1
    assert funnel["unknown"] == 0
    assert funnel["unchecked"] == 0
    assert snapshot["candidates"] == []
    assert snapshot["recommendations"] == []
    assert snapshot["selected_results"] == []
    assert snapshot["selected_market_ids"] == []
    assert funnel["gap_reason"] is not None
    assert "本轮通过 0 个" in funnel["gap_reason"]


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
    """Issue 143 + #138 round 2: with 12 qualifying markets the scan runs
    two batches and keeps checking past the first ten passers.

    Batch one reads ten markets (20 tokens) in one call; batch two reads the
    remaining two (4 tokens); no token is requested twice; the merged table
    keeps the best ten live-qualified passers and the round ends only when
    the queue is exhausted.
    """

    now = datetime(2026, 9, 17, 3, tzinfo=UTC)
    exchange = _LPCandidateQueryExchange(
        now, {f"M{index:02d}": Decimal(200 - index) for index in range(1, 13)}
    )
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
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
    # All twelve queued markets are checked (two batch calls); the ten best
    # estimated yields are published.
    assert len(exchange.book_token_reads) == 2
    first = exchange.book_token_reads[0]
    # Single-outcome fixture: one token per market, one call per batch.
    assert len(first) == 10
    funnel = snapshot["funnel"]
    assert funnel["stop_reason"] == "queue_exhausted"
    assert funnel["checked"] == 12
    assert funnel["passed"] == 12
    assert funnel["rejected"] == 0
    assert funnel["unknown"] == 0
    assert funnel["unchecked"] == 0
    assert funnel["batches"] == 2
    assert funnel["backup_read"] == 0
    assert funnel["trial"] == 10
    assert funnel["normal_queue_count"] == 12
    assert funnel["backup_queue_count"] == 0
    assert funnel["reference_price_unknown"] == 0


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
    """S2 acceptance case 1: 3 pass in batch one, batches backfill to ten."""
    now = datetime(2026, 9, 19, 6, tzinfo=UTC)
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
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    # Backup markets keep their stale summaries, so preparation reports
    # partial; the scan proceeds and treats those markets as backup rows.
    assert lp.refresh_price_history()["state"] in {"known", "partial"}

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    # Two batches of ten markets each: 9 normal + 1 backup, 20 tokens per
    # call, no token read twice.
    assert len(exchange.book_token_reads) == 2
    first, second = exchange.book_token_reads
    assert len(first) == len(second) == 20
    assert len(set(first) | set(second)) == 40
    first_markets = {
        token.removeprefix("token-").rsplit("-", 1)[0] for token in first
    }
    second_markets = {
        token.removeprefix("token-").rsplit("-", 1)[0] for token in second
    }
    assert first_markets == {
        *{f"condition-N{index:02d}" for index in range(1, 10)},
        "condition-B1",
    }
    assert second_markets == {
        *{f"condition-N{index:02d}" for index in range(10, 19)},
        "condition-B2",
    }
    funnel = snapshot["funnel"]
    assert funnel["stop_reason"] == "queue_exhausted"
    assert funnel["checked"] == 20
    assert funnel["passed"] == 10
    assert funnel["rejected"] == 10
    assert funnel["unknown"] == 0
    assert funnel["unchecked"] == 0
    assert funnel["batches"] == 2
    assert funnel["backup_read"] == 2
    candidates = snapshot["candidates"]
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
    # Equal actual capital (20 × 0.34 = 6.80): the highest pool among the
    # passers (N01, pool 499) yields the highest optimistic upper bound.
    assert candidates[0]["market_id"] == "market-N01"
    assert snapshot["recommendations"][0]["market_id"] == "market-N01"
    assert snapshot["selected_results"][0]["market_id"] == "market-N01"
    assert snapshot["selected_market_ids"] == [
        row["market_id"] for row in candidates
    ]


def test_batch_refresh_stops_at_fifty_markets_checked(tmp_path) -> None:
    """S3 acceptance case 2: only four pass, the round budget stops at 50."""
    now = datetime(2026, 9, 19, 7, tzinfo=UTC)
    pools = {f"N{index:02d}": Decimal(600 - index) for index in range(1, 61)}
    exchange = _LPBatchQueryExchange(
        now,
        pools,
        reject=frozenset({f"N{index:02d}" for index in range(5, 61)}),
    )
    lp = PolymarketLPService(
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    assert lp.refresh_price_history()["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    assert snapshot["state"] == "ready"
    funnel = snapshot["funnel"]
    assert funnel["stop_reason"] == "checked_limit"
    assert funnel["checked"] == 50
    assert funnel["passed"] == 4
    assert funnel["rejected"] == 46
    assert funnel["unknown"] == 0
    assert funnel["unchecked"] == 60 - 50
    assert funnel["batches"] == 5
    assert funnel["backup_read"] == 0
    # Binary markets: 50 markets × 2 tokens = 100 first-read tokens, each
    # requested exactly once across five ≤20-token batch calls.
    all_tokens = [token for batch in exchange.book_token_reads for token in batch]
    assert len(exchange.book_token_reads) == 5
    assert len(all_tokens) == 100
    assert len(set(all_tokens)) == 100
    candidates = snapshot["candidates"]
    assert [row["market_id"] for row in candidates] == [
        "market-N01", "market-N02", "market-N03", "market-N04",
    ]
    assert all(row["selected_direction"] is not None for row in candidates)
    assert snapshot["recommendations"][0]["market_id"] == "market-N01"


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

    snapshot = lp.refresh_candidates(force=True)

    funnel = snapshot["funnel"]
    assert funnel["checked"] == 30
    assert funnel["batches"] == 3
    assert funnel["rejected"] == 20
    assert funnel["passed"] == 10
    assert funnel["unknown"] == 0
    assert funnel["stop_reason"] == "queue_exhausted"
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
    assert funnel["stop_reason"] == "queue_exhausted"
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
    assert account_funnel["stop_reason"] == "queue_exhausted"
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
    assert funnel["stop_reason"] == "queue_exhausted"
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

    first = lp.refresh_candidates(force=True)

    funnel = first["funnel"]
    assert funnel["checked"] == 40
    assert funnel["batches"] == 4
    assert funnel["rejected"] == 20
    assert funnel["passed"] == 0
    assert funnel["unknown"] == 20
    assert funnel["stop_reason"] == "queue_exhausted"
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
    assert exchange.metadata_fresh_calls == 1
    assert exchange.metadata_fresh_reads == (
        tuple(f"condition-N{index:02d}" for index in range(21, 31)),
    )
    assert len(exchange.targeted_reward_reads) == 2
    assert set(exchange.targeted_reward_reads[0]) == {
        f"condition-N{index:02d}" for index in range(21, 31)
    }
    assert set(exchange.targeted_reward_reads[1]) == {
        f"condition-N{index:02d}" for index in range(31, 41)
    }
    assert exchange.account_reads == 2

    # The next forced round retries the renewal and recovers.
    exchange.metadata_fresh_mode = "fresh"
    second = lp.refresh_candidates(force=True)

    second_funnel = second["funnel"]
    # Issue #138 round 2: the scan consumes the whole 40-market queue, so
    # batch four (N31..N40) is checked too even though ten passers merged
    # in batch three.
    assert second_funnel["checked"] == 40
    assert second_funnel["batches"] == 4
    # Batches three and four both pass on the recovered facts: twenty
    # passers merge, and the published table keeps the best ten yields
    # (N21..N30, the highest pools among them).
    assert second_funnel["passed"] == 20
    assert second_funnel["rejected"] == 20
    assert second_funnel["unknown"] == 0
    assert second_funnel["stop_reason"] == "queue_exhausted"
    # The recovery round renews each batch's own conditions once (its
    # round-start receipts are the still-stale prepared stamps), so four
    # further targeted calls — one per batch, never wider than the batch.
    assert exchange.metadata_fresh_calls == 5
    assert exchange.metadata_fresh_reads == (
        tuple(f"condition-N{index:02d}" for index in range(21, 31)),
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
    # Batch one: N01..N09 + B1 (its two books are missing from the response).
    # Batch two: N10, N11 + B2, all passing and ending the round with ten
    # merged passers.
    assert len(exchange.book_token_reads) == 2
    missing = {"token-condition-B1-yes", "token-condition-B1-no"}
    # Each missing token was requested exactly once and never retried.
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
    funnel = snapshot["funnel"]
    assert funnel["stop_reason"] == "queue_exhausted"
    assert funnel["checked"] == 13
    assert funnel["passed"] == 12
    assert funnel["rejected"] == 0
    assert funnel["unknown"] == 1
    assert funnel["backup_read"] == 2
    unknown_reasons = [
        row for row in funnel["reasons"]["trial"]
        if row.get("condition_id") == "condition-B1"
    ]
    assert unknown_reasons
    assert all(row.get("code") == "book_unknown" for row in unknown_reasons)
    candidates = snapshot["candidates"]
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
    # 12 kept markets: the view preview batch is capped at 10, but only 8
    # markets pass the live batch check (4 reject off-tick) and only passers
    # are published under the passers-only table.
    assert funnel["checked"] == 12
    assert funnel["passed"] == 8
    assert len(candidates) == 8
    assert all(row["state"] == "eligible" for row in candidates)
    # Funnel, progress line, and table agree on the trial stage.
    assert funnel["trial"] == 8
    assert funnel["trial"] == len(candidates)


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
    """S6: 300-second scan gate, shared in-flight rounds, stale writes lose."""
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
    competition_after_first = exchange.competition_reads
    assert reads_after_first == 1

    # Inside the 300-second window a non-force call returns the snapshot
    # with zero external reads.
    current["now"] = now + timedelta(seconds=60)
    exchanged = lp.refresh_candidates(force=False)
    assert len(exchange.book_token_reads) == reads_after_first
    assert exchange.competition_reads == competition_after_first
    assert (
        [row["market_id"] for row in exchanged["candidates"]]
        == [row["market_id"] for row in first["candidates"]]
    )

    # A manual force starts a new round immediately.
    current["now"] = now + timedelta(seconds=61)
    exchange.now = current["now"]
    forced = lp.refresh_candidates(force=True)
    assert len(exchange.book_token_reads) == reads_after_first + 1
    assert exchange.competition_reads == competition_after_first + 1
    assert forced["funnel"]["batches"] == 1

    # Concurrent calls share one in-flight round: while a force round is
    # blocked inside its batch read, a non-force call returns the scanning
    # snapshot instead of starting a second round.
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
    blocked_reads = len(blocked_exchange.book_token_reads)
    round_result: dict[str, object] = {}

    def run_round() -> None:
        round_result["snapshot"] = blocked_lp.refresh_candidates(force=True)

    round_thread = threading.Thread(target=run_round)
    round_thread.start()
    assert entered.wait(timeout=5)
    shared = blocked_lp.refresh_candidates(force=False)
    assert shared["scanning"] is True
    assert len(blocked_exchange.book_token_reads) == blocked_reads

    # A snapshot persisted by a newer round wins over this blocked older
    # round: the stale response must not overwrite the newer result.
    newer_started = current["now"] + timedelta(seconds=3600)
    marker = {"market_id": "market-newer", "condition_id": "condition-newer"}
    shared_store.lp_save_screening_snapshot(
        {
            "state": "ready",
            "complete": True,
            "candidate_rows_fresh": True,
            "scan_started_at": newer_started.isoformat().replace("+00:00", "Z"),
            "funnel": {"checked": 0},
            "selected_market_ids": ["market-newer"],
            "candidates": [marker],
            "recommendations": [],
            "selected_results": [],
        }
    )
    release.set()
    round_thread.join(timeout=5)
    settled = round_result["snapshot"]
    assert [row["market_id"] for row in settled["candidates"]] == ["market-newer"]
    assert blocked_lp.candidate_snapshot()["scanning"] is False


def test_batch_refresh_early_stops_when_account_unavailable(tmp_path) -> None:
    """S7: an unusable account fact ends the round before any batch read."""
    now = datetime(2026, 9, 19, 11, tzinfo=UTC)
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
        PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now
    )
    assert lp.refresh_price_history()["state"] == "known"
    healthy = lp.refresh_candidates(force=True)
    assert [row["market_id"] for row in healthy["candidates"]] == [
        "market-N01", "market-N02", "market-N03",
    ]
    reads_after_healthy = len(exchange.book_token_reads)

    # The account read fails: the round must not consume any queue batch.
    exchange.account_mode = "failure"
    failed = lp.refresh_candidates(force=True)

    assert failed["state"] == "stale"
    assert failed["funnel"]["stop_reason"] == "account_unavailable"
    assert failed["funnel"]["checked"] == 0
    assert failed["funnel"]["passed"] == 0
    assert failed["funnel"]["batches"] == 0
    assert len(exchange.book_token_reads) == reads_after_healthy
    assert exchange.competition_reads == 1  # no re-read either
    # The previous round's rows are kept for read-only display.
    assert [row["market_id"] for row in failed["candidates"]] == [
        "market-N01", "market-N02", "market-N03",
    ]

    # A forced refresh with the account back recovers immediately.
    exchange.account_mode = "valid"
    exchange.now = now
    recovered = lp.refresh_candidates(force=True)
    assert recovered["state"] == "ready"
    # Only three queue markets exist, so the recovered round ends exhausted,
    # not filled.
    assert recovered["funnel"]["stop_reason"] == "queue_exhausted"
    assert recovered["funnel"]["checked"] == 3
    assert recovered["funnel"]["passed"] == 3
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
    assert no_direction["recommendations"] == []
    # Both batch markets lost their books: no passer is published and the
    # scan funnel reports them as unknown with book reasons.
    assert no_direction["selected_results"] == []
    assert no_direction["candidates"] == []
    assert no_direction["funnel"]["checked"] == 2
    assert no_direction["funnel"]["unknown"] == 2
    assert no_direction["funnel"]["stop_reason"] == "queue_exhausted"
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
    assert len(exchange.book_token_reads) == 1
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
    assert len(exchange.book_token_reads) == 1
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
    assert reversed_snapshot["recommendations"] == []
    assert reversed_snapshot["selected_results"][0]["state"] == "rejected"
    assert reversed_snapshot["selected_results"][0]["directions"]["YES"][
        "reason_codes"
    ]
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
    ) == tuple(value + 1 for value in initial_reader_counts)
    assert len(exchange.book_token_reads) == 2
    first_maintenance_counts = (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    )
    retry_same_round = lp.refresh_candidate_recommendations()
    assert retry_same_round["recommendations"] == []
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    ) == first_maintenance_counts

    # A failed refresh suppresses retries for its 60-second backoff window
    # (issue #146). Once the window elapses the retry runs and fails again
    # while the account stays insufficient.
    current["now"] = now + timedelta(seconds=120, microseconds=2)
    exchange.now = current["now"]
    no_retry = lp.refresh_candidate_recommendations()
    assert no_retry["recommendations"] == []
    assert no_retry["maintenance_consecutive_failures"] == 2
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    ) == tuple(value + 1 for value in first_maintenance_counts)

    # A new normal scan starts a new round and can restore the candidate.
    exchange.account_mode = "valid"
    exchange.phase = "initial"
    assert lp.refresh_price_history()["state"] == "known"
    recovered_round = lp.refresh_candidates(force=True)
    assert recovered_round["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    assert len(exchange.book_token_reads) == 4

    # With the whole fact bundle expired, all selected readers run once and
    # the actual returned timestamps permit the reversed direction.
    current["now"] = now + timedelta(seconds=180, microseconds=3)
    exchange.now = current["now"]
    exchange.phase = "reverse"
    refreshed = lp.refresh_candidate_recommendations()
    assert refreshed["recommendations"][0]["selected_direction"]["outcome"] == "YES"
    assert refreshed["recommendations"][0]["realtime_checked_at"] == current["now"].isoformat().replace("+00:00", "Z")
    assert len(exchange.book_token_reads) == 5
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
    assert len(exchange.book_token_reads) == 6

    current["now"] = now + timedelta(seconds=300, microseconds=3)
    exchange.now = current["now"]
    exchange.phase = "reverse"
    exchange.reward_mode = "failure"
    failed_reward = lp.refresh_candidate_recommendations()
    assert failed_reward["recommendations"] == []
    assert failed_reward["selected_results"][0]["directions"]["YES"]["state"] == "unknown"
    assert "reward" in " ".join(
        failed_reward["selected_results"][0]["directions"]["YES"]["reason_codes"]
    )
    assert len(exchange.book_token_reads) == 7
    failed_reward_counts = (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    )
    current["now"] = now + timedelta(seconds=360, microseconds=3)
    exchange.now = current["now"]
    # The 60-second backoff has elapsed, so the retry runs and fails again.
    assert lp.refresh_candidate_recommendations()["recommendations"] == []
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    ) == tuple(value + 1 for value in failed_reward_counts)

    current["now"] = now + timedelta(seconds=360, microseconds=3)
    exchange.now = current["now"]
    exchange.reward_mode = "valid"
    exchange.phase = "initial"
    assert lp.refresh_price_history()["state"] == "known"
    assert lp.refresh_candidates(force=True)["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    assert len(exchange.book_token_reads) == 9

    current["now"] = now + timedelta(seconds=420, microseconds=4)
    exchange.now = current["now"]
    exchange.phase = "reverse"
    exchange.metadata_mode = "failure"
    failed_metadata = lp.refresh_candidate_recommendations()
    assert failed_metadata["recommendations"] == []
    assert failed_metadata["selected_results"][0]["directions"]["YES"]["state"] == "unknown"
    assert "market" in " ".join(
        failed_metadata["selected_results"][0]["directions"]["YES"]["reason_codes"]
    )
    assert len(exchange.book_token_reads) == 10
    failed_metadata_counts = (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    )
    current["now"] = now + timedelta(seconds=480, microseconds=4)
    exchange.now = current["now"]
    # The 60-second backoff has elapsed, so the retry runs and fails again.
    assert lp.refresh_candidate_recommendations()["recommendations"] == []
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
    assert len(exchange.book_token_reads) == 12

    current["now"] = now + timedelta(seconds=540, microseconds=5)
    exchange.now = current["now"]
    exchange.phase = "failure"
    failed_books = lp.refresh_candidate_recommendations()
    assert failed_books["recommendations"] == []
    assert failed_books["selected_results"][0]["directions"]["YES"]["state"] == "unknown"
    assert failed_books["selected_results"][0]["directions"]["YES"][
        "reason_codes"
    ] == ["book_unknown"]
    assert len(exchange.book_token_reads) == 13
    failed_books_counts = (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    )
    current["now"] = now + timedelta(seconds=600, microseconds=5)
    exchange.now = current["now"]
    # The 60-second backoff has elapsed, so the retry runs and fails again.
    assert lp.refresh_candidate_recommendations()["recommendations"] == []
    assert (
        exchange.account_reads,
        exchange.reward_reads,
        exchange.metadata_reads,
        len(exchange.book_token_reads),
    ) == tuple(value + 1 for value in failed_books_counts)

    current["now"] = now + timedelta(seconds=600, microseconds=6)
    exchange.now = current["now"]
    exchange.phase = "initial"
    assert lp.refresh_price_history()["state"] == "known"
    recovered = lp.refresh_candidates(force=True)
    assert recovered["recommendations"][0]["selected_direction"]["outcome"] == "NO"
    assert len(exchange.book_token_reads) == 15

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
    assert inverse_refreshed["recommendations"] == []
    for outcome in ("YES", "NO"):
        assert inverse_refreshed["selected_results"][0]["directions"][outcome][
            "reason_codes"
        ] == ["reward_distance_invalid"]
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

    snapshot = lp.refresh_candidates(force=True)

    funnel = snapshot["funnel"]
    assert funnel["checked"] == 11
    assert funnel["passed"] == 11
    assert funnel["stop_reason"] == "queue_exhausted"
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
    pools = {f"M{index:02d}": Decimal(index) for index in range(1, 61)}
    exchange = _LPYieldBooksExchange(now, pools)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    assert lp.refresh_price_history()["state"] == "known"

    snapshot = lp.refresh_candidates(force=True)

    funnel = snapshot["funnel"]
    assert funnel["checked"] == 50
    assert funnel["passed"] == 50
    assert funnel["stop_reason"] == "checked_limit"
    assert funnel["gap_reason"] is None
    assert funnel["unchecked"] == 10
    assert funnel["batches"] == 5
    assert len(exchange.book_token_reads) == 5
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
    assert [row["market_id"] for row in rows] == [
        "market-M02", "market-M01", "market-M03",
    ]
    stale_row = rows[2]
    assert stale_row["market_id"] == "market-M03"
    assert stale_row["estimate_updated"] is False
    assert Decimal(str(stale_row["estimated_yield_pct_per_hour"])) == Decimal("2.450980")
    assert Decimal(str(stale_row["estimated_target_capital_usd"])) == Decimal("6.80")
    assert failed_row_round["recommendations"][0]["market_id"] == "market-M02"

    # A whole-round book failure degrades every row: values frozen, original
    # relative order preserved, and no current recommendation.
    exchange.fail_book_reads = True
    current["now"] = current["now"] + timedelta(seconds=65)
    exchange.now = current["now"]
    degraded_round = lp.refresh_candidate_recommendations()

    assert degraded_round["recommendations"] == []
    degraded_rows = degraded_round["candidates"]
    assert [row["market_id"] for row in degraded_rows] == [
        "market-M02", "market-M01", "market-M03",
    ]
    assert all(row["estimate_updated"] is False for row in degraded_rows)
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
