from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

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

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


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
                    "bids": [{"price": Decimal("0.34"), "size": Decimal("100")}],
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
    assert snapshot["recommendations"] == []
    assert snapshot["selected_market_ids"] == ["market-A"]
    assert exchange.competition_reads == 1


def test_refresh_candidates_drops_rows_whose_realtime_capital_over_available(tmp_path) -> None:
    """Contract: the over-available gate must use realtime ?? reference capital.

    Reference capital 20 × 0.505 = 10.100 fits the 11 available, so the row
    reaches the trial list and its live book is read; the realtime bid 0.60
    lifts capital to 12.00 which exceeds available, so the row must be
    dropped and counted after enrichment.
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
                    "bids": [{"price": Decimal("0.60"), "size": Decimal("100")}],
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
    # The row passed the reference-stage gate and its live book was read.
    assert exchange.book_token_reads == (("token-condition-A",),)
    funnel = snapshot["funnel"]
    # condition-Z exceeds available at the reference stage, condition-A only
    # after the realtime recheck: both count as over_available exclusions.
    assert funnel["excluded"]["over_available"] == 2
    assert funnel["trial"] == 0
    assert snapshot["candidates"] == []
    assert snapshot["selected_market_ids"] == []
    assert funnel["gap_reason"] is not None
    assert "本轮 0 个" in funnel["gap_reason"]
