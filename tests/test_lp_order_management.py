from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from open_trader.polymarket_trading import (
    PolymarketTradingClient,
    TradingConfig,
    _lp_bundle_receipt_facts,
)
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_arbitrage_execution import PredictionExecutionService

from tests.test_polymarket_lp import (
    _SDKAccountClient,
    _SDKPublicClient,
    _Exchange,
    _first_seen_episode,
    _snapshot,
)


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
WALLET = "0x" + "3" * 40
FOREIGN = "0x" + "4" * 40


def _trade(*, side: str) -> dict[str, object]:
    return {
        "id": f"trade-{side}",
        "market": "condition-1",
        "token_id": "token-1",
        "taker_order_id": "taker",
        "maker_address": WALLET,
        "trader_side": side.upper(),
        "side": "BUY",
        "size": Decimal("7"),
        "status": "CONFIRMED",
        "maker_orders": [
            {
                "order_id": "maker",
                "token_id": "token-1",
                "maker_address": FOREIGN if side == "taker" else WALLET,
                "matched_amount": Decimal("9"),
            }
        ],
    }


def test_lp_account_facts_publish_complete_read_and_endpoint_contract() -> None:
    account = _SDKAccountClient(NOW)
    adapter = PolymarketTradingClient(
        TradingConfig(WALLET, WALLET),
        account,
        public_client_factory=lambda: _SDKPublicClient(NOW),
    )
    adapter.lp_market_metadata = lambda condition_ids: {
        condition_ids[0]: {
            "market_id": "market-1",
            "condition_id": condition_ids[0],
            "outcomes": {
                "yes": {"token_id": "0x" + "1" * 64, "label": "Yes"},
                "no": {"token_id": "0x" + "2" * 64, "label": "No"},
            },
        }
    }
    facts = adapter._lp_account_facts()

    assert facts["authenticated"] is True
    assert facts["wallet_address"] == WALLET
    assert facts["read_started_at"] <= facts["checked_at"] <= facts["read_ended_at"]
    assert facts["pagination_complete"] is True
    assert facts["balance_complete"] is True
    assert facts["open_orders_complete"] is True
    assert facts["positions_complete"] is True
    assert facts["trades_complete"] is True
    assert [row["order_id"] for row in facts["open_orders"]] == ["order-open"]
    token = adapter.lp_account_round_begin(lambda: 3)
    rounded, _generation, trade_generation = adapter._lp_account_snapshot_for_round(token)
    adapter.lp_account_round_end(token)
    assert rounded["trade_generation"] == 3
    assert trade_generation == 3


def test_post_only_gtc_exit_does_not_invent_an_expiration() -> None:
    class Client:
        def create_limit_order(self, **kwargs):
            return {
                **kwargs,
                "post_only": True,
                "order_type": "GTC" if kwargs.get("expiration") is None else "GTD",
            }

    adapter = PolymarketTradingClient(TradingConfig(WALLET, WALLET), Client())
    signed = adapter.lp_create_limit_order(
        token_id="token-1",
        price=Decimal("0.31"),
        quantity=Decimal("7"),
        side="SELL",
        post_only=True,
        expiration=None,
    )
    assert signed["order_type"] == "GTC"
    assert signed["expiration"] is None


class _RegistrationExchange(_Exchange):
    def __init__(self) -> None:
        super().__init__()
        self.config = SimpleNamespace(wallet_address=WALLET)
        self.book_reads = 0

    def lp_order_books(self, token_ids):
        self.book_reads += len(token_ids)
        base = _snapshot(NOW)
        book = dict(base["book"])
        book["bids"] = [{"price": Decimal("0.30"), "size": Decimal("100")}, *book["bids"]]
        return {token_ids[0]: book}

    def lp_market_metadata(self, condition_ids):
        return {
            condition_ids[0]: {
                "market_id": "market-1",
                "condition_id": condition_ids[0],
                "outcomes": {
                    "yes": {"token_id": "token-1", "label": "Yes"},
                    "no": {"token_id": "token-2", "label": "No"},
                },
            }
        }


def _sync_snapshot() -> dict[str, object]:
    return {
        "authenticated": True,
        "wallet_address": WALLET,
        "account_id": WALLET.casefold(),
        "read_started_at": NOW,
        "read_ended_at": NOW,
        "checked_at": NOW,
        "pagination_complete": True,
        "balance_complete": True,
        "open_orders_complete": True,
        "positions_complete": True,
        "trades_complete": True,
        "open_orders": [
            {
                "order_id": "manual-buy",
                "condition_id": "condition-1",
                "market_id": "market-1",
                "market_title": "Will it happen?",
                "market_url": "https://polymarket.com/event/market-1",
                "token_id": "token-1",
                "outcome": "YES",
                "side": "BUY",
                "price": Decimal("0.30"),
                "original_size": Decimal("10"),
                "size_matched": Decimal("0"),
                "remaining_size": Decimal("10"),
                "status": "LIVE",
                "order_type": "GTC",
                "expiration": None,
                "created_at": NOW,
            }
        ],
        "positions": [],
        "raw_trades": [],
    }


def _generation_snapshot(store: PredictionArbitrageStore, snapshot: dict[str, object]):
    snapshot["trade_generation"] = store.lp_trade_generation()
    return snapshot


def test_sync_registers_manual_buy_with_fresh_baseline_once(tmp_path) -> None:
    exchange = _RegistrationExchange()
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    snapshot = _sync_snapshot()

    first = service.register_account_snapshot(_generation_snapshot(service.store, snapshot))
    assert first["state"] == "registered", first
    sessions = service.store.lp_active_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert session["entry_order_id"] == "manual-buy"
    assert session["owned_order_ids"] == ["manual-buy"]
    assert session["review_at"] is None
    baseline = session["queue_protection"]["levels"]["0.30"]
    assert baseline["state"] == "registered"
    assert baseline["baseline_source"] == "first_observation"
    assert Decimal(str(baseline["baseline_price"])) == Decimal("0.30")
    assert Decimal(str(baseline["baseline_front"])) == Decimal("90")
    assert session["source"] == "account_sync"
    assert session["order_history"]["manual-buy"]["order_type"] == "GTC"
    assert session["order_history"]["manual-buy"]["expiration"] is None

    repeat = service.register_account_snapshot(_generation_snapshot(service.store, snapshot))
    assert repeat["state"] == "registered"
    assert len(service.store.lp_active_sessions()) == 1
    assert exchange.book_reads == 1


def test_same_price_orders_share_one_management_unit_without_double_ceiling(tmp_path) -> None:
    exchange = _RegistrationExchange()
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    snapshot = _sync_snapshot()
    second = dict(snapshot["open_orders"][0])
    second.update(order_id="manual-buy-b", status="LIVE")
    snapshot["open_orders"].append(second)

    assert service.register_account_snapshot(_generation_snapshot(service.store, snapshot))["state"] == "registered"
    assert service.register_account_snapshot(_generation_snapshot(service.store, snapshot))["state"] == "registered"

    assert len(service.store.lp_sessions()) == 1
    session = service.store.lp_active_sessions()[0]
    assert session["owned_order_ids"] == ["manual-buy", "manual-buy-b"]
    assert session["augment_order_ids"] == ["manual-buy-b"]
    assert Decimal(str(session["quantity"])) == Decimal("20")
    assert Decimal(str(session["group_buy_quantity"])) == Decimal("20")


def test_new_exchange_id_does_not_advance_shared_account_generation(tmp_path) -> None:
    store = PredictionArbitrageStore(tmp_path)
    exchange = _RegistrationExchange()
    service = PolymarketLPService(store, exchange, clock=lambda: NOW)
    assert service.register_account_snapshot(
        _generation_snapshot(store, _sync_snapshot())
    )["state"] == "registered"
    session = store.lp_active_sessions()[0]
    old_revision = store.lp_session_revision(str(session["session_id"]), trading=True)
    generation = store.lp_trade_generation()

    result = store.lp_register_exchange_orders(
        WALLET.casefold(),
        "token-1",
        [{
            "order_id": "manual-buy-b",
            "condition_id": "condition-1",
            "token_id": "token-1",
            "side": "BUY",
            "price": Decimal("0.30"),
            "original_size": Decimal("7"),
            "size_matched": Decimal("0"),
            "remaining_size": Decimal("7"),
            "status": "LIVE",
        }],
    )

    assert result["created"] is False
    assert store.lp_trade_generation() == generation
    assert store.lp_session_revision(str(session["session_id"]), trading=True) == old_revision + 1


def test_known_terminal_owner_is_updated_while_new_id_creates_active_group(tmp_path) -> None:
    exchange = _RegistrationExchange()
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    history = _sync_snapshot()
    history["open_orders"][0].update(
        order_id="done-buy", status="FILLED", size_matched=Decimal("10"),
        remaining_size=Decimal("0"),
    )
    assert service.register_account_snapshot(
        _generation_snapshot(service.store, history)
    )["state"] == "registered"
    session_id = service.store.lp_active_sessions()[0]["session_id"]
    service.store.lp_update_session(str(session_id), state="complete")

    mixed = _sync_snapshot()
    done = dict(mixed["open_orders"][0])
    done.update(
        order_id="done-buy", status="FILLED", size_matched=Decimal("10"),
        remaining_size=Decimal("0"),
    )
    fresh = dict(done)
    fresh.update(
        order_id="fresh-buy", status="LIVE", size_matched=Decimal("0"),
        remaining_size=Decimal("4"), original_size=Decimal("4"),
    )
    mixed["open_orders"] = [done, fresh]
    result = service.register_account_snapshot(
        _generation_snapshot(service.store, mixed)
    )

    assert result["state"] == "registered"
    assert len(service.store.lp_sessions()) == 2
    complete = service.store.lp_session(str(session_id))
    active = service.store.lp_active_sessions()[0]
    assert complete["order_history"]["done-buy"]["status"] == "FILLED"
    assert active["owned_order_ids"] == ["fresh-buy"]
    assert Decimal(str(active["quantity"])) == Decimal("4")


def test_repeat_receipt_does_not_increase_fill_only_quantity(tmp_path) -> None:
    exchange = _RegistrationExchange()
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    fill = _sync_snapshot()
    fill["open_orders"] = []
    fill["raw_trades"] = [{
        "id": "trade-1", "condition_id": "condition-1", "token_id": "token-1",
        "maker_address": WALLET, "owner": WALLET, "trader_side": "MAKER",
        "side": "BUY", "price": Decimal("0.30"), "size": Decimal("2"),
        "status": "CONFIRMED", "maker_orders": [{
            "order_id": "receipt-late", "token_id": "token-1",
            "maker_address": WALLET, "side": "BUY", "price": Decimal("0.30"),
            "matched_amount": Decimal("2"),
        }],
    }]
    fill["positions"] = [{
        "condition_id": "condition-1", "token_id": "token-1",
        "outcome": "YES", "size": Decimal("2"),
    }]
    assert service.register_account_snapshot(
        _generation_snapshot(service.store, fill)
    )["state"] == "registered"
    assert service.register_account_snapshot(
        _generation_snapshot(service.store, fill)
    )["state"] == "registered"
    session = service.store.lp_active_sessions()[0]

    assert Decimal(str(session["quantity"])) == Decimal("2")
    assert Decimal(str(session["group_buy_quantity"])) == Decimal("2")
    assert Decimal(str(session["buy_filled_quantity"])) == Decimal("2")


def test_restarted_store_normalizes_amounts_and_updates_vwap_once(tmp_path) -> None:
    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session(
        "restart-order",
        "restart-order-key",
        state="entry_open",
        payload={
            "account_id": WALLET.casefold(),
            "token_id": "token-1",
            "condition_id": "condition-1",
            "entry_order_id": "vwap-buy",
            "owned_order_ids": ["vwap-buy"],
            "order_history": {
                "vwap-buy": {
                    "order_id": "vwap-buy",
                    "token_id": "token-1",
                    "side": "BUY",
                    "status": "PARTIALLY_FILLED",
                    "price": Decimal("0.50"),
                    "quantity": Decimal("10"),
                    "size_matched": Decimal("5"),
                    "average_price": Decimal("0.30"),
                    "fills": [{
                        "trade_id": "trade-1", "quantity": Decimal("5"),
                        "price": Decimal("0.30"), "notional": Decimal("1.50"),
                    }],
                }
            },
        },
    )

    restarted_store = PredictionArbitrageStore(tmp_path)
    restarted_service = PolymarketLPService(
        restarted_store, _RegistrationExchange(), clock=lambda: NOW
    )
    stale = restarted_store.lp_register_exchange_orders(
        WALLET.casefold(),
        "token-1",
        [{
            "order_id": "vwap-buy", "token_id": "token-1", "side": "BUY",
            "status": "PARTIALLY_FILLED", "price": Decimal("0.50"),
            "quantity": Decimal("10"), "size_matched": Decimal("2"),
            "average_price": Decimal("0.30"),
        }],
    )
    record = stale["session"]["order_history"]["vwap-buy"]
    assert Decimal(str(record["size_matched"])) == Decimal("5")
    assert Decimal(str(record["average_price"])) == Decimal("0.30")
    assert restarted_service is not None

    updated = restarted_store.lp_register_exchange_orders(
        WALLET.casefold(),
        "token-1",
        [{
            "order_id": "vwap-buy", "token_id": "token-1", "side": "BUY",
            "status": "PARTIALLY_FILLED", "price": Decimal("0.50"),
            "quantity": Decimal("10"), "size_matched": Decimal("8"),
            "average_price": Decimal("0.3375"),
            "fills": [{
                "trade_id": "trade-2", "quantity": Decimal("3"),
                "price": Decimal("0.40"), "notional": Decimal("1.20"),
            }],
        }],
    )
    record = updated["session"]["order_history"]["vwap-buy"]
    assert Decimal(str(record["size_matched"])) == Decimal("8")
    assert Decimal(str(record["average_price"])) == Decimal("0.3375")
    assert {item["trade_id"] for item in record["fills"]} == {"trade-1", "trade-2"}

    replay = restarted_store.lp_register_exchange_orders(
        WALLET.casefold(),
        "token-1",
        [{
            "order_id": "vwap-buy", "token_id": "token-1", "side": "BUY",
            "status": "PARTIALLY_FILLED", "price": Decimal("0.50"),
            "quantity": Decimal("10"), "size_matched": Decimal("8"),
            "average_price": Decimal("0.3375"),
            "fills": [{
                "trade_id": "trade-2", "quantity": "3",
                "price": "0.40", "notional": "1.20",
            }],
        }],
    )
    record = replay["session"]["order_history"]["vwap-buy"]
    assert Decimal(str(record["size_matched"])) == Decimal("8")
    assert Decimal(str(record["average_price"])) == Decimal("0.3375")
    assert len(record["fills"]) == 2


def test_registered_partial_fill_and_remaining_do_not_regress(tmp_path) -> None:
    store = PredictionArbitrageStore(tmp_path)
    session = {
        "session_id": "partial", "condition_id": "condition-1",
        "token_id": "token-1", "outcome": "YES", "state": "entry_open",
    }
    order = {
        "order_id": "partial-buy", "token_id": "token-1", "side": "BUY",
        "original_size": "10", "price": "0.50",
    }
    for status, matched, remaining, expected_status, expected_remaining in (
        ("LIVE", "0", "10", "LIVE", "10"),
        ("PARTIALLY_FILLED", "5", "5", "PARTIALLY_FILLED", "5"),
        ("LIVE", "0", "10", "PARTIALLY_FILLED", "5"),
        ("FILLED", "10", "0", "FILLED", "0"),
        ("PARTIALLY_FILLED", "5", "5", "FILLED", "0"),
    ):
        result = store.lp_register_exchange_orders(
            WALLET, "token-1", [{**order, "status": status,
                "size_matched": matched, "remaining_size": remaining}],
            session=session,
        )
        record = result["session"]["order_history"]["partial-buy"]
        assert record["status"] == expected_status
        assert Decimal(str(record["remaining_size"])) == Decimal(expected_remaining)
        assert Decimal(str(record["size_matched"])) == 10 - Decimal(expected_remaining)
        store = PredictionArbitrageStore(tmp_path)


@pytest.mark.parametrize("role, side, token", [
    ("entry_order_id", "SELL", "token-1"),
    ("augment_order_ids", "SELL", "token-1"),
    ("passive_exit_order_id", "BUY", "token-1"),
    ("protected_exit_order_id", "BUY", "token-1"),
    ("entry_order_id", "BUY", "other-token"),
])
def test_legacy_identity_conflict_rolls_back_without_order_history(tmp_path, role, side, token):
    store = PredictionArbitrageStore(tmp_path)
    value = ["legacy-id"] if role.endswith("ids") else "legacy-id"
    store.lp_create_session("legacy", "legacy", state="entry_open", payload={
        "account_id": WALLET, "token_id": "token-1", "condition_id": "condition-1",
        role: value, "order_history": {},
    })
    before = store.lp_session("legacy")
    with pytest.raises(ValueError, match="order_identity_(conflict|unknown)"):
        store.lp_register_exchange_orders(WALLET, token, [{
            "order_id": "legacy-id", "token_id": token, "side": side,
            "status": "LIVE", "price": "0.4", "original_size": "10", "size_matched": "0",
        }])
    assert store.lp_session("legacy") == before
    assert len(store.lp_sessions()) == 1


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_owned_only_legacy_id_recovers_real_side_once_across_restart(tmp_path, side):
    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session("legacy", "legacy", state="entry_open", payload={
        "account_id": WALLET, "token_id": "token-1", "condition_id": "condition-1",
        "owned_order_ids": ["legacy-id"], "order_history": {},
    })
    for _ in range(2):
        result = store.lp_register_exchange_orders(WALLET, "token-1", [{
            "order_id": "legacy-id", "token_id": "token-1", "side": side,
            "status": "LIVE", "price": "0.4", "original_size": "10", "size_matched": "0",
        }])
        assert result["session"]["session_id"] == "legacy"
        assert result["session"]["order_history"]["legacy-id"]["side"] == side
        assert result["session"]["owned_order_ids"] == ["legacy-id"]
        assert len(store.lp_sessions()) == 1
        store = PredictionArbitrageStore(tmp_path)


def test_first_seen_conversion_rolls_back_with_registration_transaction(tmp_path) -> None:
    store = PredictionArbitrageStore(tmp_path)
    _first_seen_episode(store, "legacy", anchors=("manual-buy",))
    candidate = {
        "session_id": "candidate",
        "condition_id": "condition-1",
        "market_id": "market-1",
        "token_id": "token-1",
        "outcome": "YES",
        "price": Decimal("0.30"),
        "quantity": Decimal("10"),
        "_first_seen_episode_id": "legacy",
        "queue_protection": {
            "baseline_front": "8000",
            "baseline_price": Decimal("0.30"),
            "state": "monitoring",
        },
        "position_reconciled": True,
    }
    connection = store._connection()
    connection.execute("BEGIN IMMEDIATE")
    try:
        store.lp_register_exchange_orders(
            WALLET.casefold(),
            "token-1",
            [{
                "order_id": "manual-buy",
                "condition_id": "condition-1",
                "token_id": "token-1",
                "side": "BUY",
                "price": Decimal("0.30"),
                "original_size": Decimal("10"),
                "size_matched": Decimal("0"),
                "remaining_size": Decimal("10"),
                "status": "LIVE",
            }],
            session=candidate,
            connection=connection,
        )
    finally:
        connection.execute("ROLLBACK")
        connection.close()

    assert store.lp_sessions() == []
    assert store.lp_first_seen_episode("legacy")["state"] == "monitoring"


def test_sync_sell_joins_the_same_token_management_unit(tmp_path) -> None:
    exchange = _RegistrationExchange()
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    sell_snapshot = _sync_snapshot()
    sell_snapshot["open_orders"][0].update(
        order_id="manual-sell", side="SELL"
    )
    result = service.register_account_snapshot(
        _generation_snapshot(service.store, sell_snapshot)
    )

    assert result["state"] == "registered"
    sessions = service.store.lp_active_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert session["passive_exit_order_id"] == "manual-sell"
    assert session["owned_order_ids"] == ["manual-sell"]
    assert session["order_history"]["manual-sell"]["side"] == "SELL"


def test_incomplete_snapshot_is_not_registered_as_zero_orders(tmp_path) -> None:
    exchange = _RegistrationExchange()
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    snapshot = _sync_snapshot()
    snapshot["open_orders_complete"] = False

    result = service.register_account_snapshot(
        _generation_snapshot(service.store, snapshot)
    )
    assert result == {"state": "skipped", "reason": "account_snapshot_incomplete"}
    assert service.store.lp_active_sessions() == []


def test_fully_filled_maker_history_registers_without_open_order(tmp_path) -> None:
    exchange = _RegistrationExchange()
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    snapshot = _sync_snapshot()
    snapshot["open_orders"] = []
    snapshot["raw_trades"] = [
        {
            "id": "trade-filled",
            "condition_id": "condition-1",
            "token_id": "token-1",
            "maker_address": WALLET,
            "owner": WALLET,
            "trader_side": "MAKER",
            "side": "BUY",
            "price": Decimal("0.30"),
            "size": Decimal("7"),
            "status": "CONFIRMED",
            "maker_orders": [{
                "order_id": "filled-buy",
                "token_id": "token-1",
                "maker_address": WALLET,
                "side": "BUY",
                "price": Decimal("0.30"),
                "matched_amount": Decimal("7"),
            }],
        }
    ]
    snapshot["positions"] = [{
        "condition_id": "condition-1",
        "token_id": "token-1",
        "outcome": "YES",
        "size": Decimal("7"),
    }]
    result = service.register_account_snapshot(
        _generation_snapshot(service.store, snapshot)
    )

    assert result["state"] == "registered"
    session = service.store.lp_active_sessions()[0]
    assert session["owned_order_ids"] == ["filled-buy"]
    assert Decimal(str(session["buy_filled_quantity"])) == Decimal("7")
    assert Decimal(str(session["residual_quantity"])) == Decimal("7")


def test_legacy_first_seen_episode_is_converted_without_duplicate_protection(tmp_path) -> None:
    store = PredictionArbitrageStore(tmp_path)
    _first_seen_episode(store, "legacy", anchors=("manual-buy",))
    exchange = _RegistrationExchange()
    service = PolymarketLPService(store, exchange, clock=lambda: NOW)

    snapshot = _sync_snapshot()
    snapshot["open_orders"][0]["token_id"] = "0x" + "1" * 64
    snapshot["open_orders"][0]["condition_id"] = "0x" + "c" * 64
    result = service.register_account_snapshot(
        _generation_snapshot(service.store, snapshot)
    )

    assert result["state"] == "registered"
    assert store.lp_active_first_seen_episodes() == []
    assert store.lp_first_seen_episode("legacy")["state"] == "converted"
    session = store.lp_active_sessions()[0]
    baseline = session["queue_protection"]["levels"]["0.30"]
    assert baseline["baseline_front"] == "8000"
    assert baseline["baseline_source"] == "first_observation"
    assert Decimal(str(baseline["baseline_price"])) == Decimal("0.30")


def test_dashboard_sync_view_marks_discovered_order_managed(tmp_path) -> None:
    account = _SDKAccountClient(NOW)
    wallet = str(account.open_order.owner)
    adapter = PolymarketTradingClient(
        TradingConfig(wallet, wallet),
        account,
        public_client_factory=lambda: _SDKPublicClient(NOW),
    )
    adapter.lp_market_metadata = lambda condition_ids: {
        condition_ids[0]: {
            "market_id": "market-1",
            "condition_id": condition_ids[0],
            "outcomes": {
                "yes": {"token_id": "0x" + "1" * 64, "label": "Yes"},
                "no": {"token_id": "0x" + "2" * 64, "label": "No"},
            },
        }
    }
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, adapter)
    engine = PredictionExecutionService(
        store=store,
        monitor=SimpleNamespace(),
        trading=adapter,
        notifier=SimpleNamespace(),
        lock_path=tmp_path / "execution.lock",
        lp=lp,
    )
    engine._breaker_open = False
    adapter_snapshot = adapter.lp_account_snapshot_shared(
        trade_generation_provider=store.lp_trade_generation
    )
    registration = lp.register_account_snapshot(adapter_snapshot)
    assert registration["state"] == "registered", registration
    assert registration["tokens"][0].get("reason") is None
    assert registration["tokens"][0]["state"] == "created"
    assert registration["state"] == "registered"
    dashboard = engine.refresh_lp_dashboard_snapshot()

    assert [row["management"] for row in dashboard["orders"]] == ["system_managed"]
    assert dashboard["orders"][0]["session_id"]
    assert len(store.lp_active_sessions()) == 1
    assert set(store.lp_active_sessions()[0]["owned_order_ids"]) == {"order-1", "order-open"}
    order_history = store.lp_active_sessions()[0]["order_history"]
    assert order_history["order-1"]["side"] == "BUY"
    assert order_history["order-open"]["side"] == "SELL"
    assert "taker-1" not in order_history
    assert "taker-1" not in store.lp_active_sessions()[0]["augment_order_ids"]
