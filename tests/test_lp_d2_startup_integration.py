"""Startup and resume compose the real owners without active runtime wiring."""
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
