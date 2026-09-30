"""Imported old-passive replacement preserves the exact GTD lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from polymarket.models.clob.account import OpenOrder
from polymarket.models.clob.order_book import OrderBook, OrderBookLevel

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

import tests.test_lp_order_registration_contract as registration_contract
from tests.test_lp_multi_sell_exit_contract import (
    _ExitAccount,
    _ExitBookClient,
    _maker_order,
    _open_order,
    _runtime,
    _trade,
    _warm_public_book,
)

CONDITION_ID = "0x" + "c" * 64
TOKEN_ID = "0x" + "1" * 64
WALLET = "0x" + "a" * 40
NEW_SELL_ID = "imported-sell-1"


class _ImportedExitAccount(_ExitAccount):
    """SDK account with exact imported receipts and a real limit boundary."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.orders = tuple(self.orders)
        self.sell_states = {"S1": "CANCELED"}
        self.old_receipt: OpenOrder | None = None
        self.limit_orders: list[dict[str, object]] = []

    def get_order(self, *, order_id: str) -> OpenOrder:
        if order_id == "F":
            return _open_order(
                "F", "BUY", price="0.40", original="15", matched="15",
                status="FILLED",
            )
        if order_id == "S1":
            assert self.old_receipt is not None
            return self.old_receipt
        if order_id == NEW_SELL_ID:
            matches = [
                order for order in self.orders
                if str(getattr(order, "id", "")) == NEW_SELL_ID
            ]
            if matches:
                return matches[0]
        raise KeyError(order_id)

    def create_limit_order(self, **kwargs: object) -> dict[str, object]:
        signed = {
            **kwargs,
            "order_type": "GTC" if kwargs.get("expiration") is None else "GTD",
        }
        assert signed["token_id"] == TOKEN_ID
        assert signed["side"] == "SELL"
        assert signed["post_only"] is True
        assert Decimal(str(signed["size"])) > 0
        self.limit_orders.append(signed)
        return signed

    def post_order(self, signed: object) -> dict[str, object]:
        assert signed["order_type"] in {"GTC", "GTD"}
        self.posts.append(signed)
        receipt = _open_order(
            NEW_SELL_ID,
            "SELL",
            price=str(signed["price"]),
            original=str(signed["size"]),
            status="LIVE",
        ).model_copy(
            update={
                "order_type": signed["order_type"],
                "expiration": signed["expiration"],
            }
        )
        self.orders = (*self.orders, receipt)
        return {
            **signed,
            "order_id": NEW_SELL_ID,
            "status": "LIVE",
            "accepted": True,
        }


class _ImportedExitBook(_ExitBookClient):
    """Normal imported-inventory book: executable bid and old quote ask."""

    def get_order_book(self, *, token_id: str) -> OrderBook:
        assert token_id == TOKEN_ID
        return OrderBook(
            market=CONDITION_ID,
            asset_id=token_id,
            timestamp=datetime.now(UTC),
            bids=(OrderBookLevel(price=Decimal("0.40"), size=Decimal("100")),),
            asks=(OrderBookLevel(price=Decimal("0.45"), size=Decimal("100")),),
            min_order_size=Decimal("1"),
            tick_size=Decimal("0.01"),
            neg_risk=False,
            hash="imported-exit-book",
        )


def _fresh_tick(execution, adapter) -> dict[str, object]:
    """Warm the real single-flight book, then consume it in one public tick."""

    _warm_public_book(adapter)
    return execution.lp_tick()


def _old_receipt(
    expiration: int | None,
    *,
    order_type: str,
) -> OpenOrder:
    """Build the exact SDK terminal receipt with its lifecycle facts."""

    return _open_order(
        "S1", "SELL", price="0.45", original="15", status="CANCELED"
    ).model_copy(
        update={"order_type": order_type, "expiration": expiration}
    )


def _assert_retained_old(
    record: dict[str, object], *, order_type: str, expiration: int | None
) -> None:
    """Assert durable old-ID facts without requiring fixture-only equality."""

    assert record["order_id"] == "S1"
    assert record["side"] == "SELL"
    assert record["status"] == "CANCELED"
    assert record["order_type"] == order_type
    assert record["expiration"] == expiration
    assert Decimal(str(record["price"])) == Decimal("0.45")
    assert Decimal(str(record["original_size"])) == Decimal("15")
    assert Decimal(str(record["size_matched"])) == Decimal("0")


def _active_payload(old_receipt: OpenOrder) -> dict[str, object]:
    return {
        "session_id": "imported-a",
        "account_id": WALLET,
        "source": "account_sync",
        "market_id": "market-1",
        "condition_id": CONDITION_ID,
        "token_id": TOKEN_ID,
        "outcome": "YES",
        "price": Decimal("0.40"),
        "quantity": Decimal("15"),
        "group_buy_quantity": Decimal("15"),
        "review_at": None,
        "entry_order_id": "F",
        "passive_exit_order_id": "S1",
        "passive_exit_price": Decimal("0.45"),
        "passive_cancel_requested": False,
        "owned_order_ids": ["F", "S1"],
        "order_history": {
            "F": {
                "order_id": "F",
                "token_id": TOKEN_ID,
                "side": "BUY",
                "status": "FILLED",
                "price": Decimal("0.40"),
                "original_size": Decimal("15"),
                "size_matched": Decimal("15"),
            },
            "S1": {
                "order_id": "S1",
                "token_id": TOKEN_ID,
                "side": "SELL",
                "status": "CANCELED",
                "price": Decimal("0.45"),
                "original_size": Decimal("15"),
                "size_matched": Decimal("0"),
                "order_type": old_receipt.order_type,
                "expiration": old_receipt.expiration,
            },
        },
    }


@pytest.mark.parametrize("session_review", [False, True])
@pytest.mark.parametrize(
    ("order_type", "ttl_seconds"),
    (
        pytest.param("GTC", None, id="gtc"),
        pytest.param("GTD", 600, id="gtd-valid"),
        pytest.param("GTD", -1, id="gtd-expired"),
    ),
)
def test_imported_passive_replacement_lifecycle(
    tmp_path, monkeypatch, order_type: str, ttl_seconds: int | None, session_review: bool
) -> None:
    """Valid imports replace once with the original expiry; expired ones wait."""

    expiration = (
        None
        if ttl_seconds is None
        else int(datetime.now(UTC).timestamp()) + ttl_seconds
    )
    old_receipt = _old_receipt(expiration, order_type=order_type)
    fill = _maker_order("F", "BUY", "15", "0.40")
    monkeypatch.setattr(
        registration_contract, "_SDKAccountClient", _ImportedExitAccount
    )
    store, adapter, account, _, execution = _runtime(
        tmp_path,
        orders=(),
        trades=(_trade("fill-F", fill, size="15"),),
        positions=(
            {
                "condition_id": CONDITION_ID,
                "token_id": TOKEN_ID,
                "outcome": "YES",
                "size": Decimal("15"),
                "average_price": Decimal("0.40"),
            },
        ),
    )
    account.old_receipt = old_receipt
    public = _ImportedExitBook(datetime.now(UTC), bid_price="0.40")
    adapter._public_client_factory = lambda: public

    payload = _active_payload(old_receipt)
    if session_review:
        payload["review_at"] = datetime.now(UTC) + timedelta(minutes=30)
    store.lp_create_session(
        "imported-a", "imported-a", state="entry_open",
        payload=payload,
    )

    try:
        assert _fresh_tick(execution, adapter)["sessions"]

        if ttl_seconds is not None and ttl_seconds < 0:
            for _ in range(3):
                assert _fresh_tick(execution, adapter)["sessions"]
                session = store.lp_session("imported-a")
                assert session["source"] == "account_sync"
                assert session["state"] == "needs_attention"
                assert session["resume_state"] == "passive_exit"
                assert session["reconciliation"] == "passive_expiration_too_soon"
                assert session["passive_exit_order_id"] == "S1"
                assert Decimal(str(session["passive_exit_price"])) == Decimal("0.45")
                assert session["owned_order_ids"] == ["F", "S1"]
                assert set(session["order_history"]) == {"F", "S1"}
                _assert_retained_old(
                    session["order_history"]["S1"],
                    order_type=order_type,
                    expiration=expiration,
                )
                assert account.limit_orders == []
                assert account.posts == []
                assert account.cancel_calls == []
            return

        assert len(account.limit_orders) == 1
        assert len(account.posts) == 1
        signed = account.limit_orders[0]
        assert signed["token_id"] == TOKEN_ID
        assert signed["side"] == "SELL"
        assert signed["post_only"] is True
        assert Decimal(str(signed["size"])) == Decimal("15")
        assert Decimal(str(signed["price"])) == Decimal("0.45")
        assert signed["order_type"] == order_type
        assert signed["expiration"] == expiration

        session = store.lp_session("imported-a")
        assert session["source"] == "account_sync"
        assert session["passive_exit_order_id"] == NEW_SELL_ID
        assert Decimal(str(session["passive_exit_price"])) == Decimal("0.45")
        assert session["owned_order_ids"] == ["F", "S1", NEW_SELL_ID]
        assert set(session["order_history"]) == {"F", "S1", NEW_SELL_ID}
        _assert_retained_old(
            session["order_history"]["S1"],
            order_type=order_type,
            expiration=expiration,
        )
        new_record = session["order_history"][NEW_SELL_ID]
        assert new_record["side"] == "SELL"
        assert new_record["status"] == "LIVE"
        assert new_record["order_type"] == order_type
        assert new_record.get("expiration") == expiration
        assert account.cancel_calls == []

        for _ in range(2):
            assert _fresh_tick(execution, adapter)["sessions"]
            session = store.lp_session("imported-a")
            assert session["passive_exit_order_id"] == NEW_SELL_ID
            assert set(session["order_history"]) == {"F", "S1", NEW_SELL_ID}
            _assert_retained_old(
                session["order_history"]["S1"],
                order_type=order_type,
                expiration=expiration,
            )
            new_record = session["order_history"][NEW_SELL_ID]
            assert new_record["status"] == "LIVE"
            assert new_record["order_type"] == order_type
            assert new_record.get("expiration") == expiration
            assert len(account.limit_orders) == 1
            assert len(account.posts) == 1
            assert account.cancel_calls == []
    finally:
        adapter.close()
