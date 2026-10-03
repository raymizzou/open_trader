"""Offline account-wide financial coverage contracts for issue #208.

Real SDK response models feed the production adapter, real SQLite Store, and
public execution entry points. Business time and read races are controlled;
no account endpoint contacts the network and no wait uses arbitrary sleeps.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event

import pytest
from polymarket.models.clob.order_book import OrderBookLevel
from polymarket.models.clob.rewards import CurrentReward, CurrentRewardConfig
from polymarket.models.data.portfolio import Position

from open_trader import polymarket_trading, prediction_arbitrage_store
from tests import test_lp_order_registration_contract as sdk
from tests.test_lp_order_registration_contract import (
    CONDITION_ID,
    NO_TOKEN_ID,
    TOKEN_ID,
    WALLET,
    _maker_order,
    _open_order,
    _trade,
)


NOW = datetime(2026, 10, 3, 8, tzinfo=UTC)


class _CandidateSDKPublic(sdk._SDKPublicClient):
    def get_market(self, *, id):
        market = super().get_market(id=id)
        return market.model_copy(update={
            "trading": market.trading.model_copy(update={"minimum_order_size": Decimal("20")}),
            "rewards": market.rewards.model_copy(update={"rewards_min_size": Decimal("20")}),
        })

    def get_order_book(self, *, token_id):
        book = super().get_order_book(token_id=token_id)
        return book.model_copy(update={
            "bids": (OrderBookLevel(price=Decimal(".40"), size=Decimal("1000")),
                     OrderBookLevel(price=Decimal(".39"), size=Decimal("1000"))),
            "asks": (OrderBookLevel(price=Decimal(".42"), size=Decimal("1000")),),
            "min_order_size": Decimal("20"),
        })

    def list_market_rewards(self, *, condition_id, sponsored):
        assert condition_id == CONDITION_ID
        return (CurrentReward(
            condition_id=condition_id, rewards_max_spread=10, rewards_min_size=Decimal("20"),
            rewards_config=(CurrentRewardConfig(
                asset_address="0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
                start_date=NOW - timedelta(days=1), end_date=NOW + timedelta(days=1),
                rate_per_day=Decimal("24"),
            ),),
        ),)

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """Freeze only adapter/store business clocks, leaving watchdogs real."""
    current = [NOW]

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class FrozenDateTime(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return current[0] if tz is not None else current[0].replace(tzinfo=None)

    monkeypatch.setattr(polymarket_trading, "datetime", FrozenDateTime)
    monkeypatch.setattr(prediction_arbitrage_store, "_utc_now", lambda: current[0].isoformat())
    monkeypatch.setattr(sdk, "NOW", NOW)
    adapters = []

    def build(**kwargs):
        result = sdk._runtime(tmp_path, **kwargs)
        result[3].clock = lambda: current[0]
        adapters.append(result[1])
        return result

    build.clock = current
    yield build
    for adapter in reversed(adapters):
        adapter.close()


def _seed_unknowns(store, execution, *, count=2, ownership=True, stage="legacy", explicit_account=False):
    """Recreate durable pre-206 rows without inventing an exchange order ID.

    A legacy row has no wallet field. Its exact original lp-auto idempotency
    key and unique pool/session binding prove ownership; a lookalike key does
    not. Terminal action timestamps prove that the old send already ended.
    """
    pool = execution._auto_pool
    document = pool._read()
    intents = []
    for index in range(count):
        iid, sid = f"old-round:{index}", f"legacy-unknown-{index}"
        facts = {
            "market_id": "market-1" if index == 0 else f"legacy-market-{index}",
            "condition_id": CONDITION_ID if index == 0 else f"0x{index + 0x20:064x}",
            "token_id": TOKEN_ID if index == 0 else f"0x{index + 0x10:064x}",
            "outcome": "YES", "price": "0.40",
            "quantity": "20", "buy_filled_quantity": "0", "buy_cost": "0",
            "sold_quantity": "0", "sold_revenue": "0", "residual_quantity": "0",
            "fees": "0", "fee_status": "unknown", "entry_order_id": None,
            "owned_order_ids": [], "order_history": {}, "submit_status": "unknown",
            "resume_state": "entry_submit_pending", "origin": "auto",
            "auto_run_id": document["run_id"],
        }
        if explicit_account:
            facts["account_id"] = WALLET
        action = {"role": "entry", "side": "BUY", "token_id": facts["token_id"]}
        if stage != "legacy":
            submit = {
                "submit_stage": stage, "post_started": True,
                "submit_post_started_at": execution._lp._now().isoformat(),
                "submit_finished_at": execution._lp._now().isoformat() if stage == "send_unknown" else None,
            }
            facts.update(submit)
            action.update(submit)
        store.lp_create_session(
            sid, f"lp-auto:{iid}" if ownership else f"unproven:{iid}",
            state="needs_attention", payload=facts,
        )
        store.lp_upsert_action(
            sid, f"{sid}:entry-submit", state="pending" if stage == "sending" else "unknown",
            payload=action,
        )
        intent = {
            "intent_id": iid, "session_id": sid, "order_id": None,
            "config_version": document["config_version"], "state": "unknown",
            "reserved_usd": "8", "inventory_cost_usd": "0", "realized_pnl_usd": "0",
            "financial_status": "unknown", "created_at": execution._lp._now().isoformat(),
            "checked_at": execution._lp._now().isoformat(),
            **{key: facts[key] for key in ("market_id", "condition_id", "token_id", "outcome", "price", "quantity")},
        }
        intents.append(intent)

    def seed(document):
        for intent in intents:
            document["intents"][intent["intent_id"]] = intent
            pool._event(document, intent, "unknown", reason="missing_reliable_order_id")

    pool._update(seed)
    return intents


def _advance(runtime, seconds=1):
    runtime.clock[0] += timedelta(seconds=seconds)


def _amount(state, name):
    return Decimal(state["funds"][name])


def _assert_unknown_audit(store, execution, originals, *, managed_order_id=None):
    persisted = execution._auto_pool._read()["intents"]
    for original in originals:
        intent = persisted[original["intent_id"]]
        assert intent["state"] == "unknown"
        assert intent["order_id"] is None
        session = store.lp_session(original["session_id"])
        assert session["submit_status"] == "unknown"
        assert session.get("entry_order_id") == managed_order_id
        action = store.lp_actions(original["session_id"])[0]
        assert action["state"] == "unknown"
        assert not action.get("order_id")


def test_two_same_token_order_ids_occupy_two_manual_slots_without_fake_intents(runtime):
    orders = (
        _open_order("manual-buy-a", "BUY", price="0.40", original="20"),
        _open_order("manual-buy-b", "BUY", price="0.40", original="20"),
    )
    store, adapter, account, lp, execution = runtime(orders=orders)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    dashboard = execution.refresh_lp_dashboard_snapshot()
    assert dashboard["state"] == "ready", dashboard
    assert len(store.lp_active_sessions()) == 1
    assert set(store.lp_active_sessions()[0]["owned_order_ids"]) == {
        "manual-buy-a", "manual-buy-b"
    }
    state = execution.lp_auto_state()
    assert state["slots"]["occupied"] == 2, state
    assert _amount(state, "buy_reserved_usd") == 16, state
    assert state["intents"] == []
    assert account.posts == account.cancels == []


def test_second_token_registration_failure_rolls_back_entire_account_round(runtime, monkeypatch):
    orders = (
        _open_order("token-a-buy", "BUY", price="0.40", original="20"),
        _open_order("token-b-buy", "BUY", price="0.40", original="20",
                    token_id=NO_TOKEN_ID, outcome="NO"),
    )
    store, adapter, account, lp, execution = runtime(orders=orders)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution)
    _advance(runtime)
    sessions_before = deepcopy(store.lp_sessions())
    pool_before = deepcopy(execution._auto_pool._read())
    register = store.lp_register_exchange_orders
    visited = []

    def fail_second(account_id, token_id, rows, **kwargs):
        visited.append(token_id)
        if token_id == NO_TOKEN_ID:
            raise ValueError("injected_second_token_failure")
        return register(account_id, token_id, rows, **kwargs)

    monkeypatch.setattr(store, "lp_register_exchange_orders", fail_second)
    result = execution.refresh_lp_dashboard_snapshot()
    assert visited == [TOKEN_ID, NO_TOKEN_ID]
    assert result["state"] != "ready", result
    assert store.lp_sessions() == sessions_before, "The failed round left token A committed"
    assert execution._auto_pool._read() == pool_before, "Coverage must roll back with the orders"
    assert _amount(execution.lp_auto_state(), "buy_reserved_usd") == 16
    _assert_unknown_audit(store, execution, originals)
    assert account.posts == account.cancels == []


def test_five_ended_legacy_unknown_sends_release_in_one_account_round(runtime):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution, count=5)
    before = execution.lp_auto_state()
    assert _amount(before, "buy_reserved_usd") == 40
    assert before["slots"]["occupied"] == 5
    actions = {row["session_id"]: store.lp_actions(row["session_id"]) for row in originals}
    _advance(runtime)

    dashboard = execution.refresh_lp_dashboard_snapshot()
    state = execution.lp_auto_state()
    assert dashboard["state"] == "ready", dashboard
    assert state["funds"]["status"] == "known", state
    assert _amount(state, "buy_reserved_usd") == 0
    assert _amount(state, "available_usd") == 100
    assert state["slots"]["occupied"] == 0
    assert len(state["intents"]) == 5
    for row in originals:
        assert store.lp_actions(row["session_id"]) == actions[row["session_id"]]
        assert store.lp_session(row["session_id"])["reservation_coverage"]["state"] == "covered"
    _assert_unknown_audit(store, execution, originals)
    assert lp._candidate_reservations() == ()
    assert account.position_reads == 1
    assert account.posts == account.cancels == []


@pytest.mark.parametrize("entrypoint", ["dashboard", "reconcile", "round"])
@pytest.mark.parametrize("actual_buy", [False, True], ids=["empty", "one-real-buy"])
def test_two_old_reservations_are_replaced_once_by_complete_account_truth(runtime, entrypoint, actual_buy):
    orders = (_open_order("actual-buy", "BUY", price="0.40", original="20"),) if actual_buy else ()
    store, adapter, account, lp, execution = runtime(orders=orders)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution)
    _advance(runtime)
    if entrypoint == "dashboard":
        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        state = execution.lp_auto_state()
    elif entrypoint == "reconcile":
        state = execution.lp_auto_reconcile_unknown()
    else:
        state = execution.lp_auto_run_once(round_id="one-complete-account-round")
    assert state["funds"]["status"] == "known", state
    assert _amount(state, "buy_reserved_usd") == (8 if actual_buy else 0), state
    assert _amount(state, "available_usd") == (92 if actual_buy else 100)
    assert state["slots"]["occupied"] == int(actual_buy)
    assert len(state["intents"]) == 2, "Account-discovered orders must not fabricate auto intents"
    _assert_unknown_audit(store, execution, originals)
    assert account.posts == account.cancels == []


@pytest.mark.parametrize("reason", ["missing-ownership", "inflight", "no-end-evidence"])
def test_unproven_or_inflight_legacy_reservations_remain_blocked(runtime, reason):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution, count=1,
        ownership=reason != "missing-ownership", stage="sending" if reason == "inflight" else "legacy")
    if reason == "no-end-evidence":
        # A pending action supplies no durable proof that the sender ended.
        store.lp_upsert_action(originals[0]["session_id"], f"{originals[0]['session_id']}:entry-submit",
                               state="pending", payload={"role": "entry", "side": "BUY", "token_id": TOKEN_ID})
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    state = execution.lp_auto_state()
    assert _amount(state, "buy_reserved_usd") == 8
    assert state["slots"]["occupied"] == 1
    assert state["funds"]["status"] == "unknown"
    assert state["funds"]["available_usd"] is None
    assert not store.lp_session(originals[0]["session_id"]).get("reservation_coverage")
    assert lp._candidate_reservations() == ({"order_id": f"lp-session:{originals[0]['session_id']}", "amount": Decimal("8")},)
    assert account.posts == account.cancels == []


@pytest.mark.parametrize("invalid", ["incomplete-balance", "incomplete-orders", "incomplete-trades", "incomplete-positions", "incomplete-pagination", "stale", "pre-send-end", "started-before-send-end"])
def test_incomplete_stale_or_pre_end_snapshot_cannot_cover_a_hold(runtime, invalid):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution, stage="send_unknown")
    if invalid != "pre-send-end":
        _advance(runtime)
    snapshot = adapter.lp_account_snapshot_shared(max_age_seconds=0, trade_generation_provider=store.lp_trade_generation)
    if invalid.startswith("incomplete-"):
        field = {"balance": "balance", "orders": "open_orders", "trades": "trades", "positions": "positions", "pagination": "pagination"}[invalid.removeprefix("incomplete-")]
        snapshot[f"{field}_complete"] = False
    elif invalid == "stale":
        _advance(runtime, 61)
    elif invalid == "started-before-send-end":
        snapshot["read_started_at"] = NOW - timedelta(seconds=1)
    lp.register_account_snapshot(snapshot)
    state = execution.lp_auto_state()
    assert _amount(state, "buy_reserved_usd") == 16
    assert state["slots"]["occupied"] == 2
    assert state["funds"]["available_usd"] is None
    for original in originals:
        assert not store.lp_session(original["session_id"]).get("reservation_coverage")
    assert account.posts == account.cancels == []


def test_read_generation_race_retains_holds_until_next_complete_round(runtime):
    entered, release = Event(), Event()

    def pause_positions(read_number):
        if read_number == 1:
            entered.set()
            assert release.wait(10), "Read-release watchdog expired"

    order = _open_order("later-buy", "BUY", price="0.40", original="20")
    store, adapter, account, lp, execution = runtime(orders=(order,), before_positions=pause_positions)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution)
    _advance(runtime)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(execution.refresh_lp_dashboard_snapshot)
        try:
            assert entered.wait(10), "Account read did not reach the controlled boundary"
            assert store.lp_advance_trade_generation(store.lp_trade_generation()) is True
        finally:
            release.set()
        racing = future.result(timeout=10)
    assert racing["state"] == "waiting", racing
    assert _amount(execution.lp_auto_state(), "buy_reserved_usd") == 16
    assert len(store.lp_sessions()) == 2
    assert all(not store.lp_session(i["session_id"]).get("reservation_coverage") for i in originals)
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    assert _amount(execution.lp_auto_state(), "buy_reserved_usd") == 8
    assert execution.lp_auto_state()["slots"]["occupied"] == 1
    assert account.position_reads == 2
    assert account.posts == account.cancels == []


def _position(size="20"):
    return Position(conditionId=CONDITION_ID, proxyWallet=WALLET, asset=TOKEN_ID,
                    size=Decimal(size), avgPrice=Decimal("0.40"), outcome="YES")


def test_actual_inventory_keeps_verified_cost_without_a_buy_slot(runtime):
    fill = _trade("inventory-fill", _maker_order("filled-buy", "BUY", "20", "0.40"), size="20")
    store, adapter, account, lp, execution = runtime(trades=(fill,), positions=(_position(),))
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution)
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    state = execution.lp_auto_state()
    assert state["funds"]["status"] == "known", state
    assert _amount(state, "inventory_cost_usd") == 8
    assert _amount(state, "buy_reserved_usd") == 0
    assert _amount(state, "available_usd") == 92
    assert state["slots"]["occupied"] == 0
    _assert_unknown_audit(store, execution, originals)
    assert account.posts == account.cancels == []


@pytest.mark.parametrize("uncertainty", ["fee", "inventory-basis"])
def test_unknown_fee_or_inventory_basis_never_releases_legacy_holds(runtime, uncertainty):
    maker = _maker_order("filled-buy", "BUY", "20", "0.40")
    if uncertainty == "fee":
        maker = maker.model_copy(update={"fee_rate_bps": None})
    fill = _trade("unknown-fee-fill", maker, size="20").model_copy(update={"fee_rate_bps": None})
    trades = (fill,) if uncertainty == "fee" else ()

    class UnknownFeePublic(sdk._SDKPublicClient):
        def get_market(self, *, id):
            market = super().get_market(id=id)
            return market.model_copy(update={"trading": market.trading.model_copy(update={
                "fees_enabled": None, "fee_schedule": None,
            })})

    store, adapter, account, lp, execution = runtime(
        trades=trades, positions=(_position(),), public_client=UnknownFeePublic(NOW),
    )
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution)
    _advance(runtime)
    execution.refresh_lp_dashboard_snapshot()
    state = execution.lp_auto_state()
    assert state["funds"]["status"] == "unknown", state
    assert state["funds"]["available_usd"] is None
    assert state["funds"]["spendable_usd"] is None
    assert _amount(state, "buy_reserved_usd") >= 16
    for original in originals:
        assert not store.lp_session(original["session_id"]).get("reservation_coverage")
    assert account.posts == account.cancels == []


def test_late_order_repeated_snapshot_and_restart_do_not_restore_covered_holds(runtime):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution)
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    assert _amount(execution.lp_auto_state(), "buy_reserved_usd") == 0
    markers = {i["session_id"]: store.lp_session(i["session_id"])["reservation_coverage"] for i in originals}
    adapter.close()

    order = _open_order("late-exchange-buy", "BUY", price="0.40", original="20")
    store, adapter, account, lp, execution = runtime(orders=(order,))
    _advance(runtime)
    for _ in range(2):
        assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
        state = execution.lp_auto_state()
        assert _amount(state, "buy_reserved_usd") == 8
        assert state["slots"]["occupied"] == 1
        assert len(state["intents"]) == 2
        for i in originals:
            assert store.lp_session(i["session_id"])["reservation_coverage"] == markers[i["session_id"]]
        _assert_unknown_audit(store, execution, originals)
    assert account.posts == account.cancels == []


def test_real_sdk_post_timeout_is_covered_without_changing_206_audit(runtime):
    """Exercise real entry validation and send-boundary writes before repair."""
    from polymarket.models.clob import SignedOrder

    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})

    def signed_order(**kwargs):
        return SignedOrder(
            builder="0x1", expiration=kwargs["expiration"], maker="0x2", maker_amount=1,
            metadata="0x3", order_type="GTD", salt=1, side="BUY", signature="0x4",
            signature_type=0, signer="0x5", taker_amount=1, timestamp=1,
            token_id=str(kwargs["token_id"]), post_only=True,
        )

    def timed_out_post(signed):
        account.posts.append(signed)
        raise TimeoutError("offline simulated lost receipt")

    account.create_limit_order = signed_order
    account.post_order = timed_out_post
    store.lp_save_price_history(CONDITION_ID, TOKEN_ID, [], {
        "state": "known", "amplitude": Decimal(".005"), "checked_at": NOW,
        "valid_until": NOW + timedelta(days=1),
    })
    result = execution.lp_submit_entry({
        "idempotency_key": "lp-auto:phase-round:0", "market_id": "market-1",
        "condition_id": CONDITION_ID, "token_id": TOKEN_ID, "outcome": "YES",
        "price": "0.39", "quantity": "20", "review_at": NOW + timedelta(minutes=10),
    })
    assert result["state"] == "needs_attention", result
    sid = result["session_id"]
    session = store.lp_session(sid)
    action = store.lp_actions(sid)[0]
    assert session["submit_stage"] == action["submit_stage"] == "send_unknown"
    assert session["post_started"] is action["post_started"] is True
    assert session["submit_finished_at"] == action["submit_finished_at"]
    assert session["submit_post_started_at"] == action["submit_post_started_at"]
    assert session["submit_status"] == action["state"] == "unknown"
    assert session["entry_order_id"] is None
    assert len(account.posts) == 1

    # Bind the already persisted request to its original durable pool intent.
    # The session and action themselves were produced by the real public send.
    intent = {
        "intent_id": "phase-round:0", "session_id": sid, "order_id": None,
        "config_version": 1, "state": "unknown", "reserved_usd": "7.80",
        "inventory_cost_usd": "0", "realized_pnl_usd": "0", "financial_status": "unknown",
        "created_at": NOW.isoformat(), "checked_at": NOW.isoformat(),
        **{key: session[key] for key in ("market_id", "condition_id", "token_id", "outcome", "price", "quantity")},
    }
    execution._auto_pool._update(lambda doc: doc["intents"].update({intent["intent_id"]: intent}))
    assert _amount(execution.lp_auto_state(), "buy_reserved_usd") == Decimal("7.80")
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    state = execution.lp_auto_state()
    assert state["funds"]["status"] == "known", state
    assert _amount(state, "buy_reserved_usd") == 0
    assert state["slots"]["occupied"] == 0
    assert store.lp_actions(sid)[0] == action
    _assert_unknown_audit(store, execution, [intent])
    assert lp._candidate_reservations() == ()
    assert len(account.posts) == 1
    assert account.cancels == []


@pytest.mark.parametrize("fence", ["stale", "generation-changed"])
def test_expired_account_proof_blocks_spending_without_restoring_old_holds(runtime, fence):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution)
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    assert _amount(execution.lp_auto_state(), "buy_reserved_usd") == 0
    if fence == "stale":
        _advance(runtime, 61)
    else:
        assert store.lp_advance_trade_generation(store.lp_trade_generation()) is True
    fenced = execution.lp_auto_state()
    assert fenced["funds"]["status"] == "unknown"
    assert fenced["funds"]["spendable_usd"] is None
    assert fenced["funds"]["available_usd"] is None
    assert _amount(fenced, "buy_reserved_usd") == 0
    assert fenced["slots"]["occupied"] == 0
    assert lp._candidate_reservations() == ()
    _assert_unknown_audit(store, execution, originals)
    assert account.posts == account.cancels == []


def test_partial_buy_counts_remaining_reserve_and_filled_inventory_once(runtime):
    order = _open_order("partial-buy", "BUY", price="0.40", original="20", matched="15")
    fill = _trade("partial-fill", _maker_order("partial-buy", "BUY", "15", "0.40"), size="15")
    store, adapter, account, lp, execution = runtime(orders=(order,), trades=(fill,), positions=(_position("15"),))
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution)
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    state = execution.lp_auto_state()
    assert state["funds"]["status"] == "known", state
    assert _amount(state, "buy_reserved_usd") == 2
    assert _amount(state, "inventory_cost_usd") == 6
    assert _amount(state, "available_usd") == 92
    assert state["slots"]["occupied"] == 1
    _assert_unknown_audit(store, execution, originals)
    assert account.posts == account.cancels == []


def test_unknown_cancel_missing_from_open_list_retains_slot_until_exact_terminal(runtime):
    from open_trader.polymarket_trading import PolymarketTradingError

    order = _open_order("cancel-buy", "BUY", price="0.40", original="20")
    store, adapter, account, lp, execution = runtime(orders=(order,))
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    sid = store.lp_active_sessions()[0]["session_id"]

    def lost_cancel_response(*, order_ids):
        account.cancels.append(order_ids)
        account.orders = ()
        raise TimeoutError("offline lost cancel receipt")

    account.cancel_orders = lost_cancel_response
    with pytest.raises(PolymarketTradingError):
        execution.lp_cancel_orders({"confirm": True, "order_ids": ["cancel-buy"]})
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    uncertain = execution.lp_auto_state()
    assert uncertain["funds"]["status"] == "unknown", uncertain
    assert uncertain["funds"]["available_usd"] is None
    assert _amount(uncertain, "buy_reserved_usd") == 8
    assert uncertain["slots"]["occupied"] == uncertain["slots"]["canceling"] == 1
    assert "cancel_terminal_unknown" in uncertain["block_reasons"]

    receipt_reads = []

    def exact_terminal(*, order_id):
        receipt_reads.append(order_id)
        assert order_id == "cancel-buy"
        return _open_order(order_id, "BUY", price="0.40", original="20", status="CANCELED")

    account.get_order = exact_terminal
    lp.reconcile_facts(sid)
    assert receipt_reads == ["cancel-buy"]
    assert store.lp_session(sid)["order_history"]["cancel-buy"]["status"] == "CANCELED"
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    recovered = execution.lp_auto_state()
    assert recovered["funds"]["status"] == "known", recovered["block_reasons"]
    assert _amount(recovered, "buy_reserved_usd") == 0
    assert recovered["slots"]["occupied"] == 0
    assert account.posts == []
    assert account.cancels == [("cancel-buy",)]


def test_enabled_round_refills_covered_legacy_market_once_without_overspending(runtime):
    from polymarket.models.clob import SignedOrder
    store, adapter, account, lp, execution = runtime(public_client=_CandidateSDKPublic(NOW))
    execution.lp_auto_configure({"budget_usd": "8", "target_buy_count": 1})
    originals = _seed_unknowns(store, execution, count=1)
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    facts = lp._read_candidate_facts({"condition_id": CONDITION_ID, "token_id": TOKEN_ID, "outcome": "YES"})
    from open_trader.polymarket_lp_risk import evaluate_lp_entry
    evaluated = evaluate_lp_entry(facts["direction"], account=facts["account"], now=lp._now(),
                                  reservations=lp._candidate_reservations(), candidate=True)
    assert evaluated["state"] == "eligible", evaluated
    assert not execution._auto_pool._excluded(CONDITION_ID), store.lp_session(originals[0]["session_id"])
    lp._candidate_pool_record_success(CONDITION_ID, {"condition_id": CONDITION_ID}, judged_at=lp._now(),
        facts={"directions": [facts["direction"]], "account": facts["account"]})
    store.lp_save_price_history(CONDITION_ID, TOKEN_ID, [], {
        "state": "known", "amplitude": Decimal(".005"), "checked_at": lp._now(),
        "valid_until": lp._now() + timedelta(days=1),
    })

    def signed_order(**kwargs):
        assert kwargs["price"] == Decimal(".40")
        assert kwargs["size"] == Decimal("20")
        _advance(runtime)  # Signing precedes the next generation-fenced account read.
        return SignedOrder(
            builder="0x1", expiration=kwargs["expiration"], maker="0x2", maker_amount=8000000,
            metadata="0x3", order_type="GTD", salt=1, side="BUY", signature="0x4",
            signature_type=0, signer="0x5", taker_amount=20000000, timestamp=1,
            token_id=str(kwargs["token_id"]), post_only=True,
        )

    def accepted_post(signed):
        _advance(runtime)
        account.posts.append(signed)
        assert len(account.posts) == 1, "Refill exceeded the one-order budget"
        account.orders = (_open_order("refill-buy", "BUY", price="0.40", original="20"),)
        return {"order_id": "refill-buy", "status": "LIVE", "accepted": True, "size_matched": "0"}

    account.create_limit_order = signed_order
    account.post_order = accepted_post
    execution.lp_auto_set_desired_running(True)
    first = execution.lp_auto_run_once(round_id="refill-covered")
    assert len(account.posts) == 1, {"last_round": first["last_round"], "blocks": first["block_reasons"]}
    assert _amount(first, "buy_reserved_usd") == 8
    assert first["slots"]["occupied"] == 1
    _advance(runtime)
    second = execution.lp_auto_run_once(round_id="refill-repeat")
    assert len(account.posts) == 1
    assert _amount(second, "buy_reserved_usd") == 8
    assert _amount(second, "available_usd") == 0
    assert second["slots"]["occupied"] == 1
    assert len(second["intents"]) == 2
    _assert_unknown_audit(store, execution, originals)
    assert account.cancels == []


@pytest.mark.parametrize("independent_unknown", [None, "passive_exit", "protected_exit"],
                         ids=["covered-entry-can-exit", "unknown-passive-blocks", "unknown-protected-blocks"])
def test_covered_entry_audit_does_not_block_actual_inventory_but_unknown_sell_does(runtime, independent_unknown):
    from polymarket.models.clob import SignedOrder

    fill = _trade("joined-fill", _maker_order("joined-buy", "BUY", "20", "0.40"), size="20")
    store, adapter, account, lp, execution = runtime(trades=(fill,), positions=(_position(),))
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution, count=1, explicit_account=True)
    sid = originals[0]["session_id"]
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    assert len(store.lp_sessions()) == 1, "The actual account order must join its existing token group"
    joined = store.lp_session(sid)
    assert joined["owned_order_ids"] == ["joined-buy"]
    # The session field is the management role established by issue #207.
    # It does not identify the old request, whose intent and action stay ID-less.
    assert joined["entry_order_id"] == "joined-buy"
    assert execution._auto_pool._read()["intents"][originals[0]["intent_id"]]["order_id"] is None
    assert joined["reservation_coverage"]["state"] == "covered"
    assert _amount(execution.lp_auto_state(), "inventory_cost_usd") == 8
    assert execution.lp_auto_state()["slots"]["occupied"] == 0

    account.get_order = lambda *, order_id: _open_order(
        order_id, "BUY", price="0.40", original="20", matched="20", status="FILLED",
    )
    signed_calls = []

    def signed_sell(**kwargs):
        signed_calls.append(kwargs)
        assert kwargs["side"] == "SELL"
        assert kwargs["size"] == Decimal("20")
        _advance(runtime)
        return SignedOrder(
            builder="0x1", expiration=kwargs["expiration"] or 0, maker="0x2", maker_amount=20000000,
            metadata="0x3", order_type="GTC" if kwargs["expiration"] is None else "GTD", salt=1,
            side="SELL", signature="0x4", signature_type=0, signer="0x5", taker_amount=8200000,
            timestamp=1, token_id=str(kwargs["token_id"]), post_only=True,
        )

    def accepted_sell(signed):
        _advance(runtime)
        account.posts.append(signed)
        account.orders = (_open_order("managed-exit", "SELL", price="0.41", original="20"),)
        return {"order_id": "managed-exit", "status": "LIVE", "accepted": True, "size_matched": "0"}

    account.create_limit_order = signed_sell
    account.post_order = accepted_sell
    if independent_unknown is not None:
        store.lp_update_session(sid, patch={f"{independent_unknown}_attempt_state": "unknown"})
        store.lp_upsert_action(sid, f"{sid}:{independent_unknown}-submit:lost", state="unknown", payload={
            "role": independent_unknown, "side": "SELL", "token_id": TOKEN_ID,
            "quantity": "20", "submit_finished_at": lp._now().isoformat(),
        })
    _advance(runtime)
    execution.lp_tick()
    # Tick starts its public market read without blocking. Wait for that
    # observable job, then let the next tick consume the completed book.
    with adapter._lp_public_reads_lock:
        public_jobs = tuple(adapter._lp_public_reads.values())
    assert public_jobs, "Expected the tick's first nonblocking market read"
    for job in public_jobs:
        job.result(timeout=10)
    _advance(runtime)
    execution.lp_tick()
    if independent_unknown is None:
        assert len(account.posts) == len(signed_calls) == 1, [(key, store.lp_session(sid).get(key)) for key in (
            "state", "resume_state", "reconciliation", "facts_error", "submit_status", "buy_filled_quantity",
            "residual_quantity", "orders_terminal", "position_reconciled", "fee_status", "financial_block_reason",
        )]
        assert store.lp_session(sid)["passive_exit_order_id"] == "managed-exit"
        _advance(runtime)
        execution.lp_tick()
        assert len(account.posts) == 1, "The already managed SELL must not be duplicated"
    else:
        assert signed_calls == account.posts == account.market_orders == []
        assert store.lp_session(sid)[f"{independent_unknown}_attempt_state"] == "unknown"
    _assert_unknown_audit(store, execution, originals, managed_order_id="joined-buy")
    assert account.cancels == []


@pytest.mark.parametrize("late_kind", ["older", "same-boundary-conflict"])
def test_late_same_generation_snapshot_waits_without_overwriting_newer_account_truth(runtime, late_kind):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    _seed_unknowns(store, execution, count=1)
    _advance(runtime)
    old = adapter.lp_account_snapshot_shared(max_age_seconds=0, trade_generation_provider=store.lp_trade_generation)
    _advance(runtime)
    account.orders = (_open_order("newer-buy", "BUY", price="0.40", original="20"),)
    newer = adapter.lp_account_snapshot_shared(max_age_seconds=0, trade_generation_provider=store.lp_trade_generation)
    assert newer["trade_generation"] == old["trade_generation"]
    assert lp.register_account_snapshot(newer)["state"] == "registered"
    before = deepcopy(execution._auto_pool._read())
    late = old if late_kind == "older" else {**newer, "open_orders": ()}
    result = lp.register_account_snapshot(late)
    assert result["state"] == "skipped", result
    assert result["reason"] == "account_round_invalid", result
    assert result["wait_reason"] == ("account_snapshot_superseded" if late_kind == "older" else "account_snapshot_conflict")
    assert execution._auto_pool._read() == before
    assert lp._account_order_sync_error is None
    state = execution.lp_auto_state()
    assert state["funds"]["status"] == "known"
    assert _amount(state, "buy_reserved_usd") == 8
    assert state["slots"]["occupied"] == 1
    assert account.posts == account.cancels == []


@pytest.mark.parametrize("reverse_input", [False, True], ids=["survivor-is-anchor", "victim-is-anchor"])
def test_real_account_same_token_overcapacity_cancels_one_id_and_waits_for_terminal(runtime, reverse_input):
    orders = (
        _open_order("rank-a", "BUY", price="0.40", original="20"),
        _open_order("rank-b", "BUY", price="0.40", original="20"),
    )
    if reverse_input:
        orders = tuple(reversed(orders))
    store, adapter, account, lp, execution = runtime(orders=orders, public_client=_CandidateSDKPublic(NOW))
    execution.lp_auto_configure({"budget_usd": "16", "target_buy_count": 1})
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    registered = store.lp_active_sessions()
    assert len(registered) == 1
    sid = registered[0]["session_id"]
    assert set(registered[0]["owned_order_ids"]) == {"rank-a", "rank-b"}
    assert set(registered[0]["order_history"]) == {"rank-a", "rank-b"}
    initial_bucket = next(iter(lp._queue_protection_levels(registered[0]).values()))
    assert initial_bucket["order_id"] == ("rank-b" if reverse_input else "rank-a")
    preserved = {key: value for key, value in initial_bucket.items()
                 if key.startswith("baseline") or key in {"threshold", "episode_id"}}

    def assert_survivor_protected():
        session = store.lp_session(sid)
        buckets = lp._queue_protection_levels(session)
        assert len(buckets) == 1
        bucket = next(iter(buckets.values()))
        assert bucket["order_id"] == "rank-a", bucket
        assert {key: bucket.get(key) for key in preserved} == preserved
        assert lp._queue_protection_gate_open(session) is True

    initial = execution.lp_auto_state()
    assert initial["slots"]["occupied"] == 2
    assert _amount(initial, "buy_reserved_usd") == 16
    assert initial["intents"] == []

    execution.lp_auto_set_desired_running(True)
    _advance(runtime)
    rotating = execution.lp_auto_run_once(round_id="real-overcapacity")
    # Same yield, condition and token: the stable exchange-ID tie-break keeps A.
    assert account.cancels == [("rank-b",)], rotating["last_round"]
    assert rotating["slots"]["occupied"] == 2
    assert rotating["slots"]["canceling"] == 1
    assert _amount(rotating, "buy_reserved_usd") == 16
    assert account.posts == account.market_orders == []
    assert rotating["intents"] == []
    assert_survivor_protected()

    # Cancellation acknowledgment plus absence is not an exact terminal fact.
    account.orders = tuple(order for order in orders if order.id == "rank-a")
    _advance(runtime)
    absent = execution.lp_auto_run_once(round_id="cancel-absent")
    assert absent["slots"]["occupied"] == 2, absent
    assert absent["slots"]["canceling"] == 1
    assert _amount(absent, "buy_reserved_usd") == 16
    assert absent["funds"]["available_usd"] is None
    assert "cancel_terminal_unknown" in absent["block_reasons"]
    assert account.cancels == [("rank-b",)]
    assert account.posts == account.market_orders == []
    assert absent["intents"] == []
    assert_survivor_protected()

    receipt_reads = []

    def canceled_receipt(*, order_id):
        receipt_reads.append(order_id)
        assert order_id == "rank-b"
        return _open_order(order_id, "BUY", price="0.40", original="20", status="CANCELED")

    account.get_order = canceled_receipt
    lp.reconcile_facts(sid)
    assert receipt_reads == ["rank-b"]
    assert store.lp_session(sid)["order_history"]["rank-b"]["status"] == "CANCELED"
    _advance(runtime)
    settled = execution.lp_auto_run_once(round_id="exact-terminal")
    assert settled["slots"]["occupied"] == 1
    assert settled["slots"]["canceling"] == 0
    assert _amount(settled, "buy_reserved_usd") == 8
    assert _amount(settled, "inventory_cost_usd") == 0
    assert _amount(settled, "available_usd") == 8
    assert settled["funds"]["status"] == "known"
    assert settled["intents"] == []
    assert_survivor_protected()
    assert account.cancels == [("rank-b",)]
    assert account.posts == account.market_orders == []
