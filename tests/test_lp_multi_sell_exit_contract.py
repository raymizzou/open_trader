"""Multi-old-SELL exit contracts for unified registration (#207).

The SDK account/public boundaries are stateful and offline, but dashboard
refresh, registration, LP reconciliation, cancellation, and protected exit all
run through the real production services.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from polymarket.models.clob.account import OpenOrder
from polymarket.models.clob.order_book import OrderBook, OrderBookLevel

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

import tests.test_lp_order_registration_contract as registration_contract
from tests.test_lp_order_registration_contract import (
    _SDKPublicClient,
    _maker_order,
    _open_order,
    _runtime,
    _trade,
)

CONDITION_ID = "0x" + "c" * 64
TOKEN_ID = "0x" + "1" * 64
WALLET = "0x" + "a" * 40


class _ExitAccount(registration_contract._SDKAccountClient):
    """SDK account with exact-ID receipts and a batch cancellation boundary."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.orders = list(self.orders)
        self.sell_states = {"S1": "LIVE", "S2": "LIVE"}
        self.partial_first_cancel = False
        self.cancel_calls: list[tuple[str, ...]] = []

    def get_order(self, *, order_id: str) -> OpenOrder:
        if order_id == "F":
            return _open_order(
                "F",
                "BUY",
                price="0.40",
                original="15",
                matched="15",
                status="FILLED",
            )
        if order_id in self.sell_states:
            return _open_order(
                order_id,
                "SELL",
                price="0.45",
                original="5",
                status=self.sell_states[order_id],
            )
        raise KeyError(order_id)

    def cancel_orders(self, *, order_ids) -> dict[str, object]:
        requested = tuple(str(order_id) for order_id in order_ids)
        assert requested
        assert set(requested) <= {"S1", "S2"}
        self.cancel_calls.append(requested)
        if self.partial_first_cancel:
            canceled = [order_id for order_id in requested if order_id == "S1"]
            not_canceled = {
                order_id: "timeout"
                for order_id in requested
                if order_id == "S2"
            }
        else:
            canceled = list(requested)
            not_canceled = {}
        return {"canceled": canceled, "not_canceled": not_canceled}

    def mark_old_sells_canceled(self) -> None:
        """Apply the venue transition outside the production under test."""

        self.sell_states["S1"] = "CANCELED"
        self.sell_states["S2"] = "CANCELED"
        self.orders = [
            order for order in self.orders if str(getattr(order, "id", "")) not in {"S1", "S2"}
        ]


class _ExitBookClient(_SDKPublicClient):
    """One-price public book used by the real protected-exit valuation."""

    def __init__(self, now: datetime, *, bid_price: str) -> None:
        super().__init__(now)
        self.bid_price = Decimal(bid_price)

    def get_order_book(self, *, token_id: str) -> OrderBook:
        assert token_id == TOKEN_ID
        return OrderBook(
            market=CONDITION_ID,
            asset_id=token_id,
            timestamp=datetime.now(UTC),
            bids=(
                OrderBookLevel(price=self.bid_price, size=Decimal("100")),
            ),
            asks=(OrderBookLevel(price=Decimal("0.41"), size=Decimal("100")),),
            min_order_size=Decimal("1"),
            tick_size=Decimal("0.01"),
            neg_risk=False,
            hash=f"exit-book-{self.bid_price}",
        )

    def get_order_books(self, *, token_ids) -> tuple[OrderBook, ...]:
        assert tuple(token_ids) == (TOKEN_ID,)
        return (self.get_order_book(token_id=TOKEN_ID),)


def _inventory_fixtures():
    sells = (
        _open_order("S1", "SELL", price="0.45", original="5"),
        _open_order("S2", "SELL", price="0.45", original="5"),
    )
    filled_buy = _open_order(
        "F", "BUY", price="0.40", original="15", matched="15", status="FILLED"
    )
    fill = _maker_order("F", "BUY", "15", "0.40")
    return sells, filled_buy, (_trade("fill-F", fill, size="15"),)


def _warm_public_book(adapter) -> None:
    """Complete the same single-flight public read that the next tick consumes."""

    key = f"{CONDITION_ID}\0{TOKEN_ID}"
    adapter._read_lp_public_snapshot(key, "market-1", TOKEN_ID, wait=False)
    future = adapter._lp_public_reads.get(key)
    if future is not None:
        future.result(timeout=10)


def _fresh_round(
    store: PredictionArbitrageStore, adapter, execution
) -> dict[str, object]:
    store.lp_advance_trade_generation(store.lp_trade_generation())
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    _warm_public_book(adapter)
    return execution.lp_tick()


def test_stop_loss_batches_old_sell_cancel_then_one_protected_exit(
    tmp_path, monkeypatch
) -> None:
    """Exact terminal old SELLs gate the single protected FOK replacement."""

    sells, filled_buy, trades = _inventory_fixtures()
    positions = (
        {
            "condition_id": CONDITION_ID,
            "token_id": TOKEN_ID,
            "outcome": "YES",
            "size": Decimal("15"),
            "average_price": Decimal("0.40"),
        },
    )
    monkeypatch.setattr(
        registration_contract, "_SDKAccountClient", _ExitAccount
    )
    store, adapter, account, _, execution = _runtime(
        tmp_path, orders=(*sells, filled_buy), trades=trades, positions=positions
    )
    account.partial_first_cancel = True
    public = _ExitBookClient(datetime.now(UTC), bid_price="0.01")
    adapter._public_client_factory = lambda: public

    try:
        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        sessions = store.lp_sessions()
        assert len(sessions) == 1
        session_id = str(sessions[0]["session_id"])
        assert set(sessions[0]["owned_order_ids"]) == {"F", "S1", "S2"}

        _warm_public_book(adapter)
        first = execution.lp_tick()
        assert first["sessions"]
        session = store.lp_session(session_id)
        assert session["state"] == "needs_attention"
        assert session["stop_loss_latched"] is True
        assert session["reconciliation"] == "passive_cancel_RuntimeError"
        assert Decimal(str(session["buy_filled_quantity"])) == Decimal("15")
        assert Decimal(str(session["buy_cost"])) == Decimal("6")
        assert Decimal(str(session["residual_quantity"])) == Decimal("15")
        assert Decimal(str(session["residual_exit_value"])) == Decimal("0.15")
        opening_loss = session.get("opening_loss")
        assert opening_loss is not None and Decimal(str(opening_loss)) > Decimal("5")

        canceled_ids = [
            order_id
            for requested in account.cancel_calls
            for order_id in requested
        ]
        assert set(canceled_ids) == {"S1", "S2"}
        assert len(canceled_ids) == 2
        assert account.market_orders == []
        assert account.posts == []

        account.mark_old_sells_canceled()
        _fresh_round(store, adapter, execution)

        assert len(account.market_orders) == 1
        assert len(account.posts) == 1
        signed = account.market_orders[0]
        assert signed["token_id"] == TOKEN_ID
        assert signed["side"] == "SELL"
        assert signed["order_type"] == "FOK"
        assert Decimal(str(signed["shares"])) == Decimal("15")
        assert Decimal(str(signed["min_price"])) > 0
        session = store.lp_session(session_id)
        assert session["protected_exit_order_id"] == "new-sell-1"
        canceled_ids = [
            order_id
            for requested in account.cancel_calls
            for order_id in requested
        ]
        assert set(canceled_ids) == {"S1", "S2"}
        assert len(canceled_ids) == 2

        posts_before = len(account.posts)
        market_orders_before = len(account.market_orders)
        _fresh_round(store, adapter, execution)
        assert len(account.posts) == posts_before
        assert len(account.market_orders) == market_orders_before
    finally:
        adapter.close()


def test_manual_stop_requests_both_old_sells_and_waits_for_receipts(
    tmp_path, monkeypatch
) -> None:
    """lp_stop cancels every old SELL before any replacement can be posted."""

    sells, filled_buy, trades = _inventory_fixtures()
    positions = (
        {
            "condition_id": CONDITION_ID,
            "token_id": TOKEN_ID,
            "outcome": "YES",
            "size": Decimal("15"),
            "average_price": Decimal("0.40"),
        },
    )
    monkeypatch.setattr(
        registration_contract, "_SDKAccountClient", _ExitAccount
    )
    store, adapter, account, _, execution = _runtime(
        tmp_path, orders=(*sells, filled_buy), trades=trades, positions=positions
    )
    account.partial_first_cancel = True
    public = _ExitBookClient(datetime.now(UTC), bid_price="0.40")
    adapter._public_client_factory = lambda: public

    try:
        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        sessions = store.lp_sessions()
        assert len(sessions) == 1
        session_id = str(sessions[0]["session_id"])
        assert set(sessions[0]["owned_order_ids"]) == {"F", "S1", "S2"}

        stopped = execution.lp_stop(session_id)
        assert stopped["state"] == "needs_attention"
        session = store.lp_session(session_id)
        assert session["stop_requested"] is True
        assert session["resume_state"] == "review"
        assert session["reconciliation"] == "stop_cancel_RuntimeError"

        canceled_ids = [
            order_id
            for requested in account.cancel_calls
            for order_id in requested
        ]
        assert set(canceled_ids) == {"S1", "S2"}
        assert len(canceled_ids) == 2
        assert account.sell_states == {"S1": "LIVE", "S2": "LIVE"}
        assert account.market_orders == []
        assert account.posts == []
    finally:
        adapter.close()
