"""Focused public contracts for Issue #288."""

from tests.test_lp_auto_pool import advance_auto_wait

from datetime import timedelta
from decimal import Decimal
import json
import sys
from types import SimpleNamespace

from polymarket.models.clob.account import OpenOrder

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.polymarket_lp_risk import evaluate_lp_entry, estimate_lp_stress_exit

from tests.test_lp_auto_pool import NOW, _fresh_registration_bundle, setup
import tests.test_lp_imported_exit_contract as imported_exit_contract
import tests.test_lp_order_registration_contract as registration_contract
from tests.test_lp_imported_exit_contract import (
    CONDITION_ID,
    TOKEN_ID,
    WALLET,
    _ImportedExitAccount,
    _ImportedExitBook,
    _active_payload,
    _fresh_tick,
    _old_receipt,
)
from tests.test_lp_order_registration_contract import _maker_order, _open_order, _runtime, _trade
from tests.test_prediction_service import (
    _production_request as _http_post,
    _response as _http_response,
    _running_server as _http_server,
)


def _seed_unknown_sell(store):
    store.lp_create_session(
        "sell-unknown",
        "sell-unknown",
        state="entry_open",
        payload={
            "session_id": "sell-unknown",
            "account_id": "test-wallet",
            "wallet_address": "test-wallet",
            "market_id": "sell-market",
            "condition_id": "sell-market",
            "token_id": "sell-token",
            "outcome": "YES",
            "state": "entry_open",
            "passive_exit_order_id": "sell-order",
            "owned_order_ids": [],
            "order_history": {
                "sell-order": {"order_id": "sell-order", "side": "SELL", "status": "LIVE"}
            },
        },
    )
    store.lp_upsert_action(
        "sell-unknown",
        "sell-unknown:passive-submit",
        state="unknown",
        payload={"role": "passive_exit", "side": "SELL", "token_id": "sell-token"},
    )


def _register_inventory(e, exchange, lp):
    exchange.positions = [
        {
            "token_id": "sell-token",
            "condition_id": "sell-market",
            "size": "20",
            "average_price": "1",
        }
    ]
    return lp.register_account_snapshot(_fresh_registration_bundle(exchange, lp))


def _enable_shared_account_refresh(exchange, lp):
    calls = [0]
    clock = [NOW]
    lp.clock = lambda: clock[0]

    def read(*, max_age_seconds=0, trade_generation_provider=None):
        del max_age_seconds
        calls[0] += 1
        clock[0] += timedelta(microseconds=1)
        checked_at = clock[0]
        snapshot = _fresh_registration_bundle(exchange, lp)
        snapshot.update(read_started_at=checked_at, read_ended_at=checked_at, checked_at=checked_at)
        if trade_generation_provider is not None:
            snapshot["trade_generation"] = trade_generation_provider()
        return snapshot

    exchange.lp_account_snapshot_shared = read

    snapshot = exchange.lp_snapshot

    def read_snapshot(request):
        value = snapshot(request)
        value["account"]["trade_generation"] = lp.store.lp_trade_generation()
        return value

    exchange.lp_snapshot = read_snapshot


def test_sell_unknown_does_not_block_configuration_or_buy_refill(tmp_path):
    e, exchange, lp, store = setup(tmp_path, 2)
    _seed_unknown_sell(store)
    assert _register_inventory(e, exchange, lp)["state"] == "registered"
    _enable_shared_account_refresh(exchange, lp)

    configured = e.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    assert configured["slots"]["occupied"] == 0
    assert Decimal(configured["funds"]["available_usd"]) == Decimal("80")
    assert configured["funds"]["realized_pnl_usd"] is None
    assert store.lp_actions("sell-unknown")[0]["state"] == "unknown"

    e.lp_auto_set_desired_running(True)
    state = e.lp_auto_run_once(round_id="issue288-a")
    assert exchange.posts
    assert all(post["side"] == "BUY" for post in exchange.posts)
    assert Decimal(state["funds"]["available_usd"]) == Decimal("80") - Decimal("8") * len(exchange.posts)
    assert store.lp_actions("sell-unknown")[0]["state"] == "unknown"


def test_sell_unknown_does_not_block_configuration_with_represented_buys(tmp_path):
    e, exchange, lp, store = setup(tmp_path, 5)
    _seed_unknown_sell(store)
    exchange.orders = [
        {
            "order_id": f"live-{index}",
            "token_id": f"m0{index}",
            "condition_id": f"m0{index}",
            "market_id": f"m0{index}",
            "outcome": "YES",
            "side": "BUY",
            "status": "LIVE",
            "price": "0.40",
            "original_size": "20",
            "size_matched": "0",
        }
        for index in range(3)
    ]
    assert _register_inventory(e, exchange, lp)["state"] == "registered"
    _enable_shared_account_refresh(exchange, lp)

    configured = e.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    assert configured["slots"]["occupied"] == 3
    assert Decimal(configured["funds"]["buy_reserved_usd"]) == Decimal("24")
    assert Decimal(configured["funds"]["available_usd"]) == Decimal("56")
    assert store.lp_actions("sell-unknown")[0]["state"] == "unknown"

    e.lp_auto_set_desired_running(True)
    state = e.lp_auto_run_once(round_id="issue288-a-represented")
    assert len(exchange.posts) == 2
    assert state["slots"]["occupied"] == 5
    assert Decimal(state["funds"]["available_usd"]) == Decimal("40")
    assert store.lp_actions("sell-unknown")[0]["state"] == "unknown"


def test_sell_unknown_does_not_block_represented_buy_refill(tmp_path):
    e, exchange, lp, store = setup(tmp_path, 3)
    _enable_shared_account_refresh(exchange, lp)
    e.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    e.lp_auto_set_desired_running(True)
    initial = e.lp_auto_run_once(round_id="issue288-a-represented-live")
    assert len(exchange.posts) == 3
    assert initial["slots"]["occupied"] == 3

    for market_id in ("m03", "m04"):
        lp._candidate_pool_record_success(
            market_id,
            dict(condition_id=market_id),
            judged_at=NOW,
            facts=dict(directions=[exchange.direction(market_id)], account=exchange.lp_account_snapshot()),
        )
        store.lp_save_price_history(
            market_id,
            market_id,
            [],
            dict(state="known", amplitude=Decimal(".005"), checked_at=NOW,
                 valid_until=NOW + timedelta(days=1)),
        )

    first = initial["intents"][0]
    store.lp_upsert_action(
        first["session_id"],
        f"{first['session_id']}:passive-unknown",
        state="unknown",
        payload={"role": "passive_exit", "side": "SELL", "token_id": first["token_id"]},
    )
    exchange.positions = [
        {"token_id": "sell-token", "condition_id": "sell-market", "size": "20", "average_price": "1"}
    ]
    e.lp_auto_set_desired_running(False)
    assert e._auto_pool._refresh_account_facts() is True
    paused = e.lp_auto_state()
    assert paused["slots"]["occupied"] == 3
    assert Decimal(paused["funds"]["buy_reserved_usd"]) == Decimal("24")
    assert Decimal(paused["funds"]["available_usd"]) == Decimal("56")
    assert store.lp_actions(first["session_id"])[-1]["state"] == "unknown"


def test_original_unknown_buy_with_sell_history_still_reserves(tmp_path):
    e, exchange, lp, store = setup(tmp_path, 1)
    _install_level2_books(
        exchange,
        lp,
        store,
        bids=(("0.40", "1000"), ("0.37", "1000")),
        count=1,
    )
    store.lp_create_session(
        "original-buy-unknown",
        "original-buy-unknown",
        state="entry_open",
        payload={
            "session_id": "original-buy-unknown",
            "account_id": "test-wallet",
            "wallet_address": "test-wallet",
            "market_id": "old-market",
            "condition_id": "old-market",
            "token_id": "old-token",
            "outcome": "YES",
            "state": "entry_open",
            "entry_order_id": None,
            "passive_exit_order_id": "old-sell",
            "price": Decimal("0.40"),
            "quantity": Decimal("20"),
            "owned_order_ids": [],
            "order_history": {
                "old-sell": {"order_id": "old-sell", "side": "SELL", "status": "LIVE"},
            },
        },
    )
    store.lp_upsert_action(
        "original-buy-unknown",
        "original-buy-unknown:entry-submit",
        state="unknown",
        payload={
            "role": "entry", "side": "BUY", "token_id": "old-token",
            "price": "0.40", "quantity": "20",
        },
    )
    original_account_snapshot = exchange.lp_account_snapshot

    def account_with_only_original_buy_budget():
        snapshot = original_account_snapshot()
        snapshot.update(balance="8", allowance="8")
        return snapshot

    exchange.lp_account_snapshot = account_with_only_original_buy_budget
    lp._candidate_pool_record_success(
        "m00",
        dict(condition_id="m00"),
        judged_at=NOW,
        facts=dict(directions=[exchange.direction("m00")], account=exchange.lp_account_snapshot()),
    )

    e.lp_auto_configure({
        "budget_usd": "100", "target_buy_count": 1, "buy_price_level": 2,
    })
    e.lp_auto_set_desired_running(True)
    state = e.lp_auto_run_once(round_id="issue288-a-original-buy")
    assert exchange.posts == []
    assert state["last_round"]["candidate_filter"]["reasons"] == {
        "balance_insufficient": 1
    }


def _http_runtime(store, exchange, lp, execution):
    runtime = SimpleNamespace(
        mode="production",
        state="RUNNING",
        production_owner=True,
        store=store,
        monitor=SimpleNamespace(),
        execution=execution,
        cross_venue_monitor=None,
    )
    runtime.lp_auto_state = execution.lp_auto_state
    return runtime


def _recreate_execution(data_dir, exchange, lock_path):
    store = PredictionArbitrageStore(data_dir)
    lp = PolymarketLPService(store, exchange, clock=lambda: NOW)
    execution = PredictionExecutionService(
        store=store,
        monitor=SimpleNamespace(),
        trading=exchange,
        notifier=SimpleNamespace(),
        lock_path=lock_path,
        lp=lp,
    )
    execution._breaker_open = False
    return store, lp, execution


def test_auto_price_level_config_survives_restart(tmp_path):
    e, exchange, lp, store = setup(tmp_path, 0)
    runtime = _http_runtime(store, exchange, lp, e)
    config_path = "/api/prediction-arbitrage/lp/auto/config"
    state_path = "/api/prediction-arbitrage/lp/auto/state"
    payload = json.dumps({
        "budget_usd": "100", "target_buy_count": 5, "buy_price_level": 2,
    }).encode()
    with _http_server(
        runtime, session_token="session-token", csrf_token="csrf-token",
    ) as (base, _server):
        status, configured = _http_response(_http_post(base, config_path, data=payload))
        assert status == 200
        assert configured["buy_price_level"] == 2
        status, state = _http_response(base + state_path)
        assert status == 200
        assert state["buy_price_level"] == 2

    restarted_store, restarted_lp, restarted = _recreate_execution(
        store.data_dir, exchange, tmp_path / "execution.lock"
    )
    restarted_runtime = _http_runtime(restarted_store, exchange, restarted_lp, restarted)
    with _http_server(
        restarted_runtime, session_token="session-token", csrf_token="csrf-token",
    ) as (base, _server):
        status, state = _http_response(base + state_path)
        assert status == 200
        assert state["buy_price_level"] == 2
        legacy = json.dumps({"budget_usd": "100", "target_buy_count": 5}).encode()
        status, preserved = _http_response(_http_post(base, config_path, data=legacy))
        assert status == 200
        assert preserved["buy_price_level"] == 2
        for invalid in (0, 3, True, 1.5, "2"):
            body = json.dumps({
                "budget_usd": "100", "target_buy_count": 5,
                "buy_price_level": invalid,
            }).encode()
            status, _error = _http_response(_http_post(base, config_path, data=body))
            assert status == 400

    legacy_e, legacy_exchange, legacy_lp, legacy_store = setup(
        tmp_path / "legacy.sqlite", 0
    )
    legacy_e.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    with legacy_store._transaction() as connection:
        row = connection.execute(
            "SELECT payload FROM lp_auto_pool WHERE singleton=1"
        ).fetchone()
        legacy_document = json.loads(row[0])
        assert legacy_document.pop("buy_price_level") == 1
        connection.execute(
            "UPDATE lp_auto_pool SET payload=? WHERE singleton=1",
            (json.dumps(legacy_document),),
        )
    legacy_store2, legacy_lp2, legacy_e2 = _recreate_execution(
        legacy_store.data_dir, legacy_exchange, tmp_path / "legacy-execution.lock"
    )
    legacy_runtime = _http_runtime(legacy_store2, legacy_exchange, legacy_lp2, legacy_e2)
    with _http_server(
        legacy_runtime, session_token="session-token", csrf_token="csrf-token",
    ) as (base, _server):
        status, legacy_state = _http_response(base + state_path)
        assert status == 200
        assert legacy_state["buy_price_level"] == 1
        legacy_payload = json.dumps({
            "budget_usd": "100", "target_buy_count": 5,
        }).encode()
        status, legacy_preserved = _http_response(
            _http_post(base, config_path, data=legacy_payload)
        )
        assert status == 200
        assert legacy_preserved["buy_price_level"] == 1

    fresh_e, fresh_exchange, fresh_lp, fresh_store = setup(tmp_path / "fresh.sqlite", 0)
    fresh_runtime = _http_runtime(fresh_store, fresh_exchange, fresh_lp, fresh_e)
    with _http_server(
        fresh_runtime, session_token="session-token", csrf_token="csrf-token",
    ) as (base, _server):
        status, fresh = _http_response(base + state_path)
        assert status == 200
        assert fresh["buy_price_level"] == 1
    assert exchange.posts == []


def _install_level2_books(exchange, lp, store, *, bids, count=6):
    original_direction = exchange.direction

    def direction(market_id):
        value = original_direction(market_id)
        value["book"] = {
            "condition_id": market_id,
            "token_id": market_id,
            "received_at": NOW,
            "bids": [dict(price=str(price), size=str(size)) for price, size in bids],
            "asks": [dict(price="0.41", size="1000")],
        }
        return value

    exchange.direction = direction
    for index in range(count):
        market_id = f"m{index:02}"
        lp._candidate_pool_record_success(
            market_id,
            dict(condition_id=market_id),
            judged_at=NOW,
            facts=dict(
                directions=[exchange.direction(market_id)],
                account=exchange.lp_account_snapshot(),
            ),
        )
        store.lp_save_price_history(
            market_id,
            market_id,
            [],
            dict(
                state="known",
                amplitude=Decimal(".005"),
                checked_at=NOW,
                valid_until=NOW + timedelta(days=1),
            ),
        )


def test_second_bid_rejects_crossed_book(tmp_path):
    _execution, exchange, _lp, _store = setup(tmp_path, 0)
    direction = exchange.direction("crossed")
    direction["book"] = {
        "condition_id": "crossed",
        "token_id": "crossed",
        "received_at": NOW,
        "bids": [
            {"price": "0.40", "size": "1000"},
            {"price": "0.37", "size": "1000"},
            {"price": "0.35", "size": "1000"},
        ],
        "asks": [{"price": "0.39", "size": "1000"}],
    }
    evaluated = evaluate_lp_entry(
        direction,
        account=exchange.lp_account_snapshot(),
        now=NOW,
        candidate=True,
        bid_level=2,
    )
    assert evaluated == {
        "state": "unknown",
        "reason_codes": ["book_crossed"],
        "guidance": None,
    }


def test_second_bid_stress_discards_actual_best_bid(tmp_path):
    _execution, exchange, _lp, _store = setup(tmp_path, 0)
    direction = exchange.direction("stress")
    direction["book"] = {
        "condition_id": "stress",
        "token_id": "stress",
        "received_at": NOW,
        "bids": [
            {"price": "0.40", "size": "20"},
            {"price": "0.37", "size": "5"},
            {"price": "0.20", "size": "15"},
        ],
        "asks": [{"price": "0.41", "size": "1000"}],
    }
    market = direction["market"]
    book = direction["book"]
    stress = estimate_lp_stress_exit(
        book,
        market=market,
        price=Decimal("0.37"),
        quantity=Decimal("20"),
        bid_level=2,
    )
    assert stress["state"] == "rejected"
    assert stress["reason_codes"] == ["stress_loss_exceeded"]
    assert stress["gross_exit_value"] == Decimal("4.85")
    assert stress["capital"] == Decimal("7.40")
    assert stress["net_loss"] == Decimal("2.55")
    assert stress["loss_ratio"] == Decimal("2.55") / Decimal("7.40")

    evaluated = evaluate_lp_entry(
        direction,
        account=exchange.lp_account_snapshot(),
        now=NOW,
        candidate=True,
        bid_level=2,
    )
    assert evaluated["state"] == "rejected"
    assert evaluated["reason_codes"] == ["stress_loss_exceeded"]
    assert evaluated["guidance"] is None


def test_auto_quotes_true_second_bid_and_caps_five_orders(tmp_path):
    e, exchange, lp, store = setup(tmp_path, 6)
    _install_level2_books(
        exchange,
        lp,
        store,
        bids=(("0.40", "10"), ("0.40", "10"), ("0.39", "0"),
              ("0.37", "1000"), ("0.35", "1000")),
    )
    e.lp_auto_configure({
        "budget_usd": "100", "target_buy_count": 5, "buy_price_level": 2,
    })
    e.lp_auto_set_desired_running(True)

    state = e.lp_auto_run_once(round_id="issue288-e")
    assert len(exchange.posts) == 5, state["last_round"]
    assert {Decimal(post["price"]) for post in exchange.posts} == {Decimal("0.37")}
    assert {Decimal(post["quantity"]) for post in exchange.posts} == {Decimal("20")}
    assert sum((Decimal(post["price"]) * Decimal(post["quantity"]) for post in exchange.posts), Decimal("0")) == Decimal("37")
    assert Decimal(state["funds"]["buy_reserved_usd"]) == Decimal("37")
    for collection in ("candidates", "targets"):
        rows = state["last_round"][collection]
        assert rows
        assert {Decimal(row["price"]) for row in rows} == {Decimal("0.37")}
        assert {Decimal(row["quantity"]) for row in rows} == {Decimal("20")}
        assert {
            Decimal(row["minimum_order_estimate"]["capital_usd"])
            for row in rows
        } == {Decimal("7.40")}
    assert state["slots"]["occupied"] == 5

    again = e.lp_auto_run_once(round_id="issue288-e-again")
    assert len(exchange.posts) == 5
    assert again["slots"]["occupied"] == 5


def test_auto_second_bid_requires_depth_and_budget(tmp_path):
    shallow, shallow_exchange, shallow_lp, shallow_store = setup(tmp_path / "shallow", 1)
    _install_level2_books(
        shallow_exchange,
        shallow_lp,
        shallow_store,
        bids=(("0.40", "1000"), ("0.39", "0")),
        count=1,
    )
    shallow.lp_auto_configure({
        "budget_usd": "100", "target_buy_count": 1, "buy_price_level": 2,
    })
    shallow.lp_auto_set_desired_running(True)
    shallow_state = shallow.lp_auto_run_once(round_id="issue288-f-depth")
    assert shallow_exchange.posts == []
    assert shallow_state["last_round"]["candidate_filter"]["reasons"] == {
        "second_bid_insufficient": 1
    }, shallow_state["last_round"]

    budget, budget_exchange, budget_lp, budget_store = setup(tmp_path / "budget", 1)
    _install_level2_books(
        budget_exchange,
        budget_lp,
        budget_store,
        bids=(("0.40", "1000"), ("0.37", "1000")),
        count=1,
    )
    budget.lp_auto_configure({
        "budget_usd": "7", "target_buy_count": 1, "buy_price_level": 2,
    })
    budget.lp_auto_set_desired_running(True)
    budget_state = budget.lp_auto_run_once(round_id="issue288-f-budget")
    assert budget_exchange.posts == []
    assert budget_state["last_round"]["actions"] == []
    assert budget_state["last_round"]["blocked"] == [
        {"condition_id": "m00", "token_id": "m00", "reason": "rotation_budget_insufficient"}
    ]


def test_auto_rechecks_second_bid_before_post(tmp_path, monkeypatch):
    e, exchange, lp, store = setup(tmp_path, 1)
    bids = [("0.40", "1000"), ("0.37", "1000")]
    _install_level2_books(exchange, lp, store, bids=bids, count=1)
    e.lp_auto_configure({
        "budget_usd": "100", "target_buy_count": 1, "buy_price_level": 2,
    })
    e.lp_auto_set_desired_running(True)

    def move_second_bid_before_post():
        bids[1] = ("0.36", "1000")

    exchange.before_sign = move_second_bid_before_post
    stale = e.lp_auto_run_once(round_id="issue288-g-stale")
    assert exchange.posts == []
    assert stale["slots"]["occupied"] == 0
    assert stale["last_round"]["actions"], stale["last_round"]
    stale_action = stale["last_round"]["actions"][0]
    assert stale_action["condition_id"] == "m00"
    assert stale_action["state"] == "rejected"
    assert stale_action["request_state"] == "entry_rejected"
    assert stale_action["reason"] == "candidate_bid_level_changed"

    exchange.before_sign = None
    advance_auto_wait(e, monkeypatch, refresh=False)
    monkeypatch.setattr(sys.modules[__name__], 'NOW', e._lp._now())
    lp._candidate_pool_record_success(
        "m00",
        dict(condition_id="m00"),
        judged_at=NOW,
        facts=dict(directions=[exchange.direction("m00")], account=exchange.lp_account_snapshot()),
    )
    fresh = e.lp_auto_run_once(round_id="issue288-g-fresh")
    assert len(exchange.posts) == 1
    assert Decimal(exchange.posts[0]["price"]) == Decimal("0.36")
    assert fresh["slots"]["occupied"] == 1


class _UnknownImportedBuyAccount(_ImportedExitAccount):
    def get_order(self, *, order_id: str) -> OpenOrder:
        if order_id == "F":
            raise KeyError(order_id)
        return super().get_order(order_id=order_id)


class _UnknownBuyWithOrphanSellAccount(_UnknownImportedBuyAccount):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.orphan_status = "LIVE"

    def get_order(self, *, order_id: str) -> OpenOrder:
        if order_id == "O1":
            return _open_order(
                "O1", "SELL", price="0.46", original="5",
                status=self.orphan_status,
            )
        return super().get_order(order_id=order_id)

    def cancel_orders(self, *, order_ids):
        requested = tuple(order_ids)
        self.cancel_calls.append(requested)
        assert requested == ("O1",), "The managed passive SELL must survive"
        self.orphan_status = "CANCELED"
        return {"canceled": list(requested), "not_canceled": {}}


def test_group_collect_preserves_managed_sell_but_cancels_other_owned_sell(tmp_path, monkeypatch):
    old_receipt = _open_order(
        "S1", "SELL", price="0.45", original="20", status="LIVE",
    )
    fill = _maker_order("F", "BUY", "20", "0.40")
    monkeypatch.setattr(
        registration_contract, "_SDKAccountClient", _UnknownBuyWithOrphanSellAccount
    )
    store, adapter, account, _, execution = _runtime(
        tmp_path,
        orders=(),
        trades=(_trade("fill-F", fill, size="20"),),
        positions=({
            "condition_id": CONDITION_ID, "token_id": TOKEN_ID,
            "outcome": "YES", "size": Decimal("20"),
            "average_price": Decimal("0.40"),
        },),
    )
    account.old_receipt = old_receipt
    adapter._public_client_factory = lambda: _ImportedExitBook(NOW, bid_price="0.40")
    payload = _active_payload(old_receipt)
    payload.update(quantity=Decimal("20"), group_buy_quantity=Decimal("20"))
    payload["order_history"]["F"].update(status="UNKNOWN", size_matched=Decimal("20"))
    payload["order_history"]["F"].pop("original_size", None)
    payload["order_history"]["S1"].update(status="LIVE", original_size=Decimal("20"))
    payload["owned_order_ids"].append("O1")
    payload["order_history"]["O1"] = {
        "order_id": "O1", "token_id": TOKEN_ID, "side": "SELL",
        "status": "LIVE", "price": Decimal("0.46"),
        "original_size": Decimal("5"), "size_matched": Decimal("0"),
    }
    store.lp_create_session(
        "imported-mixed", "imported-mixed", state="entry_open",
        payload={**payload, "session_id": "imported-mixed"},
    )
    try:
        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        for _ in range(3):
            _fresh_tick(execution, adapter)
        session = store.lp_session("imported-mixed")
        assert account.cancel_calls == [("O1",)]
        assert session["passive_exit_order_id"] == "S1"
        assert session["order_history"]["F"]["status"] == "UNKNOWN"
        assert Decimal(str(session["residual_quantity"])) == Decimal("20")
        assert account.limit_orders == account.posts == []
    finally:
        adapter.close()


def test_unknown_imported_buy_keeps_live_passive_sell(tmp_path, monkeypatch):
    old_receipt = _open_order(
        "S1", "SELL", price="0.45", original="20", status="LIVE",
    )
    fill = _maker_order("F", "BUY", "20", "0.40")
    monkeypatch.setattr(
        registration_contract, "_SDKAccountClient", _UnknownImportedBuyAccount
    )
    store, adapter, account, _, execution = _runtime(
        tmp_path,
        orders=(),
        trades=(_trade("fill-F", fill, size="20"),),
        positions=(
            {
                "condition_id": CONDITION_ID,
                "token_id": TOKEN_ID,
                "outcome": "YES",
                "size": Decimal("20"),
                "average_price": Decimal("0.40"),
            },
        ),
    )
    account.old_receipt = old_receipt
    public = _ImportedExitBook(NOW, bid_price="0.40")
    adapter._public_client_factory = lambda: public
    payload = _active_payload(old_receipt)
    payload["quantity"] = Decimal("20")
    payload["group_buy_quantity"] = Decimal("20")
    payload["order_history"]["F"].update(
        status="UNKNOWN", original_size=None, size_matched=Decimal("20")
    )
    payload["order_history"]["F"].pop("original_size", None)
    payload["order_history"]["S1"].update(
        status="LIVE", original_size=Decimal("20"), size_matched=Decimal("0")
    )
    store.lp_create_session("imported-unknown", "imported-unknown", state="entry_open",
                            payload={**payload, "session_id": "imported-unknown"})
    try:
        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        for _ in range(3):
            _fresh_tick(execution, adapter)
        session = store.lp_session("imported-unknown")
        assert session["order_history"]["F"]["status"] == "UNKNOWN"
        assert session["passive_exit_order_id"] == "S1"
        assert account.cancel_calls == []
        assert account.limit_orders == []
        assert account.posts == []
    finally:
        adapter.close()


def test_unknown_imported_buy_does_not_repeat_terminal_exit(tmp_path, monkeypatch):
    old_receipt = _open_order(
        "S1", "SELL", price="0.45", original="20", status="CANCELED",
    )
    fill = _maker_order("F", "BUY", "20", "0.40")
    monkeypatch.setattr(
        registration_contract, "_SDKAccountClient", _UnknownImportedBuyAccount
    )
    store, adapter, account, _, execution = _runtime(
        tmp_path,
        orders=(),
        trades=(_trade("fill-F", fill, size="20"),),
        positions=(
            {
                "condition_id": CONDITION_ID,
                "token_id": TOKEN_ID,
                "outcome": "YES",
                "size": Decimal("20"),
                "average_price": Decimal("0.40"),
            },
        ),
    )
    account.old_receipt = old_receipt
    public = _ImportedExitBook(NOW, bid_price="0.40")
    adapter._public_client_factory = lambda: public
    payload = _active_payload(old_receipt)
    payload["quantity"] = Decimal("20")
    payload["group_buy_quantity"] = Decimal("20")
    payload["order_history"]["F"].update(status="UNKNOWN", size_matched=Decimal("20"))
    payload["order_history"]["F"].pop("original_size", None)
    store.lp_create_session("imported-terminal", "imported-terminal", state="entry_open",
                            payload={**payload, "session_id": "imported-terminal"})
    try:
        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        for _ in range(3):
            _fresh_tick(execution, adapter)
        session = store.lp_session("imported-terminal")
        assert session["order_history"]["F"]["status"] == "UNKNOWN"
        assert session["passive_exit_order_id"] == "S1"
        assert account.cancel_calls == []
        assert account.limit_orders == []
        assert account.posts == []
    finally:
        adapter.close()


def test_explicit_stop_of_unknown_imported_session_survives_ticks(tmp_path, monkeypatch):
    old_receipt = _open_order(
        "S1", "SELL", price="0.45", original="20", status="LIVE",
    )
    fill = _maker_order("F", "BUY", "20", "0.40")
    monkeypatch.setattr(
        registration_contract, "_SDKAccountClient", _UnknownImportedBuyAccount
    )
    store, adapter, account, _, execution = _runtime(
        tmp_path,
        orders=(),
        trades=(_trade("fill-F", fill, size="20"),),
        positions=(
            {
                "condition_id": CONDITION_ID,
                "token_id": TOKEN_ID,
                "outcome": "YES",
                "size": Decimal("20"),
                "average_price": Decimal("0.40"),
            },
        ),
    )
    account.old_receipt = old_receipt
    public = _ImportedExitBook(NOW, bid_price="0.40")
    adapter._public_client_factory = lambda: public
    payload = _active_payload(old_receipt)
    payload["quantity"] = Decimal("20")
    payload["group_buy_quantity"] = Decimal("20")
    payload["order_history"]["F"].update(
        status="UNKNOWN", original_size=None, size_matched=Decimal("20")
    )
    payload["order_history"]["F"].pop("original_size", None)
    payload["order_history"]["S1"].update(
        status="LIVE", original_size=Decimal("20"), size_matched=Decimal("0")
    )
    store.lp_create_session(
        "imported-stop", "imported-stop", state="entry_open",
        payload={**payload, "session_id": "imported-stop"},
    )
    try:
        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        stopped = execution.lp_stop("imported-stop")
        assert stopped["state"] in {"review", "needs_attention"}
        assert store.lp_session("imported-stop")["stop_requested"] is True

        account.old_receipt = _open_order(
            "S1", "SELL", price="0.45", original="20", status="CANCELED",
        )
        for _ in range(3):
            _fresh_tick(execution, adapter)
        session = store.lp_session("imported-stop")
        assert session["state"] == "review"
        assert session["stop_requested"] is True
        assert session["order_history"]["F"]["status"] == "UNKNOWN"
        assert Decimal(str(session["residual_quantity"])) == Decimal("20")
        assert account.limit_orders == []
        assert account.posts == []
        assert len(account.cancel_calls) == 1
    finally:
        adapter.close()
