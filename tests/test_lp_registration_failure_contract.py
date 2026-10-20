"""A failed order import is visible and cannot authorize another BUY."""

from tests.test_lp_auto_pool import advance_auto_wait
from datetime import datetime, timedelta
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
def test_financial_registration_failure_fences_planning_but_preserves_approved_send(tmp_path, monkeypatch, during_sign):
    from collections import Counter
    from dataclasses import asdict, is_dataclass
    from pathlib import Path
    import json
    _diagnostic_completed = False
    try:
        from tests import test_lp_auto_pool as pool
        monkeypatch.setattr(pool,'NOW',pool.NOW)
        execution, exchange, lp, store = setup(tmp_path)
        execution.lp_auto_configure(dict(budget_usd="100", target_buy_count=1))
        execution.lp_auto_set_desired_running(True)
        captured = []
        def incomplete_registration():
            if during_sign:
                captured.append(execution.lp_auto_state())
            snapshot = _fresh_registration_bundle(exchange, lp)
            snapshot["pagination_complete"] = False
            assert lp.register_account_snapshot(snapshot)["state"] == "skipped"
        if during_sign:
            exchange.before_sign = incomplete_registration
        else:
            incomplete_registration()
        result = execution.lp_auto_run_once()
        assert "account_order_sync_unknown" in result["admission_block_reasons"]
        if during_sign:
            assert len(captured) == 1 and captured[0]['active_plan']
            original_plan = captured[0]['active_plan']
            action, = result['last_round']['actions']
            assert action['action_id'] == original_plan['actions'][0]['action_id']
            assert result['last_round']['targets'] == captured[0]['last_round']['targets']
            assert action['state'] == 'success' and action['request_state'] == 'entry_open'
            session = store.lp_session(action['session_id'])
            assert session['idempotency_key'] == 'lp-auto:' + action['action_id']
            assert session['state'] == 'entry_open' and session['entry_order_id'] == action['order_id']
            audit, = store.lp_actions(action['session_id'])
            assert audit['state'] == 'accepted' and audit['post_started'] is True
            assert 'attempts' not in action and action.get('request_id', action['action_id']) == action['action_id']
            assert len(exchange.posts) == 1 and exchange.posts[0]['token_id'] == 'm00'
            assert exchange.posts[0]['price'] == Decimal('.40') and exchange.posts[0]['quantity'] == 20
            assert result['slots']['occupied'] == 1 and result['funds']['status'] == 'unknown'
            assert result['last_round']['completed_at'] and result['plan_wait'] is None
            execution.lp_auto_run_once()
            from tests.test_lp_auto_plan_scheduler import restart_engine
            restarted = restart_engine(execution, exchange)
            restarted.lp_auto_run_once()
            assert len(exchange.posts) == 1 and store.lp_actions(action['session_id']) == [audit]
        else:
            assert exchange.posts == []
            assert result['slots']['occupied'] == 0 and Decimal(result['funds']['buy_reserved_usd']) == 0
            assert result['plan_wait'] is None and result['last_round']['completed_at']
            assert result['active_plan'] is None and result['last_round']['actions'] == []
        exchange.before_sign = None
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
        assert "account_order_sync_unknown" not in execution.lp_auto_state()['admission_block_reasons']
        if during_sign:
            assert execution.lp_auto_state()['funds']['status'] == 'known'
            assert store.lp_actions(action['session_id']) == [audit] and len(exchange.posts) == 1
            _diagnostic_completed = True
            return
        now[0] += timedelta(microseconds=1)
        advance_auto_wait(execution, monkeypatch)
        recovered = execution.lp_auto_run_once()
        assert recovered['last_round']['round_id'] != result['last_round']['round_id']
        assert len(exchange.posts) == 1 and exchange.posts[0]['token_id'] == 'm00'
        assert exchange.posts[0]['price'] == Decimal('.40') and exchange.posts[0]['quantity'] == 20
        assert recovered['last_round']['completed_at'] and recovered['plan_wait'] is None
        execution.lp_auto_run_once()
        assert len(exchange.posts) == 1
        _diagnostic_completed = True
    finally:
        _values = dict(locals())
        _store = _values.get('store')
        _service = _values.get('engine',_values.get('execution'))
        _external = _values.get('exchange',_values.get('account'))
        if _store is not None:
            _record = dict(completed=_diagnostic_completed,
                states={k:v for k,v in _values.items() if k in ('state','result','recovered','captured','publications','observation','plans','first','old_round','old_audits','reads','counts','failures','failed_at','observations','source','requests','original','audit','published','saved','signed','history_calls','baseline','actions','terms')},
                state=_service.lp_auto_state(),sessions=_store.lp_sessions(),
                audits={s['session_id']:_store.lp_actions(s['session_id']) for s in _store.lp_sessions()},
                posts=_external.posts,cancels=getattr(_external,'cancels',[]),orders=_external.orders,
                external_counts={k:getattr(_external,k,None) for k in ('balance_reads','order_reads','trade_reads','position_reads')})
            def _serialize(value):
                if is_dataclass(value):return asdict(value)
                if hasattr(value,'model_dump'):return value.model_dump(mode='json')
                return str(value)
            (Path(_store.path).parent/('final-direct25-'+'test_financial_registration_failure_fences_planning_but_preserves_approved_send'+'.json')).write_text(json.dumps(_record,indent=2,default=_serialize)+'\n')
