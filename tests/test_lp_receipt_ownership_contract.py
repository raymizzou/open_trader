"""Late accepted-action ownership stays with the exact historical manager."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import tests.test_lp_order_registration_contract as registration_contract
from tests.test_lp_order_registration_contract import (
    _SDKAccountClient,
    _SDKPublicClient,
    _open_order,
    _runtime,
)


CONDITION_ID = "0x" + "c" * 64
TOKEN_ID = "0x" + "1" * 64
WALLET = "0x" + "a" * 40


class _OwnershipAccountSDK(_SDKAccountClient):
    """SDK account with exact-ID lookup for the late terminal journal."""

    def get_order(self, *, order_id: str):
        if order_id == "A":
            return _open_order(
                "A",
                "BUY",
                price="0.40",
                original="10",
                matched="0",
                status="LIVE",
            )
        if order_id == "D":
            return _open_order(
                "D",
                self.expected_d_side,
                price="0.40",
                original="5",
                matched="5",
                status="MATCHED",
            )
        raise KeyError(order_id)


def _warm_public_book(adapter) -> None:
    """Complete the same public single-flight read that lp_tick consumes."""

    key = f"{CONDITION_ID}\0{TOKEN_ID}"
    adapter._read_lp_public_snapshot(key, "market-1", TOKEN_ID, wait=False)
    future = adapter._lp_public_reads.get(key)
    if future is not None:
        future.result(timeout=10)


def _history_payload(side: str) -> dict[str, object]:
    return {
        "order_id": "D",
        "token_id": TOKEN_ID,
        "side": side,
        "status": "MATCHED",
        "price": Decimal("0.40"),
        "original_size": Decimal("5"),
        "size_matched": Decimal("5"),
    }


def _active_payload(review_at: datetime) -> dict[str, object]:
    return {
        "session_id": "active-a",
        "account_id": WALLET,
        "market_id": "market-1",
        "condition_id": CONDITION_ID,
        "token_id": TOKEN_ID,
        "outcome": "YES",
        "price": Decimal("0.40"),
        "quantity": Decimal("10"),
        "group_buy_quantity": Decimal("10"),
        "review_at": review_at,
        "entry_order_id": "A",
        "passive_exit_order_id": None,
        "protected_exit_order_id": None,
        "owned_order_ids": ["A"],
        "order_history": {
            "A": {
                "order_id": "A",
                "token_id": TOKEN_ID,
                "side": "BUY",
                "status": "LIVE",
                "price": Decimal("0.40"),
                "original_size": Decimal("10"),
                "size_matched": Decimal("0"),
            }
        },
    }


@pytest.mark.parametrize(
    ("role", "side"),
    (
        ("entry", "BUY"),
        ("passive_exit", "SELL"),
        ("protected_exit", "SELL"),
    ),
)
def test_accepted_historical_id_does_not_change_active_owner(
    tmp_path, monkeypatch, role: str, side: str
) -> None:
    """A late D journal is audited as D and cannot mutate active manager A."""

    now = datetime.now(UTC).replace(microsecond=0)
    review_at = now + timedelta(hours=1)

    monkeypatch.setattr(
        registration_contract, "_SDKAccountClient", _OwnershipAccountSDK
    )
    store, adapter, account, _, execution = _runtime(
        tmp_path,
        orders=(_open_order("A", "BUY", price="0.40", original="10"),),
        trades=(),
        positions=(),
    )
    account.expected_d_side = side
    public = _SDKPublicClient(now)
    adapter._public_client_factory = lambda: public

    store.lp_create_session(
        "complete-d",
        "complete-d",
        state="complete",
        payload={
            "session_id": "complete-d",
            "account_id": WALLET,
            "market_id": "market-1",
            "condition_id": CONDITION_ID,
            "token_id": TOKEN_ID,
            "outcome": "YES",
            "owned_order_ids": ["D"],
            "order_history": {"D": _history_payload(side)},
            "group_buy_quantity": Decimal("5"),
        },
    )
    store.lp_create_session(
        "active-a",
        "active-a",
        state="entry_open",
        payload=_active_payload(review_at),
    )
    late_action = store.lp_upsert_action(
        "active-a",
        "active-a:late-receipt:D",
        state="accepted",
        payload={
            "role": role,
            "order_id": "D",
            "side": side,
            "status": "MATCHED",
            "price": Decimal("0.40"),
            "quantity": Decimal("5"),
            "size_matched": Decimal("5"),
        },
    )

    posts_before = list(account.posts)
    market_orders_before = list(account.market_orders)

    try:
        _warm_public_book(adapter)
        result = execution.lp_tick()
        assert result["sessions"]

        history = store.lp_session("complete-d")
        active = store.lp_session("active-a")
        assert history["state"] == "complete"
        assert history["owned_order_ids"] == ["D"]
        assert set(history["order_history"]) == {"D"}
        assert history["order_history"]["D"]["side"] == side
        assert Decimal(str(history["order_history"]["D"]["size_matched"])) == Decimal("5")

        assert active["entry_order_id"] == "A"
        assert active["passive_exit_order_id"] is None
        assert active["protected_exit_order_id"] is None
        assert active["owned_order_ids"] == ["A"]
        assert set(active["order_history"]) == {"A"}
        assert active["state"] != "complete"
        assert "D" not in active["order_history"]

        actions = store.lp_actions("active-a")
        assert len(actions) == 1
        audited = actions[0]
        assert audited["action_key"] == late_action["action_key"]
        assert audited["state"] == "accepted"
        assert audited["role"] == role
        assert audited["order_id"] == "D"
        assert audited["side"] == side
        assert Decimal(str(audited["size_matched"])) == Decimal("5")

        assert account.posts == posts_before
        assert account.market_orders == market_orders_before
    finally:
        adapter.close()
