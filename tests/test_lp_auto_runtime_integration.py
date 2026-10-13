"""#196 runs the actual #195 pool, SQLite state and LP order lifecycle."""

from tests.test_lp_auto_pool import advance_auto_wait

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
import threading
import sqlite3

import pytest

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_lp_scheduler import LPAutoScheduler
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests import test_lp_auto_pool as pool


def restart(tmp_path, exchange, old_lp):
    store = PredictionArbitrageStore(tmp_path / "state.sqlite")
    lp = PolymarketLPService(store, exchange, clock=lambda: pool.NOW)
    lp._candidate_pool = deepcopy(old_lp._candidate_pool)
    lp._candidate_qualification_facts = deepcopy(old_lp._candidate_qualification_facts)
    execution = PredictionExecutionService(
        store=store, monitor=SimpleNamespace(), trading=exchange,
        notifier=SimpleNamespace(), lock_path=tmp_path / "execution.lock", lp=lp,
    )
    execution._breaker_open = False  # Simulated clean startup; venue reads remain real core gates.
    return execution, lp


def refresh_candidates(lp, exchange):
    for condition in lp._candidate_qualification_facts:
        lp._candidate_pool_record_success(
            condition, {"condition_id": condition}, judged_at=pool.NOW,
            facts={"directions": [exchange.direction(condition)],
                   "account": exchange.lp_account_snapshot()},
        )


@pytest.mark.parametrize("paused", [False, True])
def test_restart_reconciles_before_refill_and_preserves_manual_intent(tmp_path, monkeypatch, paused):
    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 2})
    # Startup isolates a failed read of the first order while its new sibling
    # can use fresh account facts and the remaining conservative allocation.
    second = lp._candidate_qualification_facts.pop("m01")
    execution.lp_auto_set_desired_running(True)
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    assert len(exchange.posts) == 1
    if paused:
        execution.lp_auto_set_desired_running(False)
    lp._candidate_qualification_facts["m01"] = second
    restored, restored_lp = restart(tmp_path, exchange, lp)
    read_snapshot = exchange.lp_snapshot

    def unavailable(request):
        raise OSError("account unavailable after restart")

    exchange.lp_snapshot = unavailable
    after_restart = LPAutoScheduler(restored, clock=lambda: pool.NOW)
    advance_auto_wait(restored, monkeypatch)
    assert after_restart.run_due()
    assert len(exchange.posts) == (1 if paused else 2)
    assert restored.lp_auto_state()["desired_running"] is (not paused)
    assert restored.lp_auto_state()["block_reasons"]
    exchange.lp_snapshot = read_snapshot
    monkeypatch.setattr(pool, "NOW", pool.NOW + timedelta(seconds=60))
    refresh_candidates(restored_lp, exchange)
    advance_auto_wait(restored, monkeypatch)
    assert after_restart.run_due()
    assert len(exchange.posts) == (1 if paused else 2)
    assert restored.lp_auto_state()["run_id"] == execution.lp_auto_state()["run_id"]
    if paused:
        restored.lp_auto_set_desired_running(True)
        after_restart.request_check()
        advance_auto_wait(restored, monkeypatch)
        assert after_restart.run_due()
        assert len(exchange.posts) == 2


def test_pause_during_reconciliation_survives_late_result(tmp_path, monkeypatch):
    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 2})
    second = lp._candidate_qualification_facts.pop("m01")
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    lp._candidate_qualification_facts["m01"] = second
    entered, release = threading.Event(), threading.Event()
    read_snapshot = exchange.lp_snapshot

    def delayed(request):
        entered.set()
        assert release.wait(3)
        return read_snapshot(request)

    exchange.lp_snapshot = delayed
    advance_auto_wait(execution, monkeypatch)
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    worker = threading.Thread(target=scheduler.run_due)
    worker.start()
    try:
        assert entered.wait(3)
        assert execution.lp_auto_set_desired_running(False)["pause_confirmed"] is True
        with pytest.raises(ValueError):
            execution.lp_auto_configure({"budget_usd": "0", "target_buy_count": 0})
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert len(exchange.posts) == 1
    assert execution.lp_auto_state()["desired_running"] is False


def test_midnight_gtd_termination_refills_from_fresh_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "NOW", datetime(2026, 9, 27, 15, 59, 30, tzinfo=UTC))
    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    execution.lp_auto_configure({"budget_usd": "8", "target_buy_count": 1})
    assert not exchange.posts
    execution.lp_auto_set_desired_running(True)
    scheduler.request_check()
    advance_auto_wait(execution, monkeypatch)
    assert scheduler.run_due()
    assert len(exchange.posts) == 1
    identity = execution.lp_auto_state()["run_id"]
    exchange.orders[0]["status"] = "EXPIRED"
    monkeypatch.setattr(pool, "NOW", pool.NOW + timedelta(seconds=60))
    # The next pool has a different eligible market; no old queue is replayed.
    lp._candidate_qualification_facts.pop("m00")
    refresh_candidates(lp, exchange)
    execution.lp_tick()
    assert scheduler.run_due()
    assert len(exchange.posts) == 2
    assert exchange.posts[-1]["token_id"] == "m01"
    state = execution.lp_auto_state()
    assert state["run_id"] == identity
    assert state["desired_running"] is True
    assert state["slots"]["occupied"] == 1


def test_terminal_pause_confirmation_blocks_unsigned_remainder_and_keeps_buy(tmp_path, monkeypatch, capsys):
    from open_trader import cli
    from tests.test_lp_auto_control import runtime_for
    from tests.test_prediction_service import _server

    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 2})
    second = lp._candidate_qualification_facts.pop("m01")
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    lp._candidate_qualification_facts["m01"] = second
    entered, release = threading.Event(), threading.Event()

    def before_sign():
        entered.set()
        assert release.wait(3)

    exchange.before_sign = before_sign
    advance_auto_wait(execution, monkeypatch)
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    runtime = runtime_for(tmp_path)
    runtime.execution = execution
    runtime._lp_auto_scheduler = scheduler
    with _server(runtime) as base:
        worker = threading.Thread(target=scheduler.run_due)
        worker.start()
        try:
            assert entered.wait(3)
            assert cli.main(["prediction-arb", "lp-auto", "pause", "--url", base]) == 0
            assert "PAUSED" in capsys.readouterr().out
        finally:
            release.set()
            worker.join(3)
    assert not worker.is_alive()
    assert len(exchange.posts) == 1
    assert exchange.orders[0]["status"] == "LIVE"
    assert execution.lp_auto_state()["desired_running"] is False


@pytest.mark.parametrize("pause_while_blocked", [False, True])
def test_durable_receipt_reconciles_after_interrupted_submit_without_overriding_pause(tmp_path, monkeypatch, pause_while_blocked):
    import json
    import uuid
    from collections import Counter
    from tests.test_lp_auto_plan_execution import install_order_result_reader

    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    result_reader = install_order_result_reader(exchange, monkeypatch)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 2})
    execution.lp_auto_set_desired_running(True)
    current, phase = [execution], ['original_local_attempts']
    evidence = dict(parameter=pause_while_blocked,faults=[],states=[],result_packets=[],full_snapshot_reads=[],financial_reads=[])
    def document():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        engine=current[0];state=deepcopy(engine.lp_auto_state())
        evidence['states'].append(dict(label=label,state=state,document=document(),sessions=engine._store.lp_sessions(),
            audits={s['session_id']:engine._store.lp_actions(s['session_id']) for s in engine._store.lp_sessions()},
            orders=deepcopy(exchange.orders),posts=deepcopy(exchange.posts)))
        return state
    account=exchange.lp_account_snapshot
    def financial():
        value=account()
        evidence['financial_reads'].append(dict(phase=phase[0],active_plan=bool(current[0].lp_auto_state()['active_plan']),at=pool.NOW))
        return value
    monkeypatch.setattr(exchange,'lp_account_snapshot',financial)
    original_snapshot=exchange.lp_snapshot
    unavailable=[False]
    def full_snapshot(request):
        row=dict(phase=phase[0],request=deepcopy(request),at=pool.NOW,unavailable=unavailable[0],
            active_plan=bool(current[0].lp_auto_state()['active_plan']))
        evidence['full_snapshot_reads'].append(row)
        if unavailable[0]:
            try:
                raise OSError("read unavailable")
            except OSError as exc:
                row['actual_error']=dict(type=type(exc).__name__,message=str(exc))
                raise
        value=original_snapshot(request);row['packet']=deepcopy(value);return value
    monkeypatch.setattr(exchange,'lp_snapshot',full_snapshot)
    def results(request):
        value=result_reader(request)
        for field in ('orders','open_orders'):
            for row in value[field]:
                if (row.get('side')=='BUY' and row.get('token_id')==row.get('condition_id')
                        and row.get('token_id') in ('m00','m01') and row.get('status')=='LIVE'
                        and row.get('fill_quantity_known') in (None,True)
                        and Decimal(str(row['price']))==Decimal('.40') and Decimal(str(row['original_size']))==20
                        and Decimal(str(row['size_matched']))==0):
                    row['fill_quantity_known']=True
        evidence['result_packets'].append(deepcopy(dict(phase=phase[0],request=request,packet=value)))
        return value
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',results)
    update = store.lp_update_session
    def fail_after_receipt(session_id, **kwargs):
        if (kwargs.get("patch") or {}).get("submit_status") == "accepted":
            observed=dict(session_id=session_id,patch=deepcopy(kwargs['patch']),session=store.lp_session(session_id),
                audits=store.lp_actions(session_id),phase=phase[0],at=pool.NOW)
            evidence['faults'].append(observed)
            try:
                raise OSError("interrupted after durable accepted action")
            except OSError as exc:
                observed['actual_error']=dict(type=type(exc).__name__,message=str(exc))
                raise
        return update(session_id, **kwargs)
    monkeypatch.setattr(store, "lp_update_session", fail_after_receipt)
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    def no_io_before(engine,checking_scheduler,state,elapsed):
        counts=(len(evidence['financial_reads']),len(evidence['full_snapshot_reads']),len(evidence['result_packets']))
        latest=deepcopy(state['last_round']);wait=deepcopy(state['plan_wait'])
        pool.NOW=datetime.fromisoformat(wait['started_at'])+timedelta(seconds=elapsed)
        assert pool.NOW<datetime.fromisoformat(wait['deadline'])
        checking_scheduler.request_check();checking_scheduler.run_due();engine.lp_auto_run_once()
        assert counts==(len(evidence['financial_reads']),len(evidence['full_snapshot_reads']),len(evidence['result_packets']))
        assert engine.lp_auto_state()['last_round']==latest and engine.lp_auto_state()['plan_wait']==wait
    def audit_immutable(engine,originals):
        for sid,row in originals.items():
            assert engine._store.lp_actions(sid)==[row]
        assert {x['session_id'] for x in engine._store.lp_sessions()}==set(originals)
    try:
        assert scheduler.run_due()
        original=capture('both_original_requests_locally_complete_with_apply_failures')
        original_round=deepcopy(original['last_round'])
        assert original['active_plan'] is None and original_round['completed_at']
        assert datetime.fromisoformat(original['plan_wait']['deadline'])==datetime.fromisoformat(original_round['completed_at'])+timedelta(seconds=30)
        assert Counter(p['token_id'] for p in exchange.posts)==Counter(['m00','m01'])
        assert all(p['side']=='BUY' and Decimal(p['price'])==Decimal('.40') and Decimal(p['quantity'])==20 and p['post_only'] is True for p in exchange.posts)
        assert len(exchange.orders)==2 and all(o['status']=='LIVE' and Decimal(o['price'])==Decimal('.40')
            and Decimal(o['original_size'])==20 and Decimal(o['size_matched'])==0 for o in exchange.orders)
        actual_ids={o['token_id']:o['order_id'] for o in exchange.orders}
        assert set(actual_ids)=={'m00','m01'} and len(set(actual_ids.values()))==2
        canonical={};audits={}
        for action in original_round['actions']:
            assert action['kind']=='buy' and action['state']=='unknown'
            assert action['error_type']=='OSError' and action['reason']=='interrupted after durable accepted action'
            assert action['request_id']==action['action_id'] and action['request_id'].startswith(original_round['round_id']+':')
            assert action['order_id']==actual_ids[action['token_id']]
            sid=action['session_id'];canonical[action['token_id']]=sid
            row,=[a for a in store.lp_actions(sid) if a.get('role')=='entry']
            assert row['state']=='accepted' and row['post_started'] is True and row['order_id']==actual_ids[action['token_id']]
            assert row['token_id']==action['token_id'] and Decimal(row['price'])==Decimal('.40') and Decimal(row['quantity'])==20
            uuid.UUID(row['action_id']);audits[sid]=deepcopy(row)
        assert set(canonical)=={'m00','m01'} and len(set(canonical.values()))==2
        assert {x['session_id'] for x in evidence['faults']}==set(canonical.values())
        assert all(x['actual_error']==dict(type='OSError',message='interrupted after durable accepted action') for x in evidence['faults'])
        assert all(store.lp_session(sid).get('submit_status') is None and store.lp_session(sid).get('entry_order_id')==actual_ids[token] for token,sid in canonical.items())
        assert original['slots']['occupied']==2 and Decimal(original['funds']['buy_reserved_usd'])==16
        evidence['original_round']=original_round;evidence['original_audits']=audits
        evidence['canonical']=canonical;evidence['actual_venue_ids']=actual_ids;evidence['scheduler_first']=scheduler.snapshot()
        phase[0]='full_cold_before_original_deadline'
        restored, restored_lp = restart(tmp_path, exchange, lp)
        current[0]=restored
        recovered = LPAutoScheduler(restored, clock=lambda: pool.NOW)
        no_io_before(restored,recovered,original,29)
        assert len(exchange.posts)==2 and {o['token_id']:o['order_id'] for o in exchange.orders}==actual_ids
        assert all(restored._store.lp_session(sid).get('submit_status') is None and restored._store.lp_session(sid).get('entry_order_id')==actual_ids[token] for token,sid in canonical.items())
        audit_immutable(restored,audits)
        phase[0]='actual_full_snapshot_failure_and_narrow_recovery'
        unavailable[0]=True
        pool.NOW=datetime.fromisoformat(original['plan_wait']['deadline'])
        result_start=len(evidence['result_packets']);full_start=len(evidence['full_snapshot_reads'])
        assert recovered.run_due()
        blocked=capture('original_metadata_recovered_independently_of_failed_full_read')
        assert any(x.get('actual_error')==dict(type='OSError',message='read unavailable') for x in evidence['full_snapshot_reads'][full_start:])
        packets=evidence['result_packets'][result_start:]
        assert packets and set(actual_ids.values())<={oid for x in packets for oid in x['request'].get('owned_order_ids',())}
        assert any(x['packet']['authenticated'] is True and x['packet']['wallet_address']=='test-wallet'
            and x['packet']['open_orders_complete'] is True and x['packet']['pagination_complete'] is True
            and {o['order_id'] for o in x['packet']['orders']}==set(actual_ids.values())
            and all(o['fill_quantity_known'] is True and o['status']=='LIVE' and Decimal(o['price'])==Decimal('.40')
                and Decimal(o['original_size'])==20 and Decimal(o['size_matched'])==0 for o in x['packet']['orders']) for x in packets)
        for token,sid in canonical.items():
            session=restored._store.lp_session(sid)
            assert session['submit_status']=='accepted' and session['entry_order_id']==actual_ids[token]
            assert session['token_id']==token and actual_ids[token] in session['owned_order_ids']
        assert blocked['active_plan'] is None and blocked['last_round']['round_id']!=original_round['round_id']
        assert blocked['block_reasons']
        assert blocked['funds']['as_of']==original['funds']['as_of']
        assert blocked['slots']['occupied']==2 and Decimal(blocked['funds']['buy_reserved_usd'])==16
        assert datetime.fromisoformat(blocked['plan_wait']['deadline'])==datetime.fromisoformat(blocked['plan_wait']['started_at'])+timedelta(seconds=30)
        assert len(exchange.posts)==2 and {o['token_id']:o['order_id'] for o in exchange.orders}==actual_ids
        audit_immutable(restored,audits)
        if pause_while_blocked:
            assert restored.lp_auto_set_desired_running(False)['pause_confirmed'] is True
        assert restored.lp_auto_state()['desired_running'] is (not pause_while_blocked)
        unavailable[0]=False
        phase[0]='restored_external_full_read_before_deadline'
        for elapsed in (1,29):
            no_io_before(restored,recovered,blocked,elapsed)
            assert len(exchange.posts)==2 and {o['token_id']:o['order_id'] for o in exchange.orders}==actual_ids
            audit_immutable(restored,audits)
        phase[0]='next_current_healthy_observation'
        pool.NOW=datetime.fromisoformat(blocked['plan_wait']['deadline'])
        refresh_candidates(restored_lp, exchange)
        full_start=len(evidence['full_snapshot_reads'])
        assert recovered.run_due()
        healthy=capture('healthy_current_two_owned_orders_pause_preserved')
        assert healthy['active_plan'] is None and not healthy['block_reasons']
        assert healthy['last_round']['actions']==[]
        assert healthy['desired_running'] is (not pause_while_blocked)
        assert healthy['slots']['occupied']==2 and Decimal(healthy['funds']['buy_reserved_usd'])==16
        assert healthy['last_round']['round_id'] not in (original_round['round_id'],blocked['last_round']['round_id'])
        assert healthy['plan_wait']['kind']=='round'
        assert datetime.fromisoformat(healthy['plan_wait']['deadline'])==datetime.fromisoformat(healthy['plan_wait']['started_at'])+timedelta(seconds=30)
        if pause_while_blocked:
            assert healthy['last_round']['actions']==[] and healthy['last_round']['checked_at']
        else:
            assert healthy['last_round']['completed_at']
        assert len(exchange.posts)==2 and all(p['side']=='BUY' for p in exchange.posts)
        assert {o['token_id']:o['order_id'] for o in exchange.orders}==actual_ids
        assert all(o['status']=='LIVE' for o in exchange.orders)
        assert not any(x.get('actual_error') for x in evidence['full_snapshot_reads'][full_start:])
        for token,sid in canonical.items():
            session=restored._store.lp_session(sid)
            assert session['submit_status']=='accepted' and session['entry_order_id']==actual_ids[token] and not session.get('facts_error')
        audit_immutable(restored,audits)
        phase[0]='healthy_full_cold_before_next_deadline'
        counts=(len(evidence['financial_reads']),len(evidence['full_snapshot_reads']),len(evidence['result_packets']))
        again,_=restart(tmp_path,exchange,restored_lp);current[0]=again
        again.lp_auto_run_once()
        assert counts==(len(evidence['financial_reads']),len(evidence['full_snapshot_reads']),len(evidence['result_packets']))
        assert again.lp_auto_state()['desired_running'] is (not pause_while_blocked)
        assert len(exchange.posts)==2 and {o['token_id']:o['order_id'] for o in exchange.orders}==actual_ids
        audit_immutable(again,audits)
        assert not any(x['active_plan'] for x in evidence['financial_reads'])
        evidence['completed']='full'
    finally:
        evidence.update(phase=phase[0],clock=pool.NOW,state=current[0].lp_auto_state(),document=document(),
            sessions=current[0]._store.lp_sessions(),audits={s['session_id']:current[0]._store.lp_actions(s['session_id']) for s in current[0]._store.lp_sessions()},
            orders=exchange.orders,posts=exchange.posts)
        (tmp_path/'RT-diagnostics.json').write_text(json.dumps(evidence,indent=2,default=str)+'\n')

def test_shadow_auto_http_reads_do_not_initialize_or_write_durable_state(tmp_path):
    from tests.test_lp_auto_control import runtime_for
    from tests.test_prediction_service import _server, _response

    execution, exchange, lp, store = pool.setup(tmp_path)
    runtime = runtime_for(tmp_path)
    runtime._mode = "shadow"
    runtime.execution, runtime.lp, runtime.store = execution, lp, store
    runtime._lp_auto_scheduler = None
    with sqlite3.connect(f"file:{store.path}?mode=ro", uri=True) as connection:
        version = connection.execute("PRAGMA data_version").fetchone()
        with _server(runtime) as base:
            for suffix in ("auto/state", "dashboard", "auto/state"):
                status, payload = _response(base + "/api/prediction-arbitrage/lp/" + suffix)
                assert status == 200
        assert connection.execute("PRAGMA data_version").fetchone() == version
        assert connection.execute("SELECT COUNT(*) FROM lp_auto_pool").fetchone()[0] == 0
    assert not exchange.posts


def test_paused_inventory_exit_continues_and_confirmed_proceeds_fund_next_buy(tmp_path, monkeypatch):
    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    execution.lp_auto_configure({"budget_usd": "8", "target_buy_count": 1})
    execution.lp_auto_set_desired_running(True)
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    scheduler.run_due()
    exchange.orders[0].update(status="FILLED", size_matched="20")
    exchange.positions = [{"token_id": "m00", "condition_id": "m00", "size": "20"}]
    execution.lp_auto_set_desired_running(False)
    execution.lp_tick()
    assert len(exchange.posts) == 2
    assert exchange.posts[-1]["side"] == "SELL"
    scheduler.request_check()
    advance_auto_wait(execution, monkeypatch)
    scheduler.run_due()
    assert Decimal(execution.lp_auto_state()["funds"]["inventory_cost_usd"]) == 8
    assert Decimal(execution.lp_auto_state()["funds"]["available_usd"]) == 0
    execution.lp_auto_set_desired_running(True)
    scheduler.request_check()
    scheduler.run_due()
    assert len(exchange.posts) == 2
    exchange.orders[-1].update(status="FILLED", size_matched="20")
    exchange.positions = []
    monkeypatch.setattr(pool, "NOW", pool.NOW + timedelta(seconds=60))
    lp._candidate_qualification_facts.pop("m00")
    refresh_candidates(lp, exchange)
    execution.lp_tick()
    scheduler.run_due()
    assert len(exchange.posts) == 3
    assert exchange.posts[-1]["side"] == "BUY"
    assert exchange.posts[-1]["token_id"] == "m01"
    state = execution.lp_auto_state()
    assert Decimal(state["funds"]["realized_pnl_usd"]) == Decimal("0.20")
    assert Decimal(state["funds"]["total_usd"]) == Decimal("8")
    assert Decimal(state["funds"]["available_usd"]) == Decimal("0")


def test_existing_lp_card_keeps_real_auto_trading_controls_without_daily_summary(tmp_path):
    import json
    from tests.test_dashboard_web import run_dashboard_js

    execution, exchange, lp, store = pool.setup(tmp_path)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1})
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    auto = execution.lp_auto_state()
    dashboard = {"auto": auto, "auto_summary": execution.lp_auto_report()}
    html = run_dashboard_js(
        "console.log(predictionLpCard({lp_dashboard:" + json.dumps(dashboard) + "}));"
    )
    assert 'data-lp-auto-action="pause"' in html
    assert "策略总资金" in html and "BUY 总预留" in html
    assert auto["intents"][0]["intent_id"] not in html
    assert auto["intents"][0]["order_id"] not in html
    assert "自动订单 / 意图" not in html
    assert "自然日汇总" not in html
