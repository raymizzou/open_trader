"""Inactive composition at real peer, SQLite and offline SDK boundaries."""
from datetime import UTC, datetime
from decimal import Decimal
import time
import pytest

from open_trader.polymarket_lp_market_reader import MarketReader, ReadScope
from open_trader.polymarket_lp_market_calculation import CalculationPolicy
from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_order_registration_contract import _adapter, WALLET


def seed_running(store):
    store.lp_save_current_control(dict(account_id=WALLET, process_epoch='seed', request_version=1,
        request_id='seed', desired_running=True, requested_at=datetime.now(UTC), actor=None))


def test_cold_restart_discards_old_provider_deadline_but_honors_new_limit(tmp_path):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    adapter, sdk = _adapter(tmp_path)
    clock, calls = [0.0], []
    original = sdk.get_balance_allowance
    class Limited(Exception):
        retry_after_seconds = 60
    def balance(**kwargs):
        calls.append(clock[0])
        raise Limited()
    sdk.get_balance_allowance = balance
    reader = MarketReader(adapter.lp_reader_source_port(), clock=lambda: clock[0])
    frames = []
    read_current = reader.read_current
    def captured_read(*args, **kwargs):
        frame = read_current(*args, **kwargs)
        frames.append(frame)
        return frame
    reader.read_current = captured_read
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    live = None
    try:
        old = reader.read_current(ReadScope(WALLET, ('balance',)), time.monotonic()+2)
        assert old.sources['balance'].retry_at == 60 and calls == [0]
        clock[0] = 20
        live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
            process_epoch='cold-epoch', policy=CalculationPolicy(Decimal('40')), reader=reader,
            clock=lambda: clock[0])
        live.initialize(ValidatedStartAdmission('cold-epoch', 'offline-claimed-permit'))
        frames.clear()
        result = live.run_once()
        assert len(frames) == 1
        assert result.acceptance.observation.source_frame is frames[0]
        assert live.state.snapshot().observation.source_frame is frames[0]
        assert calls == [0, 20]
        assert result.acceptance.observation.source_frame.sources['balance'].retry_at == 80
        assert result.acceptance.observation.orders_complete is True
        healthy = sdk.order_reads
        clock[0] = 30
        frames.clear()
        again = live.run_once()  # ordinary wake and repeated RUN do not reset
        assert len(frames) == 1
        assert again.acceptance.observation.source_frame is frames[0]
        assert live.state.snapshot().observation.source_frame is frames[0]
        assert live.request_running(True).status == 'noop'
        assert calls == [0, 20] and sdk.order_reads > healthy
        assert again.acceptance.observation.source_frame.sources['balance'].retry_at == 80
        clock[0] = 80
        sdk.get_balance_allowance = original
        frames.clear()
        recovered = live.run_once()
        assert len(frames) == 1
        assert recovered.acceptance.observation.source_frame is frames[0]
        assert live.state.snapshot().observation.source_frame is frames[0]
        assert recovered.acceptance.observation.source_frame.sources['balance'].complete
        assert recovered.acceptance.observation.source_frame.sources['balance'].retry_at is None
        assert old.sources['balance'].retry_at == 60  # historical source never renewed
    finally:
        if live: assert live.close(timeout=3)
        else: assert reader.close(timeout=3)
        adapter.close()


def settle_control(live):
    deadline = time.monotonic()+3
    while time.monotonic() < deadline:
        status = live.poll()
        if status.saved_version == status.requested_version and not status.native_save_in_flight:
            return status
        time.sleep(.001)  # completion status is the oracle, not this polling delay
    raise AssertionError('actual control save completion watchdog')


def test_resume_resets_round_zero_and_cleans_fresh_manual_buy_ids(tmp_path):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_reader import SourceResult
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    adapter, sdk = _adapter(tmp_path)
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='resume-epoch', policy=CalculationPolicy(Decimal('40')))
    try:
        live.initialize(ValidatedStartAdmission('resume-epoch', 'offline-permit'))
        first = live.run_once()
        assert live.rounds.snapshot().round_number == 1
        live.run_once(SourceResult('captured-empty', (), checked_at=datetime.now(UTC)))
        assert live.rounds.snapshot().round_number == 2
        old_generation = live.state.snapshot().current.cycle_generation
        assert live.request_running(False).status == 'accepted'
        settle_control(live)
        sdk.orders = (_open_order('auto-buy', 'BUY', price='.40', original='10'),
            _open_order('manual-during-pause', 'BUY', price='.40', original='5'),
            _open_order('inventory-sell', 'SELL', price='.45', original='5'))
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID,
            size=Decimal('5'), average_price=Decimal('.40')),)
        cancel = sdk.cancel_orders
        def cancel_and_remove(*, order_ids):
            result = cancel(order_ids=order_ids)
            sdk.orders = tuple(o for o in sdk.orders if o.id not in result['canceled'])
            return result
        sdk.cancel_orders = cancel_and_remove
        assert live.request_running(True).status == 'accepted'
        resumed = settle_control(live)
        assert resumed.current.effective_running is True
        assert live.rounds.snapshot().round_number == 0
        assert resumed.current.cycle_generation != old_generation
        complete_orders = sdk.orders
        sdk.orders = None
        incomplete = live.run_once()
        assert live.rounds.snapshot().round_number == 0 and sdk.cancels == []
        assert incomplete.acceptance.observation.orders_complete is False
        sdk.orders = complete_orders
        cleanup = live.run_once()
        assert live.rounds.snapshot().round_number == 1
        assert sdk.cancels == [('auto-buy',), ('manual-during-pause',)]
        assert [o.id for o in sdk.orders] == ['inventory-sell']
        snapshot = live.state.snapshot()
        assert snapshot.positions[0].quantity.value == 5
        assert snapshot.inventory_cost.value == Decimal('2.00')
        assert all(o.side == 'SELL' for o in snapshot.active_orders)
        assert len(cleanup.outcomes) == 2 and all(o.status.value == 'accepted' for o in cleanup.outcomes)
        assert len({o.identity.request_id for o in cleanup.outcomes}) == 2
        assert sdk.posts == []
    finally:
        assert live.close(timeout=3)
        adapter.close()


def candidates_for_fixed_send():
    from open_trader.polymarket_lp_market_reader import SourceResult
    rows = [dict(condition_id='condition-'+name, market_id='market-'+name, token_id='token-'+name,
        venue='polymarket', price=Decimal('.40'), quantity=Decimal('20'), order_type='GTD',
        expiration=1893456000, post_only=True, send_options=(),
        direction=dict(market=dict(reward_min_size=Decimal('1'), reward_max_spread=Decimal('.10')),
            book=dict(bids=[dict(price='.40', size='100')], asks=[dict(price='.42', size='100')]),
            daily_pool_usd=Decimal(pool))) for name,pool in [('F','12'),('E','24'),('D','36'),('C','48')]]
    return SourceResult('captured-CDEF', rows, checked_at=datetime.now(UTC)), rows


@pytest.mark.parametrize('option_variant', ['supported_builder', 'unsupported_option'])
def test_fixed_cde_finishes_once_with_mixed_sync_and_unknown_results(tmp_path, monkeypatch, option_variant):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from polymarket.models.clob.order_response import AcceptedOrder, RejectedOrder
    from tests.test_lp_business_writer_contract import blocked_database, request, wait_status, stored_actions
    from open_trader.lp_business_writer import LPBusinessWriter
    adapter, sdk = _adapter(tmp_path)
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    writer = LPBusinessWriter(store)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='fixed-CDE', policy=CalculationPolicy(Decimal('40'), 3), writer=writer)
    captured, caller_rows = candidates_for_fixed_send()
    builder_code = '0x' + 'b'*64
    caller_rows[3].update(order_type='GTC', expiration=None,
        send_options=(('builder_code', builder_code),) if option_variant == 'supported_builder' else (('defer_exec', False),))
    signs, posts = [], []
    def sign(**kwargs):
        signs.append(dict(kwargs))
        if len(signs) == 1:
            caller_rows[0]['direction']['daily_pool_usd'] = Decimal('9999')  # F appears after sealing
            caller_rows[1]['price'] = Decimal('.99')
        return dict(kwargs, order_type='GTC' if kwargs['expiration'] is None else 'GTD')
    def post(signed):
        posts.append(dict(signed))
        if signed['token_id'] == 'token-C':
            return AcceptedOrder(ok=True, order_id='venue-C', status='live',
                making_amount=Decimal('0'), taking_amount=Decimal('0'),
                trade_ids=(), transactions_hashes=())
        if signed['token_id'] == 'token-D':
            return RejectedOrder(ok=False, code='post_only_would_cross', message='offline venue rejection')
        raise TimeoutError('offline local attempt timed out')
    sdk.create_limit_order, sdk.post_order = sign, post
    sdk.list_account_trades = lambda: (_ for _ in ()).throw(AssertionError('history sweep'))
    blocker = None
    try:
        live.initialize(ValidatedStartAdmission('fixed-CDE', 'offline-permit'))
        live.run_once()  # valid empty startup cleanup
        assert live.rounds.snapshot().round_number == 1
        blocker, entered = blocked_database(store, monkeypatch)
        writer.offer(request('held-business-DB'))
        assert entered.wait(2)
        before = (sdk.balance_reads, sdk.order_reads, sdk.position_reads)
        result = live.run_once(captured)
        supported = option_variant == 'supported_builder'
        assert [o.status.value for o in result.outcomes] == [
            'accepted' if supported else 'not_sent', 'rejected', 'unknown_timeout']
        expected_tokens = ['token-C', 'token-D', 'token-E'] if supported else ['token-D', 'token-E']
        assert [s['token_id'] for s in signs] == expected_tokens
        assert [p['token_id'] for p in posts] == expected_tokens
        if supported:
            assert signs[0]['builder_code'] == builder_code  # actual SDK boundary, after real adapter
        else:
            assert result.outcomes[0].venue_order_id is None and result.outcomes[0].receipt_status is None
            assert live.status()['last_preparation_rejection'] == (
                result.outcomes[0].identity.request_id, 'unsupported_send_option')
        assert all('builder_code' not in signed for signed in signs if signed['token_id'] != 'token-C')
        assert (sdk.balance_reads, sdk.order_reads, sdk.position_reads) == tuple(x+1 for x in before)
        assert all(s['price'] == Decimal('.40') and s['size'] == 20 and s['side'] == 'BUY'
            and s['post_only'] is True and s['expiration'] == (None if s['token_id'] == 'token-C' else 1893456000) for s in signs)
        token = result.start.token
        assert [a.target.market_id for a in token.actions] == ['market-C','market-D','market-E']
        assert len({a.request_id for a in token.actions}) == 3
        assert tuple(o.identity for o in result.outcomes) == token.actions
        assert live.rounds.snapshot().round_number == 2
        assert stored_actions(store) == []  # round completion did not wait on businessDB
        if supported:
            current = live.state.snapshot().orders[0]
            assert current.key == (WALLET, 'condition-C', 'token-C', 'venue-C')
            assert current.original_size.value == 20 and current.size_matched.value == 0
            assert current.remaining.value == 20
            assert live.state.snapshot().buy_reserved.value == Decimal('8.00')
            assert live.state.snapshot().new_capacity.state == 'unknown'
        else:
            assert live.state.snapshot().orders == ()
        from dataclasses import replace
        assert live.complete_local_attempts(replace(token, account_id='foreign-account'), result.outcomes).status.value == 'identity_error'
        assert live.complete_local_attempts(replace(token, cycle_generation='stale-generation'), result.outcomes).status.value == 'stale'
        wrong_venue = replace(token.actions[0], target=replace(token.actions[0].target, venue='wrong-venue'))
        assert live.complete_local_attempts(replace(token, actions=(wrong_venue, *token.actions[1:])), result.outcomes).status.value == 'identity_error'
        assert live.complete_local_attempts(token, result.outcomes).status.value == 'duplicate'
        assert len(posts) == len(expected_tokens)
        blocker.execute('COMMIT')
        wait_status(writer, lambda s: s['pending_entries'] == s['inflight_entries'] == 0)
        recorded = dict(stored_actions(store))
        for action in token.actions:
            row = recorded[action.request_id]
            assert row['batch_id'] == token.batch_id and row['cycle_generation'] == token.cycle_generation
            assert row['observation_id'] == token.observation_id and row['round_number'] == 1
            assert row['venue'] == 'polymarket' and row['condition_id'] == 'condition-'+action.target.token_id[-1]
            assert row['price'] == '0.40' and row['quantity'] == '20'
            is_c = action.target.token_id == 'token-C'
            assert row['order_type'] == ('GTC' if is_c else 'GTD')
            assert row['expiration'] == (None if is_c else 1893456000) and row['post_only'] is True
            assert row['send_options'] == (
                [['builder_code', builder_code]] if supported and is_c else
                [['defer_exec', False]] if is_c else [])
        if not supported:
            rejected = recorded[token.actions[0].request_id]['receipt']
            assert rejected['outcome'] == 'not_sent' and rejected['reason_code'] == 'unsupported_send_option'
        else:
            assert recorded[token.actions[0].request_id]['receipt']['order_id'] == 'venue-C'
        assert recorded[token.actions[2].request_id]['receipt']['outcome'] == 'unknown'
    finally:
        if blocker is not None:
            if blocker.in_transaction: blocker.execute('ROLLBACK')
            blocker.close()
        assert live.close(timeout=3)
        adapter.close()


def test_independent_cancel_preserves_native_inflight_lifetime(tmp_path):
    from threading import Event
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    adapter, sdk = _adapter(tmp_path)
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='native-owner', policy=CalculationPolicy(Decimal('40'), 3))
    entered, release = Event(), Event()
    calls = []
    def cancel(*, order_ids):
        calls.append(order_ids)
        entered.set()
        assert release.wait(5), 'actual native cancellation release watchdog'
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    sdk.create_limit_order = lambda **kw: dict(kw, order_type='GTD')
    def post(signed):
        sdk.posts.append(signed)
        return dict(accepted=True, status='LIVE', order_id='venue-'+signed['token_id'], size_matched='0')
    sdk.post_order = post
    key = (WALLET, CONDITION_ID, TOKEN_ID, 'ordinary-A')
    try:
        wrong = None
        try:
            with pytest.raises(ValueError):
                wrong = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='wrong-venue',
                    process_epoch='wrong-venue', policy=CalculationPolicy(Decimal('40')))
        finally:
            if wrong is not None: assert wrong.close(timeout=3)
        live.initialize(ValidatedStartAdmission('native-owner', 'offline-permit'))
        live.run_once()
        sdk.orders = (_open_order('ordinary-A', 'BUY', price='.40', original='10'),)
        live.run_once()  # observe the exact order; no fixed plan means no BUY sends
        with pytest.raises(ValueError): live.cancel_independent(('wrong-account', *key[1:]))
        with pytest.raises(ValueError): live.cancel_independent((WALLET, 'polymarket', TOKEN_ID, 'ordinary-A'))
        with pytest.raises(ValueError): live.cancel_independent((WALLET, CONDITION_ID, TOKEN_ID, 'wrong-order'))
        assert calls == []
        attempt = live.cancel_independent(key)
        assert entered.wait(2)
        assert attempt.identity.target.existing_order_id == 'ordinary-A'
        assert attempt.identity.target.venue == 'polymarket'
        assert attempt.done() is False
        captured, _ = candidates_for_fixed_send()
        bought = live.run_once(captured)
        assert [o.status.value for o in bought.outcomes] == ['accepted']*3
        assert live.rounds.snapshot().round_number == 2
        assert live.status()['native_inflight'] == 1 and not attempt.done()
        old_generation = live.state.snapshot().current.cycle_generation
        live.request_running(False)
        settle_control(live)
        live.request_running(True)
        settle_control(live)
        assert live.rounds.snapshot().round_number == 0
        assert live.state.snapshot().current.cycle_generation != old_generation
        assert live.cancel_independent(key) is attempt  # no second native request after logical resume
        cleanup = live.run_once()
        assert cleanup.outcomes[0].status.value == 'not_sent'
        assert calls == [('ordinary-A',)]
        assert not attempt.done() and live.status()['native_inflight'] == 1
        assert live.close(timeout=0) is False
        assert live.status()['native_inflight'] == 1
        release.set()
        outcome = attempt.result(timeout=3)
        assert outcome.identity is attempt.identity and outcome.status.value == 'accepted'
        assert live.close(timeout=3) is True
        assert live.status()['native_inflight'] == 0
        assert calls == [('ordinary-A',)] and len(sdk.posts) == 3
        current = next(o for o in live.state.snapshot().orders if o.key == key)
        assert current.terminal_proof.status == 'CANCELED'
    finally:
        release.set()
        assert live.close(timeout=3)
        adapter.close()
