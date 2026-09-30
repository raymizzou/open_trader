"""End-to-end contracts for exchange-ID registration (#207).

These tests keep the SDK boundary realistic while remaining offline: SDK model
objects feed the real trading adapter, and persistence goes through the real
store/execution entry points.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
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
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


WALLET = "0x" + "a" * 40
CONDITION_ID = "0x" + "c" * 64
TOKEN_ID = "0x" + "1" * 64
NOW = datetime.now(UTC).replace(microsecond=0)


class _Notifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> bool:
        self.calls.append((title, message))
        return True


class _SDKAccountClient:
    def __init__(
        self,
        now: datetime,
        *,
        orders: tuple[object, ...] = (),
        trades: tuple[object, ...] = (),
        positions: tuple[object, ...] = (),
        before_positions=None,
    ) -> None:
        self.now = now
        self.environment = SimpleNamespace(standard_exchange=WALLET)
        self.orders = orders
        self.trades = trades
        self.positions = positions
        self.before_positions = before_positions
        self.balance_reads = 0
        self.order_reads = 0
        self.trade_reads = 0
        self.position_reads = 0
        self.market_orders: list[dict[str, object]] = []
        self.posts: list[object] = []

    @staticmethod
    def close() -> None:
        return None

    def get_balance_allowance(self, **_kwargs: object) -> object:
        self.balance_reads += 1
        return SimpleNamespace(
            balance="100000000",
            allowances={self.environment.standard_exchange: "100000000"},
        )

    def list_open_orders(self, **_kwargs: object) -> tuple[object, ...]:
        self.order_reads += 1
        return self.orders

    def list_account_trades(self, **_kwargs: object) -> tuple[object, ...]:
        self.trade_reads += 1
        return self.trades

    def list_positions(self, **_kwargs: object) -> tuple[object, ...]:
        self.position_reads += 1
        if self.before_positions is not None:
            self.before_positions(self.position_reads)
        return self.positions

    def create_market_order(self, **kwargs: object) -> dict[str, object]:
        signed = {**kwargs, "order_type": "FOK"}
        self.market_orders.append(signed)
        return signed

    def post_order(self, signed: object) -> object:
        self.posts.append(signed)
        return {
            **signed,
            "order_id": f"new-sell-{len(self.posts)}",
            "status": "FILLED",
            "accepted": True,
        }

    def get_order_scoring(self, *, order_id: str) -> bool:
        del order_id
        return True


class _SDKPublicClient:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def get_market(self, *, id: str) -> Market:
        assert id == "market-1"
        return Market(
            id="market-1",
            condition_id=CONDITION_ID,
            state=MarketState(
                active=True,
                closed=False,
                acceptingOrders=True,
                enableOrderBook=True,
            ),
            outcomes=MarketOutcomes(
                yes=MarketOutcome(label="Yes", tokenId=TOKEN_ID),
                no=MarketOutcome(label="No", tokenId="0x" + "2" * 64),
            ),
            metrics=MarketMetrics(),
            prices=MarketPrices(),
            trading=MarketTrading(
                minimumOrderSize=Decimal("1"),
                minimumTickSize=Decimal("0.01"),
                feesEnabled=True,
                feeSchedule=FeeSchedule(
                    exponent=1,
                    rate=Decimal("0"),
                    takerOnly=True,
                    rebateRate=Decimal("0"),
                ),
            ),
            resolution=MarketResolution(source="UMA"),
            rewards=MarketRewards(rewardsMinSize=Decimal("1"), rewardsMaxSpread=10),
            sports=MarketSportsMetadata(),
            events=(),
            tags=(),
        )

    def get_order_book(self, *, token_id: str) -> OrderBook:
        assert token_id == TOKEN_ID
        return OrderBook(
            market=CONDITION_ID,
            asset_id=token_id,
            timestamp=self.now,
            bids=(OrderBookLevel(price=Decimal("0.39"), size=Decimal("100")),),
            asks=(OrderBookLevel(price=Decimal("0.41"), size=Decimal("100")),),
            min_order_size=Decimal("1"),
            tick_size=Decimal("0.01"),
            neg_risk=False,
            hash="book-hash",
        )

    def get_order_books(self, *, token_ids: tuple[str, ...]) -> tuple[OrderBook, ...]:
        return tuple(self.get_order_book(token_id=token_id) for token_id in token_ids)

    def list_markets(self, **_kwargs: object) -> list[Market]:
        return [self.get_market(id="market-1")]

    def get_order_scoring(self, *, order_id: str) -> bool:
        del order_id
        return True


def _maker_order(order_id: str, side: str, matched: str, price: str) -> MakerOrder:
    return MakerOrder(
        order_id=order_id,
        asset_id=TOKEN_ID,
        maker_address=WALLET,
        owner=WALLET,
        side=side,
        price=Decimal(price),
        matched_amount=Decimal(matched),
        outcome="YES",
        fee_rate_bps=Decimal("0"),
    )


def _trade(
    trade_id: str,
    maker_order: MakerOrder,
    *,
    size: str,
    status: str = "CONFIRMED",
) -> ClobTrade:
    return ClobTrade(
        id=trade_id,
        market=CONDITION_ID,
        asset_id=TOKEN_ID,
        owner=WALLET,
        maker_address=WALLET,
        taker_order_id=f"taker-{trade_id}",
        side=maker_order.side,
        trader_side="MAKER",
        price=maker_order.price,
        size=Decimal(size),
        outcome="YES",
        status=status,
        fee_rate_bps=Decimal("0"),
        bucket_index=0,
        transaction_hash=f"0x{trade_id}",
        maker_orders=(maker_order,),
        match_time=NOW,
        last_update=NOW,
    )


def _open_order(
    order_id: str,
    side: str,
    *,
    price: str,
    original: str,
    matched: str = "0",
    status: str = "LIVE",
    order_type: str = "GTC",
    expiration: int | None = None,
) -> OpenOrder:
    return OpenOrder(
        id=order_id,
        market=CONDITION_ID,
        asset_id=TOKEN_ID,
        owner=WALLET,
        maker_address=WALLET,
        side=side,
        price=Decimal(price),
        original_size=Decimal(original),
        size_matched=Decimal(matched),
        outcome="YES",
        order_type=order_type,
        expiration=expiration,
        status=status,
        associate_trades=(),
        created_at=NOW,
    )


def _adapter(
    tmp_path,
    *,
    orders=(),
    trades=(),
    positions=(),
    before_positions=None,
):
    account = _SDKAccountClient(
        NOW,
        orders=orders,
        trades=trades,
        positions=positions,
        before_positions=before_positions,
    )
    public = _SDKPublicClient(NOW)
    adapter = PolymarketTradingClient(
        TradingConfig(WALLET, WALLET),
        account,
        public_client_factory=lambda: public,
    )
    return adapter, account


def _runtime(tmp_path, **adapter_kwargs):
    store = PredictionArbitrageStore(tmp_path / "state.sqlite")
    adapter, account = _adapter(tmp_path, **adapter_kwargs)
    notifier = _Notifier()
    lp = PolymarketLPService(store, adapter, clock=lambda: datetime.now(UTC))
    lp.set_protection_notifier(lambda title, message, xiaoai: notifier.notify(title, message))
    execution = PredictionExecutionService(
        store=store,
        monitor=SimpleNamespace(),
        trading=adapter,
        notifier=notifier,
        lock_path=tmp_path / "execution.lock",
        lp=lp,
    )
    execution._breaker_open = False
    return store, adapter, account, lp, execution


def test_newly_managed_orders_read_their_own_scoring_before_first_tick(tmp_path):
    store, adapter, account, lp, execution = _runtime(tmp_path, orders=(
        _open_order("scoring-a", "BUY", price="0.40", original="10"),
        _open_order("scoring-b", "BUY", price="0.40", original="10"),
    ))
    reads = []

    def scoring(*, order_id):
        reads.append(order_id)
        return order_id == "scoring-a"

    account.get_order_scoring = scoring
    try:
        dashboard = execution.refresh_lp_dashboard_snapshot()
        assert dashboard["state"] == "ready"
        assert len(store.lp_active_sessions()) == 1
        assert {row["management"] for row in dashboard["orders"]} == {"system_managed"}
        assert {row["order_id"]: row["scoring_status"] for row in dashboard["orders"]} == {
            "scoring-a": "true", "scoring-b": "false",
        }
        assert sorted(reads) == ["scoring-a", "scoring-b"]
        assert account.posts == []
    finally:
        adapter.close()


def test_shared_snapshot_cache_follows_trade_generation(tmp_path) -> None:
    """One generation reuses endpoint reads; a real generation forces a new one."""

    store, adapter, account, _, _ = _runtime(tmp_path)
    provider = lambda: store.lp_trade_generation()
    initial_generation = provider()
    try:
        first = adapter.lp_account_snapshot_shared(
            max_age_seconds=10, trade_generation_provider=provider
        )
        assert first["wallet_address"] == WALLET
        assert first["account_id"] == WALLET
        assert first["read_started_at"] <= first["checked_at"] <= first["read_ended_at"]
        assert all(
            first[name] is True
            for name in (
                "balance_complete",
                "open_orders_complete",
                "trades_complete",
                "positions_complete",
                "pagination_complete",
            )
        )
        assert first["trade_generation"] == initial_generation
        endpoint_counts = (
            account.balance_reads,
            account.order_reads,
            account.trade_reads,
            account.position_reads,
        )
        started, ended = first["read_started_at"], first["read_ended_at"]

        second = adapter.lp_account_snapshot_shared(
            max_age_seconds=10, trade_generation_provider=provider
        )
        assert second == first
        assert (second["read_started_at"], second["read_ended_at"]) == (started, ended)
        assert (
            account.balance_reads,
            account.order_reads,
            account.trade_reads,
            account.position_reads,
        ) == endpoint_counts

        assert store.lp_advance_trade_generation(initial_generation) is True
        third = adapter.lp_account_snapshot_shared(
            max_age_seconds=10, trade_generation_provider=provider
        )
        assert third["trade_generation"] == initial_generation + 1
        assert third["read_started_at"] > ended
        assert (
            account.balance_reads,
            account.order_reads,
            account.trade_reads,
            account.position_reads,
        ) == tuple(value + 1 for value in endpoint_counts)
    finally:
        adapter.close()


def test_dashboard_generation_race_waits_and_next_round_registers(tmp_path) -> None:
    """A generation advance during positions is waiting evidence, not registration."""

    advanced = [False]

    def advance_before_registration(read_number: int) -> None:
        if read_number == 1 and not advanced[0]:
            advanced[0] = True
            assert store.lp_advance_trade_generation(
                store.lp_trade_generation()
            ) is True

    store, adapter, account, _, execution = _runtime(
        tmp_path,
        orders=(_open_order("A", "BUY", price="0.40", original="20"),),
        before_positions=advance_before_registration,
    )
    try:
        racing = execution.refresh_lp_dashboard_snapshot()
        assert racing["state"] == "waiting"
        assert advanced[0] is True
        assert account.position_reads == 1
        assert store.lp_sessions() == []
        assert execution.lp_dashboard()["orders"] == []

        again = execution.refresh_lp_dashboard_snapshot()
        assert again["state"] == "ready"
        assert account.position_reads == 2
        sessions = store.lp_sessions()
        assert len(sessions) == 1
        assert sessions[0]["owned_order_ids"] == ["A"]
    finally:
        adapter.close()


def test_invalid_round_preserves_last_dashboard_orders(tmp_path) -> None:
    store, adapter, account, _, execution = _runtime(
        tmp_path,
        orders=(_open_order("A", "BUY", price="0.40", original="20"),),
    )
    try:
        previous = execution.refresh_lp_dashboard_snapshot()
        assert previous["state"] == "ready"
        previous_sessions = store.lp_sessions()
        assert store.lp_advance_trade_generation(store.lp_trade_generation())

        def invalidate_during_positions(_read_number):
            assert store.lp_advance_trade_generation(store.lp_trade_generation())

        account.before_positions = invalidate_during_positions
        waiting = execution.refresh_lp_dashboard_snapshot()
        assert waiting["state"] == "waiting"
        assert waiting["stale"] is True
        assert waiting["orders"] == previous["orders"]
        assert waiting["checked_at"] == previous["checked_at"]
        assert execution.lp_dashboard()["state"] == "waiting"
        assert store.lp_sessions() == previous_sessions
        assert account.posts == []
    finally:
        adapter.close()


def test_display_only_dashboard_reads_without_registering_orders(tmp_path) -> None:
    store, adapter, account, _, execution = _runtime(
        tmp_path,
        orders=(_open_order("A", "BUY", price="0.40", original="20"),),
    )
    execution._display_only = True
    try:
        result = execution.refresh_lp_dashboard_snapshot()
        assert result["state"] == "ready"
        assert {row["order_id"] for row in result["orders"]} == {"A"}
        assert account.order_reads == 1
        assert store.lp_sessions() == []
        assert account.posts == []
    finally:
        adapter.close()


def test_incomplete_or_foreign_snapshots_cannot_replace_registration(tmp_path) -> None:
    """Strict identity/freshness/completeness facts are a registration gate."""

    store, adapter, _, lp, _ = _runtime(
        tmp_path,
        orders=(_open_order("A", "BUY", price="0.40", original="20"),),
    )
    try:
        valid = adapter.lp_account_snapshot_shared(
            max_age_seconds=0, trade_generation_provider=store.lp_trade_generation
        )
        result = lp.register_account_snapshot(valid)
        assert result["state"] == "registered", result
        sessions = store.lp_sessions()
        assert len(sessions) == 1
        assert sessions[0]["owned_order_ids"] == ["A"]
        baseline = sessions[0]["queue_protection"]

        invalid = [
            {**valid, "pagination_complete": False},
            {**valid, "checked_at": NOW - timedelta(seconds=61)},
            {**valid, "read_ended_at": NOW - timedelta(seconds=61)},
            {**valid, "wallet_address": "0x" + "b" * 40},
            {**valid, "account_id": "0x" + "b" * 40},
            {**valid, "open_orders_complete": "True"},
            {**valid, "positions_complete": "True"},
        ]
        for snapshot in invalid:
            result = lp.register_account_snapshot(snapshot)
            assert result["state"] == "skipped", result
            assert result["reason"], result
        sessions = store.lp_sessions()
        assert len(sessions) == 1
        assert sessions[0]["owned_order_ids"] == ["A"]
        assert sessions[0]["queue_protection"] == baseline
    finally:
        adapter.close()


def test_concurrent_discovery_keeps_distinct_ids_and_replays_once(tmp_path) -> None:
    """A/B/C are one durable group; parallel replay cannot duplicate identity."""

    store = PredictionArbitrageStore(tmp_path / "state.sqlite")
    store.lp_create_session(
        "group-a",
        "group-a",
        state="entry_open",
        payload={
            "account_id": WALLET,
            "market_id": "market-1",
            "condition_id": CONDITION_ID,
            "token_id": TOKEN_ID,
            "outcome": "YES",
            "price": Decimal("0.40"),
            "quantity": Decimal("20"),
            "entry_order_id": "A",
            "owned_order_ids": ["A"],
            "order_history": {"A": {"order_id": "A", "side": "BUY", "status": "LIVE"}},
        },
    )
    orders_b = {"order_id": "B", "token_id": TOKEN_ID, "side": "BUY", "status": "LIVE", "price": "0.40",
                "original_size": "20", "size_matched": "0"}
    orders_c = {"order_id": "C", "token_id": TOKEN_ID, "side": "BUY", "status": "LIVE", "price": "0.40",
                "original_size": "20", "size_matched": "0"}
    barrier = Barrier(2)

    def register(payload: dict[str, object]) -> None:
        barrier.wait(timeout=30)
        store.lp_register_exchange_orders(
            WALLET,
            TOKEN_ID,
            [payload],
            session=store.lp_session("group-a"),
            expected_generation=store.lp_trade_generation(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(register, (orders_b, orders_c)))

    sessions = store.lp_sessions()
    assert len(sessions) == 1
    assert set(sessions[0]["owned_order_ids"]) == {"A", "B", "C"}
    before = store.lp_session("group-a")

    restarted_store = PredictionArbitrageStore(tmp_path / "state.sqlite")
    restarted_store.lp_register_exchange_orders(
        WALLET,
        TOKEN_ID,
        [orders_b, orders_c],
        session=restarted_store.lp_session("group-a"),
        expected_generation=restarted_store.lp_trade_generation(),
    )
    after = restarted_store.lp_session("group-a")
    assert set(after["owned_order_ids"]) == set(before["owned_order_ids"])
    assert after["order_history"] == before["order_history"]

def test_terminal_history_and_late_direct_receipt_stay_one_group(tmp_path) -> None:
    """Completed D stays historical; E forms the only new active group."""

    store = PredictionArbitrageStore(tmp_path / "state.sqlite")
    terminal = {
        "order_id": "D", "token_id": TOKEN_ID, "side": "BUY", "status": "MATCHED", "price": "0.40",
        "original_size": "20", "size_matched": "20",
    }
    live = {
        "order_id": "E", "token_id": TOKEN_ID, "side": "BUY", "status": "LIVE", "price": "0.40",
        "original_size": "10", "size_matched": "0",
    }
    old_group = {
        "session_id": "history-group",
        "account_id": WALLET,
        "market_id": "market-1",
        "condition_id": CONDITION_ID,
        "token_id": TOKEN_ID,
        "outcome": "YES",
        "state": "complete",
        "owned_order_ids": ["D"],
        "order_history": {"D": {**terminal, "role": "BUY"}},
        "group_buy_quantity": Decimal("20"),
    }
    store.lp_create_session(
        "history-group",
        "history-group",
        state="complete",
        payload=old_group,
    )
    new_candidate = {
        "session_id": "new-active",
        "account_id": WALLET,
        "market_id": "market-1",
        "condition_id": CONDITION_ID,
        "token_id": TOKEN_ID,
        "outcome": "YES",
        "owned_order_ids": ["E"],
        "order_history": {"E": {**live, "role": "BUY"}},
        "group_buy_quantity": Decimal("10"),
    }

    def assert_groups() -> None:
        old = store.lp_session("history-group")
        new = store.lp_session("new-active")
        assert old["state"] == "complete"
        assert old["owned_order_ids"] == ["D"]
        assert old["order_history"]["D"]["order_id"] == "D"
        assert Decimal(str(old["group_buy_quantity"])) == Decimal("20")
        assert new["state"] != "complete"
        assert new["owned_order_ids"] == ["E"]
        assert Decimal(str(new["group_buy_quantity"])) == Decimal("10")
        assert len(store.lp_sessions()) == 2

    store.lp_register_exchange_orders(
        WALLET, TOKEN_ID, [terminal, live], session=new_candidate,
        expected_generation=store.lp_trade_generation(),
    )
    assert_groups()

    store.lp_register_exchange_orders(
        WALLET, TOKEN_ID, [terminal, live],
        session=store.lp_session("new-active"),
        expected_generation=store.lp_trade_generation(),
    )
    store.lp_register_exchange_orders(
        WALLET, TOKEN_ID, [live],
        session=store.lp_session("new-active"),
        expected_generation=store.lp_trade_generation(),
    )
    assert_groups()

    restarted_store = PredictionArbitrageStore(tmp_path / "state.sqlite")
    store = restarted_store
    store.lp_register_exchange_orders(
        WALLET, TOKEN_ID, [terminal, live],
        session=store.lp_session("new-active"),
        expected_generation=store.lp_trade_generation(),
    )
    assert_groups()

def test_late_direct_receipt_returns_exact_terminal_owner(tmp_path) -> None:
    """D's direct receipt returns D even when E is the token's active group."""

    store = PredictionArbitrageStore(tmp_path / "state.sqlite")
    terminal = {
        "order_id": "D", "token_id": TOKEN_ID, "side": "BUY", "status": "MATCHED",
        "price": "0.40", "original_size": "20", "size_matched": "20",
    }
    live = {
        "order_id": "E", "token_id": TOKEN_ID, "side": "BUY", "status": "LIVE",
        "price": "0.40", "original_size": "10", "size_matched": "0",
    }
    store.lp_create_session(
        "history-group", "history-group", state="complete",
        payload={
            "account_id": WALLET, "token_id": TOKEN_ID,
            "condition_id": CONDITION_ID, "owned_order_ids": ["D"],
            "order_history": {"D": terminal}, "group_buy_quantity": "20",
        },
    )
    store.lp_create_session(
        "active-group", "active-group", state="entry_open",
        payload={
            "account_id": WALLET, "token_id": TOKEN_ID,
            "condition_id": CONDITION_ID, "owned_order_ids": ["E"],
            "order_history": {"E": live}, "group_buy_quantity": "10",
        },
    )
    before_history = store.lp_session("history-group")
    before_active = store.lp_session("active-group")

    receipt = {**terminal, "status": "CANCELED", "size_matched": "0"}
    result = store.lp_register_exchange_orders(
        WALLET, TOKEN_ID, [receipt],
        expected_generation=store.lp_trade_generation(),
    )

    assert result["session"]["session_id"] == "history-group"
    assert result["session"]["order_history"]["D"]["status"] == "MATCHED"
    assert Decimal(str(result["session"]["order_history"]["D"]["size_matched"])) == Decimal("20")
    assert result["session"]["owned_order_ids"] == ["D"]
    assert "D" not in store.lp_session("active-group")["owned_order_ids"]
    assert store.lp_session("history-group")["state"] == before_history["state"] == "complete"
    assert store.lp_session("active-group") == before_active
    assert len(store.lp_sessions()) == 2

def test_existing_inventory_and_sells_do_not_duplicate_exit(tmp_path) -> None:
    """Dashboard registration plus tick leaves two SELLs and no extra sale."""

    buy = _open_order("F", "BUY", price="0.40", original="20", matched="15", status="LIVE")
    sells = (
        _open_order("S1", "SELL", price="0.45", original="5"),
        _open_order("S2", "SELL", price="0.45", original="5"),
    )
    fill = _maker_order("F", "BUY", "15", "0.40")
    trades = (_trade("fill-F", fill, size="15"),)
    positions = (
        {
            "condition_id": CONDITION_ID,
            "token_id": TOKEN_ID,
            "outcome": "YES",
            "size": Decimal("15"),
            "average_price": Decimal("0.40"),
        },
    )
    store, adapter, account, lp, execution = _runtime(
        tmp_path, orders=(*sells, buy), trades=trades, positions=positions
    )

    def registration_facts():
        sessions = store.lp_sessions()
        assert len(sessions) == 1
        history = sessions[0]["order_history"]
        ids = set(sessions[0]["owned_order_ids"])
        buy_ids = {
            str(row["order_id"]) for row in history.values()
            if str(row.get("side") or row.get("role") or "").upper() == "BUY"
        }
        sell_ids = {
            str(row["order_id"]) for row in history.values()
            if str(row.get("side") or row.get("role") or "").upper() == "SELL"
        }
        return ids, buy_ids, sell_ids

    try:
        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        baseline_ids, baseline_buys, baseline_sells = registration_facts()
        assert baseline_ids == {"F", "S1", "S2"}
        assert baseline_buys == {"F"}
        assert baseline_sells == {"S1", "S2"}
        assert execution.lp_tick()["sessions"]
        assert registration_facts() == (
            baseline_ids, baseline_buys, baseline_sells,
        )
        assert account.market_orders == []
        assert account.posts == []

        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        restarted_lp = PolymarketLPService(
            store, adapter, clock=lambda: datetime.now(UTC)
        )
        restarted_execution = PredictionExecutionService(
            store=store,
            monitor=SimpleNamespace(),
            trading=adapter,
            notifier=_Notifier(),
            lock_path=tmp_path / "execution.lock",
            lp=restarted_lp,
        )
        restarted_execution._breaker_open = False
        assert restarted_execution.lp_tick()["sessions"]
        assert registration_facts() == (
            baseline_ids, baseline_buys, baseline_sells,
        )
        assert account.market_orders == []
        assert account.posts == []
    finally:
        adapter.close()
