"""A failed order import is visible and cannot authorize another BUY."""
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.test_lp_order_registration_contract import _runtime, _open_order
from tests.test_lp_auto_pool import setup, _fresh_registration_bundle


@pytest.mark.parametrize("failure", [ValueError, RuntimeError])
def test_failed_registration_keeps_dashboard_stale_until_success(tmp_path, monkeypatch, failure):
    store, adapter, account, lp, execution = _runtime(tmp_path)
    try:
        previous = execution.refresh_lp_dashboard_snapshot()
        assert previous["state"] == "ready"
        account.orders = (_open_order("new", "BUY", price="0.40", original="10"),)
        register = store.lp_register_exchange_orders

        def fail_registration(*args, **kwargs):
            raise failure("order_identity_conflict")

        monkeypatch.setattr(store, "lp_register_exchange_orders", fail_registration)
        adapter._lp_account_shared_cache = None
        failed = execution.refresh_lp_dashboard_snapshot()
        assert failed["stale"] is True
        assert failed["state"] != "ready"
        assert failed["reason"] == "account_order_sync_unknown"
        assert failed["checked_at"] == previous["checked_at"]
        assert failed["last_success_at"] == previous["last_success_at"]
        assert failed["orders"] == previous["orders"]
        assert "account_order_sync_unknown" in execution.lp_auto_state()["admission_block_reasons"]
        assert account.posts == []

        monkeypatch.setattr(store, "lp_register_exchange_orders", register)
        adapter._lp_account_shared_cache = None
        recovered = execution.refresh_lp_dashboard_snapshot()
        assert recovered["state"] == "ready"
        assert recovered["stale"] is False
        assert store.lp_active_sessions()[0]["owned_order_ids"] == ["new"]
        assert "account_order_sync_unknown" not in execution.lp_auto_state()["admission_block_reasons"]
    finally:
        adapter.close()


@pytest.mark.parametrize("during_sign", [False, True])
def test_failed_registration_blocks_auto_buy_before_or_during_sign(tmp_path, during_sign):
    execution, exchange, lp, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd="100", target_buy_count=1))
    execution.lp_auto_set_desired_running(True)

    def incomplete_registration():
        snapshot = _fresh_registration_bundle(exchange, lp)
        snapshot["pagination_complete"] = False
        assert lp.register_account_snapshot(snapshot)["state"] == "skipped"

    if during_sign:
        exchange.before_sign = incomplete_registration
    else:
        incomplete_registration()
    blocked = execution.lp_auto_run_once()
    assert exchange.posts == []
    assert "account_order_sync_unknown" in blocked["admission_block_reasons"]
    assert blocked["slots"]["occupied"] == 0
    assert Decimal(blocked["funds"]["buy_reserved_usd"]) == 0

    exchange.before_sign = None
    # Recovery and the subsequent send each need a new complete, fenced
    # account round. The old test adapter had only an unfenced display read.
    now = [lp._now()]
    lp.clock = lambda: now[0]
    def complete_account_round(*, max_age_seconds=0, trade_generation_provider=None):
        del max_age_seconds
        now[0] += timedelta(microseconds=1)
        snapshot = _fresh_registration_bundle(exchange, lp)
        snapshot.update(read_started_at=now[0], read_ended_at=now[0], checked_at=now[0])
        if trade_generation_provider is not None:
            snapshot['trade_generation'] = trade_generation_provider()
        return snapshot
    exchange.lp_account_snapshot_shared = complete_account_round
    assert lp.register_account_snapshot(complete_account_round())["state"] == "registered"
    execution.lp_auto_run_once()
    assert len(exchange.posts) == 1
