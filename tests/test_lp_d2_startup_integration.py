"""Startup and resume compose the real owners through current state and actual service ownership."""
from decimal import Decimal
import time
from threading import Event
import pytest

from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
from open_trader.polymarket_lp_market_calculation import CalculationPolicy
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_d2_integration import seed_running, settle_control
from tests.test_lp_order_registration_contract import _adapter, _open_order, WALLET, CONDITION_ID, TOKEN_ID


def test_resume_resets_unknown_timer_without_delaying_cleanup(tmp_path):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    adapter, sdk = _adapter(tmp_path, orders=(_open_order('unknown-A', 'BUY', price='.40', original='10'),))
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    clock, attempts = [0.0], []
    def failed_cancel(*, order_ids):
        attempts.append((order_ids, clock[0]))
        return dict(canceled=(), not_canceled={order_ids[0]: 'offline-unconfirmed'})
    sdk.cancel_orders = failed_cancel
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='unknown-resume', policy=CalculationPolicy(Decimal('40')), clock=lambda: clock[0])
    key = (WALLET, CONDITION_ID, TOKEN_ID, 'unknown-A')
    try:
        live.initialize(ValidatedStartAdmission('unknown-resume', 'offline-permit'))
        live.run_once()
        assert live.state.snapshot().yields[key].unknown_since_monotonic == 0
        clock[0] = 500
        live.run_once()
        assert live.state.snapshot().yields[key].unknown_since_monotonic == 0
        live.request_running(False)
        settle_control(live)
        live.request_running(True)
        settle_control(live)
        assert live.rounds.snapshot().round_number == 0
        assert live.state.snapshot().yields[key].unknown_since_monotonic is None
        result = live.run_once()
        assert result.outcomes[0].status.value == 'rejected'
        assert attempts == [(('unknown-A',), 0), (('unknown-A',), 500)]
        assert live.state.snapshot().yields[key].unknown_since_monotonic == 500
        assert live.state.snapshot().buy_reserved.value == Decimal('4.00')
        assert next(o for o in live.state.snapshot().active_orders if o.key == key).remaining.value == 10
        clock[0] = 1099
        assert live.state.snapshot().unknown_yield_due == ()
        clock[0] = 1100
        assert live.state.snapshot().unknown_yield_due == (key,)
        assert sdk.posts == []
    finally:
        assert live.close(timeout=3)
        adapter.close()


@pytest.mark.parametrize('response_timing', ['completed_before_resume', 'response_after_resume'])
def test_resume_clears_old_provider_wait_but_new_limit_applies(tmp_path, response_timing):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    adapter, sdk = _adapter(tmp_path)
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    clock, calls, limit = [0.0], [], [60]
    entered, release, returned = Event(), Event(), Event()
    original_balance = sdk.get_balance_allowance
    class Limited(Exception):
        @property
        def retry_after_seconds(self): return limit[0]
    def balance(**kwargs):
        calls.append(clock[0])
        if response_timing == 'response_after_resume':
            if len(calls) > 1:
                return original_balance(**kwargs)
            entered.set()
            assert release.wait(5), 'native balance response release watchdog'
            returned.set()
        raise Limited()
    sdk.get_balance_allowance = balance
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='limit-resume', policy=CalculationPolicy(Decimal('40')), clock=lambda: clock[0])
    try:
        live.initialize(ValidatedStartAdmission('limit-resume', 'offline-permit'))
        if response_timing == 'response_after_resume':
            before = live.run_once(timeout=.05)
            assert entered.wait(2)
            assert calls == [0]
            assert before.acceptance.observation.source_frame.sources['balance'].error == 'timeout'
            assert live.reader.drain(timeout=0) is False
            clock[0] = 20
            live.request_running(False)
            settle_control(live)
            live.request_running(True)
            settle_control(live)
            pending = live.run_once()
            assert pending.acceptance.observation.source_frame.sources['balance'].error == 'in_progress'
            assert calls == [0] and not returned.is_set()
            assert live.reader.drain(timeout=0) is False
            clock[0], limit[0] = 25, 120
            release.set()
            assert returned.wait(2)
            assert live.reader.drain(timeout=2) is True  # actual response publication completed
            clock[0] = 26
            assert live.request_running(True).status == 'noop'
            ordinary = live.run_once()
            assert ordinary.acceptance.observation.source_frame.sources['balance'].retry_at == 145
            assert calls == [0]  # the pre-resume native job supplied a NEW limit
            clock[0] = 145
            allowed = live.run_once()
            assert calls == [0, 145]
            assert allowed.acceptance.observation.source_frame.sources['balance'].complete
            return
        before = live.run_once()
        assert before.acceptance.observation.source_frame.sources['balance'].retry_at == 60
        assert calls == [0]
        clock[0] = 20
        live.request_running(False)
        settle_control(live)
        live.request_running(True)
        settle_control(live)
        limit[0] = 120
        fresh = live.run_once()
        assert calls == [0, 20]
        assert fresh.acceptance.observation.source_frame.sources['balance'].retry_at == 140
        count = sdk.order_reads
        clock[0] = 21
        assert live.request_running(True).status == 'noop'
        ordinary = live.run_once()
        assert calls == [0, 20] and sdk.order_reads > count
        assert ordinary.acceptance.observation.source_frame.sources['balance'].retry_at == 140
        assert before.acceptance.observation.source_frame.sources['balance'].retry_at == 60
    finally:
        release.set()
        assert live.close(timeout=3)
        adapter.close()


@pytest.mark.parametrize('saved_state', ['paused', 'running', 'foreign'])
def test_existing_pool_control_identity_load_save_preserves_pause(tmp_path, saved_state):
    import json
    import sqlite3
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from tests.test_lp_d2_integration import settle_control
    pool_key = '30ffa9b5e1968e54bddadba7ff14c633c7c955a6f41808d9f5633a42c07537dd'
    stored_running = saved_state == 'running'
    original = dict(account_id='f'*64 if saved_state == 'foreign' else pool_key,
        run_id='existing-d1-pool', config_version=7, desired_running=stored_running,
        ever_enabled=True, enabled_at='2026-10-01T01:02:03+00:00',
        updated_at='2026-10-02T01:02:03+00:00', budget_usd='123.45',
        target_buy_count=3, buy_price_level=2, custom_settings={'operator_note': 'keep', 'mode': 9},
        allocations=[], intents={}, events={}, rounds={}, last_round={})
    store = PredictionArbitrageStore(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute('INSERT INTO lp_auto_pool(singleton,payload) VALUES(1,?)', (json.dumps(original),))
    orders = (_open_order('paused-preserved-buy', 'BUY', price='.40', original='10'),) if saved_state == 'paused' else ()
    adapter, sdk = _adapter(tmp_path, orders=orders)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='existing-d1-control', policy=CalculationPolicy(Decimal('40')))
    def saved_document():
        with sqlite3.connect(store.path) as db:
            return json.loads(db.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    try:
        live.initialize(ValidatedStartAdmission('existing-d1-control', 'offline-permit'))
        status = live.control.status()
        if saved_state == 'foreign':
            assert status.fatal is not None and status.fatal.origin == 'startup_read'
            assert status.current.effective_running is None  # failed startup has no admitted control
            assert status.current.shutdown_requested is True
            assert (sdk.balance_reads, sdk.order_reads, sdk.position_reads) == (0, 0, 0)
            assert sdk.posts == [] and sdk.cancels == []
            assert saved_document() == original
            assert live.request_running(True).status == 'rejected'
            return
        assert status.fatal is None
        assert status.current.account_id == WALLET
        assert status.current.effective_running is stored_running
        observed = live.run_once()
        assert observed.acceptance.observation.source_frame.account == WALLET
        if not stored_running:
            assert observed.outcomes == () and live.rounds.snapshot().round_number == 0
            assert sdk.cancels == [] and sdk.posts == []
            assert live.state.snapshot().active_orders[0].key[3] == 'paused-preserved-buy'
        assert saved_document() == original
        assert live.request_running(not stored_running, actor='operator').status == 'accepted'
        saved = settle_control(live)
        assert saved.saved_version == saved.requested_version
        assert saved.current.account_id == WALLET
        assert saved.current.effective_running is (not stored_running)
        actual = saved_document()
        assert actual['account_id'] == pool_key
        assert actual['desired_running'] is (not stored_running)
        assert {k:v for k,v in actual.items() if k not in ('desired_running', 'updated_at')} == {
            k:v for k,v in original.items() if k not in ('desired_running', 'updated_at')}
    finally:
        assert live.close(timeout=3)
        adapter.close()


def test_startup_read_failure_stays_stopped_after_relaunch(tmp_path):
    """A failed claimed startup cannot turn later dependency recovery into RUN."""
    import hashlib
    import json
    from datetime import UTC, datetime
    from tests.lp_d2_service_harness import ServiceChild
    for fault in ('control', 'settings', 'sdk'):
        scenario = 'startup-' + fault + '-failure'
        with ServiceChild(tmp_path / fault, scenario=scenario) as child:
            child.release('metadata')
            child.start(expected_running=None)
            code = child.wait_exit()
            original = json.loads(child.permit_path.read_text())
            assert child.marker('actual-store-sql-' + scenario).exists()
            if fault == 'sdk':
                assert child.marker('external-sdk-init-failed').exists()
            else:
                assert child.marker('startup-' + fault + '-read-' + scenario).exists()
            print('actual failed startup', fault, code, original['state'])
            assert (code, original['state']) == (78, 'CLAIMED'), child.output.read_text()
            child.assert_owner_available()
            assert child.actual_posts() == 0
            # Only the failed external dependency changes. The real source,
            # database and original permit remain the same across processes.
            recovered = 'automatic-startup-' + fault + '-recovered'
            child.launch(recovered)
            assert child.wait_exit() == 78, child.output.read_text()
            assert json.loads(child.permit_path.read_text()) == original
            for boundary in ('runtime-sdk-factory-', 'startup-control-read-', 'startup-settings-read-'):
                assert not child.marker(boundary + recovered).exists()
            assert child.actual_posts() == 0
            child.assert_owner_available()
            # Human recovery removes the fault prohibition, never a saved pause.
            store = PredictionArbitrageStore(child.data_dir)
            store.lp_save_current_control(dict(account_id=hashlib.sha256(WALLET.encode()).hexdigest(),
                process_epoch='offline-paused-recovery', request_version=2,
                request_id='operator-pause', desired_running=False,
                requested_at=datetime(2026, 10, 16, 12, tzinfo=UTC), actor='operator'))
            child.launch('operator-recovery-' + fault + '-foreground-cli', expected_running=False)
            current = child.wait_state(lambda state: state.get('effective_running') is False
                and state.get('current_exposure_usd') == '3.80')
            assert current['desired_running'] is False and current['fatal'] is None
            replacement = json.loads(child.permit_path.read_text())
            assert replacement['state'] == 'CLAIMED'
            assert replacement['claim_id'] != original['claim_id']
            assert replacement['process_epoch'] != original['process_epoch']
            assert child.actual_posts() == 0
            child.process.terminate()
            assert child.wait_exit() == 0, child.output.read_text()
            child.assert_owner_available()
    # A valid unset budget is unconfigured, not a failed settings read or zero.
    with ServiceChild(tmp_path / 'unconfigured', scenario='rollback-current-startup-unconfigured') as child:
        child.release('metadata')
        child.start(expected_running=False)
        state = child.wait_state(lambda state: state.get('current_exposure_usd') == '3.80')
        assert state['fatal'] is None and state['budget_usd'] is None
        assert state['new_capacity_usd'] is None
        assert state['desired_running'] is False and child.actual_posts() == 0
        child.process.terminate()
        assert child.wait_exit() == 0, child.output.read_text()
        child.assert_owner_available()


def test_native_inflight_survives_logical_resume_and_shutdown(tmp_path):
    """Logical control generations do not end real reader or sender ownership."""
    import json
    from tests.lp_d2_service_harness import ServiceChild
    for lane in ('reader', 'sender'):
        with ServiceChild(tmp_path / lane, scenario='resume-' + lane) as child:
            child.start()
            native = 'reader' if lane == 'reader' else 'post'
            child.wait(native + '-entered')
            if lane == 'reader':
                # The same sole Scheduler started a later real order read while
                # its first balance call still owns a native reader slot.
                child.wait('native-reader-orders-2')
                assert not child.marker('reader-ended').exists()
                assert not child.marker('native-balance-call-2').exists()
            else:
                assert child.actual_posts() == 1
                child.wait('business-entered')
            child.wait('metadata-entered')
            before = child.state()
            original_generation = before['round']['cycle_generation']
            paused = child.pause()
            assert paused['effective_running'] is False
            child.wait_state(lambda s: s['pause_confirmed'] is True)
            child.advance(20)
            resumed = child.request('enable', {'confirm': True})
            assert resumed['desired_running'] is True
            current = child.wait_state(lambda s: s['effective_running'] is True
                and s['saved_control_version'] == s['requested_control_version'] == 2)
            generation = current['round']['cycle_generation']
            assert generation != original_generation and current['fatal'] is None
            assert not child.marker(native + '-ended').exists()
            child.assert_owned()
            if lane == 'reader':
                assert not child.marker('native-balance-call-2').exists()
                child.advance(25)
                child.release('reader'); child.wait('reader-ended')
                child.wait('new-provider-limit-observed')
                child.advance(26)
                child.wait('native-reader-orders-at-26.0')
                noop = child.request('enable', {'confirm': True})
                assert noop['control_request_status'] == 'noop'
                assert noop['round']['cycle_generation'] == generation
                child.advance(144)
                child.wait('native-reader-orders-at-144.0')
                assert not child.marker('native-balance-call-2').exists()
                child.advance(145)
                child.wait('reader-shutdown-entered')
                assert child.marker('native-balance-call-2').read_text() == '145.0'
                native = 'reader-shutdown'
            else:
                assert child.actual_posts() == 1
            # Accepted HTTP work is a separate real owner, not a completed
            # Future or a client close event. Each native owner ends separately.
            child.begin_held_http()
            child.process.terminate(); child.wait('metadata-admission-closed')
            child.assert_owned()
            assert not child.marker('public-client-closed').exists()
            for resource in (('metadata', native) if lane == 'reader'
                    else ('business', 'metadata', native)):
                child.release(resource); child.wait(resource + '-ended')
                child.assert_owned()
                assert not child.marker('process-finalizing').exists()
                assert not child.marker('public-client-closed').exists()
            child.release('http'); child.wait_http()
            assert child.wait_exit() == 0, child.output.read_text()
            child.assert_owner_available()
            assert json.loads(child.permit_path.read_text())['state'] == 'ARMED'
            assert child.actual_posts() == (0 if lane == 'reader' else 1)
            assert child.marker('public-client-closed').read_text() == child.marker('public-client-created').read_text()


def _restart_with_history(tmp_path, mode):
    import json
    import os
    import subprocess
    import sys
    from tests.lp_d2_service_harness import seed_no_reconcile_history, no_reconcile_history
    store=PredictionArbitrageStore(tmp_path)
    seed_running(store)
    seed_no_reconcile_history(store)
    if mode=='sells':
        from open_trader.lp_business_writer import RequestRecord
        from datetime import UTC, datetime
        store.lp_log_business_record(RequestRecord(account_id=WALLET,process_epoch='old-process',
            request_id='old-sell-request',session_id='old-sell-audit',condition_id=CONDITION_ID,
            token_id=TOKEN_ID,kind='SELL',price=Decimal('.60'),quantity=Decimal('10'),
            requested_at=datetime(2026,10,1,tzinfo=UTC),order_id='old-bot-sell'))
    before=json.loads(json.dumps(no_reconcile_history(store),default=str))
    process=subprocess.run([sys.executable,'-B','-m','tests.lp_d2_service_harness',
        'restart-invariant',str(tmp_path),mode],capture_output=True,text=True,
        timeout=20,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
    assert process.returncode==0, process.stdout+process.stderr
    return before,json.loads((tmp_path/'restart-result.json').read_text())


def test_restart_discards_missing_old_orders_without_inventing_terminal_results(tmp_path):
    before,fresh=_restart_with_history(tmp_path,'empty')
    assert fresh['accepted']=='accepted' and fresh['orders_complete'] is True
    assert fresh['orders']==[] and Decimal(fresh['reserved'])==0
    assert fresh['posts']==fresh['market_orders']==fresh['cancels']==[]
    assert fresh['history']==before  # Pending/audit rows are not FILLED/CANCELED by absence.
    assert fresh['history']['intents']['old-intent']['state']=='pending'


def test_restart_manual_sell_is_visible_and_never_automatically_canceled(tmp_path):
    before,fresh=_restart_with_history(tmp_path,'sells')
    assert fresh['orders_complete'] is True
    assert {tuple(o) for o in fresh['observed_orders']}=={
        ('A','BUY'),('old-bot-sell','SELL'),('manual-sell','SELL')}
    assert {tuple(o) for o in fresh['orders']}=={('old-bot-sell','SELL'),('manual-sell','SELL')}
    assert fresh['cancels']==[['A']]  # Includes first cycle and automatic protection cycle.
    assert fresh['posts']==fresh['market_orders']==[]  # No duplicate protective SELL.
    assert fresh['history']==before
