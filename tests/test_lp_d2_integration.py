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
    store.lp_save_current_control(dict(account_id='30ffa9b5e1968e54bddadba7ff14c633c7c955a6f41808d9f5633a42c07537dd',
        process_epoch='seed', request_version=1,
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
    signs, posts, after_c = [], [], []
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
            after_c.append(live.state.snapshot())  # C is applied; E has not been posted
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
            assert after_c[0].buy_reserved.value == Decimal('8.00')
            assert after_c[0].orders[0] is current  # E uncertainty does not renew C's evidence
        else:
            assert live.state.snapshot().orders == ()
        final = live.state.snapshot()
        assert all(f.state == 'unknown' and f.value is None for f in (
            final.buy_reserved, final.inventory_cost, final.current_exposure, final.new_capacity))
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
        assert [o.status.value for o in bought.outcomes] == ['accepted']*2
        assert [post['token_id'] for post in sdk.posts] == ['token-C', 'token-D']
        assert live.rounds.snapshot().round_number == 2
        assert live.status()['native_inflight'] == 1 and not attempt.done()
        old_generation = live.state.snapshot().current.cycle_generation
        live.request_running(False)
        settle_control(live)
        live.request_running(True)
        settle_control(live)
        assert live.rounds.snapshot().round_number == 0
        assert live.state.snapshot().current.cycle_generation != old_generation
        with pytest.raises(ValueError,match='independent_cancel_not_current'):
            live.cancel_independent(key)  # logical resume supplies no new current facts
        assert not attempt.done() and live.status()['native_inflight'] == 1
        assert live.close(timeout=0) is False
        assert live.status()['native_inflight'] == 1
        release.set()
        outcome = attempt.result(timeout=3)
        assert outcome.identity is attempt.identity and outcome.status.value == 'accepted'
        attempt._thread.join(3);assert attempt.done()
        assert calls == [('ordinary-A',)]
        assert live.close(timeout=3) is True
        assert live.status()['native_inflight'] == 0
        assert calls == [('ordinary-A',)] and len(sdk.posts) == 2
        current = next(o for o in live.state.snapshot().orders if o.key == key)
        assert current.terminal_proof.status == 'CANCELED'
    finally:
        release.set()
        assert live.close(timeout=3)
        adapter.close()


def test_accepted_buy_can_be_canceled_before_another_observation(tmp_path):
    from polymarket.models.clob.order_response import AcceptedOrder
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    adapter, sdk = _adapter(tmp_path)
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    sdk.create_limit_order = lambda **kw: dict(kw, order_type='GTD')
    def post(signed):
        sdk.posts.append(signed)
        return AcceptedOrder(order_id='venue-C', status='live', making_amount=Decimal('0'),
            taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
    sdk.post_order = post
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='receipt-then-cancel', policy=CalculationPolicy(Decimal('40'), 1))
    try:
        live.initialize(ValidatedStartAdmission('receipt-then-cancel', 'offline-permit'))
        live.run_once()
        captured, _ = candidates_for_fixed_send()
        bought = live.run_once(captured)
        assert [o.status.value for o in bought.outcomes] == ['accepted']
        key = (WALLET, 'condition-C', 'token-C', 'venue-C')
        snapshot = live.state.snapshot()
        assert snapshot.observation.orders == ()  # actual frame predates this receipt
        current, = snapshot.active_orders
        assert current.key == key and current.price.value == Decimal('.40')
        assert current.original_size.value == 20
        before = (sdk.balance_reads, sdk.order_reads, sdk.position_reads)
        with pytest.raises(ValueError):
            live.cancel_independent((WALLET, 'condition-C', 'token-C', 'foreign-order'))
        attempt = live.cancel_independent(key)
        outcome = attempt.result(timeout=3)
        assert outcome.identity is attempt.identity and outcome.status.value == 'accepted'
        assert outcome.identity.account_id == WALLET
        assert outcome.identity.target.venue == 'polymarket'
        assert outcome.identity.target.token_id == 'token-C'
        assert outcome.identity.target.existing_order_id == 'venue-C'
        assert outcome.identity.request_id and outcome.identity.request_id != bought.outcomes[0].identity.request_id
        assert sdk.cancels == [('venue-C',)] and len(sdk.posts) == 1
        assert before == (sdk.balance_reads, sdk.order_reads, sdk.position_reads)
        assert live.state.snapshot().observation is snapshot.observation
        # R2 owns final fill provenance; this case does not seal/assert old matched0.
    finally:
        assert live.close(timeout=3)
        adapter.close()


@pytest.mark.parametrize('fill_ordering', ['fill_after_ack', 'newer_fill_before_ack'])
def test_cancel_ack_preserves_unknown_final_fill_until_authoritative_api(tmp_path, fill_ordering):
    from threading import Event
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    adapter, sdk = _adapter(tmp_path)
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='cancel-final-unknown', policy=CalculationPolicy(Decimal('40')))
    entered, release = Event(), Event()
    def cancel(*, order_ids):
        sdk.cancels.append(order_ids)
        entered.set()
        assert release.wait(5), 'actual ID-only cancel completion watchdog'
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    sdk.list_account_trades = lambda **kw: pytest.fail('cancel reconciliation attempted whole-account history')
    key = (WALLET, CONDITION_ID, TOKEN_ID, 'cancel-A')
    def current_order(): return next(o for o in live.state.snapshot().orders if o.key == key)
    try:
        live.initialize(ValidatedStartAdmission('cancel-final-unknown', 'offline-permit'))
        live.run_once()
        sdk.orders = (_open_order('cancel-A', 'BUY', price='.40', original='10'),)
        observed = live.run_once()
        original = current_order()
        assert original.size_matched.value == 0 and original.original_size.value == 10
        attempt = live.cancel_independent(key)
        assert entered.wait(2)
        if fill_ordering == 'newer_fill_before_ack':
            sdk.orders = (_open_order('cancel-A', 'BUY', price='.40', original='10', matched='3'),)
            sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='3', average_price='.40'),)
            live.run_once()
        lower_bound = current_order().size_matched
        reads = (sdk.balance_reads, sdk.order_reads, sdk.position_reads)
        release.set()
        outcome = attempt.result(timeout=3)
        assert outcome.status.value == 'accepted' and outcome.identity is attempt.identity
        acked = current_order()
        assert acked.terminal_proof is not None and acked.terminal_proof.status == 'CANCELED'
        assert acked.terminal_proof.matched.state == 'unknown' and acked.terminal_proof.matched.value is None
        assert acked.terminal_proof.original_size is original.original_size
        assert acked.size_matched is lower_bound  # old observed quantity keeps its own provenance
        assert acked.remaining.state == 'unknown' and acked.remaining.value is None
        assert live.status()['last_current_evidence'] == (attempt.identity.request_id, 'cancel', 'applied')
        assert reads == (sdk.balance_reads, sdk.order_reads, sdk.position_reads)
        assert observed.acceptance.observation.orders[0].size_matched.value == 0
        assert observed.acceptance.observation.orders[0].terminal_proof is None
        # A contradictory ordinary LIVE row cannot resurrect a canceled order.
        sdk.orders = (_open_order('cancel-A', 'BUY', price='.40', original='10'),)
        live.run_once()
        assert current_order().terminal_proof is acked.terminal_proof
        # Later authoritative ordinary API supplies the actual final cumulative fills.
        sdk.orders = (_open_order('cancel-A', 'BUY', price='.40', original='10', matched='3', status='CANCELED'),)
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='3', average_price='.40'),)
        reconciled = live.run_once()
        final = current_order()
        assert final.terminal_proof.matched.value == 3 and final.size_matched.value == 3
        assert final.remaining.value == 7
        assert live.state.snapshot().positions[0].quantity.value == 3
        assert live.state.snapshot().inventory_cost.value == Decimal('1.20')
        assert final.size_matched.sources[0].source_id == reconciled.acceptance.observation.source_frame.sources['open_orders'].source_id
        with pytest.raises(ValueError): live.cancel_independent(key)
        assert sdk.cancels == [('cancel-A',)] and sdk.posts == []
    finally:
        release.set()
        assert live.close(timeout=3)
        adapter.close()


@pytest.mark.parametrize('failure', ['post_timeout', 'post_error', 'missing_quantity',
    'invalid_quantity', 'conflicting_quantity', 'create_not_sent'])
def test_all_uncertain_buy_results_invalidate_financial_authority(tmp_path, monkeypatch, failure):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.lp_business_writer import LPBusinessWriter
    from tests.test_lp_business_writer_contract import blocked_database, request, stored_actions
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    adapter, sdk = _adapter(tmp_path)
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    writer = LPBusinessWriter(store)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='all-uncertain', policy=CalculationPolicy(Decimal('40'), 3), writer=writer)
    signs, posts, old_tickets = [], [], []
    def sign(**kw):
        signs.append(dict(kw))
        if failure == 'create_not_sent': raise TimeoutError('offline create/sign did not post')
        return dict(kw, order_type='GTD')
    def post(signed):
        posts.append(dict(signed))
        if not old_tickets:
            old_tickets.append(live.state.begin_observation(live.state.snapshot().current.cycle_generation,
                ('balance', 'open_orders', 'positions')))
        if failure == 'post_timeout': raise TimeoutError('offline unknown native post')
        if failure == 'post_error': raise RuntimeError('offline unknown native post')
        response = dict(accepted=True, status='LIVE', order_id='possible-'+signed['token_id'])
        if failure == 'invalid_quantity': response['taking_amount'] = Decimal('NaN')
        if failure == 'conflicting_quantity': response.update(taking_amount=Decimal('0'), size_matched=Decimal('1'))
        return response
    sdk.create_limit_order, sdk.post_order = sign, post
    sdk.list_account_trades = lambda **kw: pytest.fail('uncertain BUY attempted history recovery')
    blocker = None
    try:
        live.initialize(ValidatedStartAdmission('all-uncertain', 'offline-permit'))
        live.run_once()
        baseline = live.state.snapshot()
        assert baseline.buy_reserved.value == baseline.inventory_cost.value == baseline.current_exposure.value == 0
        assert baseline.new_capacity.value == 40
        blocker, entered = blocked_database(store, monkeypatch)
        writer.offer(request('held-uncertainty-audit'))
        assert entered.wait(2)
        captured, _ = candidates_for_fixed_send()
        reads = (sdk.balance_reads, sdk.order_reads, sdk.position_reads)
        result = live.run_once(captured)
        expected = 'not_sent' if failure == 'create_not_sent' else 'unknown_timeout' if failure == 'post_timeout' else 'unknown_error'
        assert [o.status.value for o in result.outcomes] == [expected]*3
        assert tuple(o.identity for o in result.outcomes) == result.start.token.actions
        assert len({o.identity.request_id for o in result.outcomes}) == 3
        assert [s['token_id'] for s in signs] == ['token-C', 'token-D', 'token-E']
        assert [s['token_id'] for s in posts] == ([] if failure == 'create_not_sent' else ['token-C', 'token-D', 'token-E'])
        assert (sdk.balance_reads, sdk.order_reads, sdk.position_reads) == tuple(n+1 for n in reads)
        assert live.rounds.snapshot().round_number == 2
        assert live.complete_local_attempts(result.start.token, result.outcomes).status.value == 'duplicate'
        snapshot = live.state.snapshot()
        assert snapshot.orders == () and snapshot.own_fills == {}
        assert snapshot.observation is result.acceptance.observation
        assert stored_actions(store) == []  # actual audit write is still blocked
        finances = (snapshot.buy_reserved, snapshot.inventory_cost, snapshot.current_exposure, snapshot.new_capacity)
        if failure == 'create_not_sent':
            assert [f.value for f in finances] == [0, 0, 0, 40]
            assert all(f.state == 'known' for f in finances)
            assert snapshot.trade_version == baseline.trade_version
            assert live.status()['last_buy_uncertainty'] is None
            return
        assert all(f.state == 'unknown' and f.value is None for f in finances)
        assert snapshot.trade_version == baseline.trade_version + 3
        uncertainty = live.status()['last_buy_uncertainty']
        last = result.start.token.actions[-1]
        assert uncertainty.request_id == last.request_id and uncertainty.account_id == WALLET
        assert uncertainty.process_epoch == 'all-uncertain'
        assert uncertainty.cycle_generation == result.start.token.cycle_generation
        assert uncertainty.batch_id == result.start.token.batch_id and uncertainty.round_number == 1
        assert uncertainty.observation_id == result.start.token.observation_id
        assert (uncertainty.venue, uncertainty.condition_id, uncertainty.token_id) == ('polymarket', 'condition-E', 'token-E')
        assert uncertainty.price == Decimal('.40') and uncertainty.quantity == 20
        assert uncertainty.order_type == 'GTD' and uncertainty.expiration == 1893456000
        assert uncertainty.post_only is True and uncertainty.send_options == ()
        assert uncertainty.native_started_at <= uncertainty.observed_at
        assert uncertainty.reason == ('unknown_timeout' if failure == 'post_timeout' else 'unknown_error' if failure == 'post_error' else 'invalid_receipt')
        assert uncertainty.venue_order_id == (None if failure in ('post_timeout', 'post_error') else 'possible-token-E')
        assert live.status()['last_current_evidence'] == (last.request_id, 'buy_uncertainty', 'applied')
        old_frame, old_calculation = result.acceptance.observation.source_frame, result.acceptance.observation.calculation
        assert live.state.accept(old_tickets[0], old_frame, old_calculation, evaluated_at=datetime.now(UTC)).status == 'stale'
        fresh_ticket = live.state.begin_observation(snapshot.current.cycle_generation, ('balance', 'open_orders', 'positions'))
        live.state.accept(fresh_ticket, old_frame, old_calculation, evaluated_at=datetime.now(UTC))
        assert live.state.snapshot().new_capacity.state == 'unknown'  # new ticket cannot renew pre-event sources
        sdk.orders = (_open_order('fresh-api-order', 'BUY', price='.40', original='20', matched='3'),)
        sdk.positions = None
        live.run_once()
        assert live.state.snapshot().new_capacity.state == 'unknown'  # partial API is not recovered authority
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='3', average_price='.40'),)
        fresh = live.run_once()
        restored = live.state.snapshot()
        assert restored.buy_reserved.value == Decimal('6.80') and restored.inventory_cost.value == Decimal('1.20')
        assert restored.current_exposure.value == Decimal('8.00') and restored.new_capacity.value == Decimal('33.20')
        assert fresh.acceptance.observation.source_frame.sources['open_orders'].checked_at > uncertainty.observed_at
        assert sdk.cancels == [] and len(posts) == 3
        assert stored_actions(store) == []  # current recovery still has no business DB dependency
    finally:
        if blocker is not None:
            if blocker.in_transaction: blocker.execute('ROLLBACK')
            blocker.close()
        assert live.close(timeout=3)
        adapter.close()


def _d2_protection_runtime(tmp_path, monkeypatch, *, quantity='2000', submit=True,
                           budget='10000', bid='.30', ask='.32'):
    """Actual D2 components with only SDK responses and business time controlled."""
    from datetime import timedelta
    from types import SimpleNamespace
    from polymarket.models.clob.order_book import OrderBookLevel
    from polymarket.models.clob.order_response import AcceptedOrder
    from polymarket.models.clob.rewards import CurrentReward, CurrentRewardConfig
    from open_trader import polymarket_lp_market_reader, polymarket_trading
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from tests.test_lp_order_registration_contract import _SDKPublicClient, _open_order, CONDITION_ID, TOKEN_ID
    clock, origin = [0.0], datetime.now(UTC)
    class ClockMeta(type):
        def __instancecheck__(cls, value): return isinstance(value, datetime)
    class BusinessDateTime(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            now = origin + timedelta(seconds=clock[0])
            return now if tz is not None else now.replace(tzinfo=None)
    monkeypatch.setattr(polymarket_lp_market_reader, 'datetime', BusinessDateTime)
    monkeypatch.setattr(polymarket_trading, 'datetime', BusinessDateTime)
    bid_value, ask_value = Decimal(bid), Decimal(ask)
    class Public(_SDKPublicClient):
        level, bid, ask, reward, failed, reward_unknown = (
            Decimal('10000'), bid_value, ask_value, Decimal('24'), False, False
        )
        book_reads = 0
        def get_order_book(self, *, token_id):
            self.book_reads += 1
            if self.failed:
                class Unavailable(RuntimeError): retry_after_seconds = 0
                raise Unavailable('offline current book unavailable')
            return super().get_order_book(token_id=token_id).model_copy(update={
                'timestamp': BusinessDateTime.now(UTC),
                'bids': (OrderBookLevel(price=self.bid, size=self.level),),
                'asks': (OrderBookLevel(price=self.ask, size=Decimal('10000')),),
            })
        def list_market_rewards(self, *, condition_id, sponsored):
            assert condition_id == CONDITION_ID
            if self.reward_unknown:
                class Unavailable(RuntimeError): retry_after_seconds = 0
                raise Unavailable('offline current reward unavailable')
            return (CurrentReward(condition_id=condition_id, rewards_max_spread=10,
                rewards_min_size=Decimal('1'), rewards_config=(CurrentRewardConfig(
                    asset_address='0x2791bca1f2de4661ed88a30c99a7a9449aa84174',
                    start_date=origin-timedelta(days=1), end_date=origin+timedelta(days=1),
                    rate_per_day=Decimal('0') if sponsored else self.reward),)),)
    public = Public(origin)
    adapter, sdk = _adapter(tmp_path, public_client=public)
    sdk.get_balance_allowance = lambda **kw: SimpleNamespace(balance='100000000000',
        allowances={sdk.environment.standard_exchange: '100000000000'})
    sdk.create_limit_order = lambda **kw: dict(kw, order_type='GTC' if kw['expiration'] is None else 'GTD')
    def post(signed):
        sdk.posts.append(signed)
        sdk.orders += (_open_order('entry-A', 'BUY', price=str(signed['price']), original=str(signed['size'])),)
        return AcceptedOrder(order_id='entry-A', status='live', making_amount=Decimal('0'),
            taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
    sdk.post_order = post
    def bounded_empty_trades(**kwargs):
        assert kwargs == dict(market=CONDITION_ID)
        return ()
    sdk.list_account_trades = bounded_empty_trades
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='protection-epoch', policy=CalculationPolicy(Decimal(budget), 1),
        clock=lambda: clock[0], utc_now=lambda: BusinessDateTime.now(UTC))
    live.initialize(ValidatedStartAdmission('protection-epoch', 'offline-permit'))
    live.run_once()  # actual round0 empty cleanup, never seed peer internals
    if submit:
        book = adapter.lp_order_books((TOKEN_ID,))[TOKEN_ID]
        captured = polymarket_lp_market_reader.SourceResult('pre-submit-current-book', (
            dict(condition_id=CONDITION_ID, market_id='market-1', token_id=TOKEN_ID,
                venue='polymarket', price=Decimal(bid), quantity=Decimal(quantity),
                order_type='GTC', expiration=None, post_only=True, send_options=(),
                direction=dict(market=dict(reward_min_size=Decimal('1'), reward_max_spread=Decimal('.10')),
                    book=book, daily_pool_usd=Decimal('24'))),), checked_at=book['received_at'])
        result = live.run_once(captured)
        assert [o.status.value for o in result.outcomes] == ['accepted']
    return live, sdk, public, clock


def test_d2_queue_protection_cancels_exact_price_bucket_once(tmp_path, monkeypatch):
    from threading import Event, Condition
    from tests.test_lp_order_registration_contract import _open_order
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    changed = Condition()
    def cancel(*, order_ids):
        with changed:
            sdk.cancels.append(order_ids)
            if {oid for ids in sdk.cancels for oid in ids} == {'entry-A', 'manual-peer'}:
                entered.set()
            changed.notify_all()
        assert release.wait(5), 'exact queue native cancel release watchdog'
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    try:
        live.run_once()
        before = (sdk.balance_reads, sdk.order_reads, sdk.position_reads, public.book_reads)
        live.run_protection_once()
        snap = live.state.snapshot()
        baseline = next(b for k,b in snap.baselines.items() if k.kind == 'queue_bucket')
        progress = snap.progress[baseline.key]
        assert baseline.baseline_front.value == 10000 and baseline.baseline_source == 'submit'
        assert progress.front_estimate.value == 8000 and progress.level_total.value == 10000
        assert progress.ratio.value == Decimal('.80') and sdk.cancels == []
        assert baseline.source.checked_at <= progress.data_time
        assert before == (sdk.balance_reads, sdk.order_reads, sdk.position_reads, public.book_reads)
        sdk.orders += (_open_order('manual-peer', 'BUY', price='.30', original='500'),
            _open_order('other-price', 'BUY', price='.29', original='100'),
            _open_order('sell-inventory', 'SELL', price='.30', original='100'))
        public.level = Decimal('5000')
        live.run_once()
        before = (sdk.order_reads, public.book_reads)
        attempts = live.run_protection_once()
        assert entered.wait(2)
        snap = live.state.snapshot()
        progress = snap.progress[baseline.key]
        assert progress.front_estimate.value == 2500 and progress.level_total.value == 5000
        assert progress.ratio.value == Decimal('.50')
        assert {t.order_key[3] for t in progress.cancel_targets} == {'entry-A', 'manual-peer'}
        assert {oid for ids in sdk.cancels for oid in ids} <= {'entry-A', 'manual-peer'}
        assert before == (sdk.order_reads, public.book_reads)
        live.run_protection_once()
        assert live.status()['native_inflight'] > 0
        release.set()
        for attempt in attempts:
            assert attempt.result(timeout=3).status.value == 'accepted'
            attempt._thread.join(timeout=2)
            assert attempt.done(), 'exact queue cancellation must end natively'
        assert live.close(timeout=3)
        assert sorted(oid for ids in sdk.cancels for oid in ids) == ['entry-A', 'manual-peer']
        assert len(sdk.posts) == 1
    finally:
        release.set()
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('applicability', ['eligible', 'filled', 'terminal', 'already_requested', 'identity_conflict'])
def test_d2_queue_outage_nine_ten_keeps_applicability_gates(tmp_path, monkeypatch, applicability):
    from threading import Event
    from tests.test_lp_order_registration_contract import CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch)
    release, entered = Event(), Event()
    pending = None
    def advance_failed():
        clock[0] += 1
        live.run_once()
        return live.run_protection_once()
    def failure():
        return next(f for f in live.state.snapshot().failure_states.values() if f.scope_kind == 'queue_group')
    try:
        live.run_once()
        live.run_protection_once()
        assert failure().data_failures == 0
        if applicability == 'eligible':
            public.failed = True
            for _ in range(3): advance_failed()
            assert failure().data_failures == 3
            public.failed = False
            clock[0] += 1
            live.run_once()
            live.run_protection_once()
            assert failure().data_failures == 0  # valid evaluation resets the GROUP streak
        elif applicability == 'filled':
            sdk.orders = (sdk.orders[0].model_copy(update={'size_matched': Decimal('1')}),)
        elif applicability == 'terminal':
            sdk.orders = (sdk.orders[0].model_copy(update={'status': 'CANCELED'}),)
        elif applicability == 'identity_conflict':
            sdk.orders = (sdk.orders[0].model_copy(update={'maker_address': '0x'+'f'*40}),)
        else:
            def cancel(*, order_ids):
                sdk.cancels.append(order_ids)
                entered.set()
                assert release.wait(5), 'native pre-existing cancel completion watchdog'
                return dict(canceled=order_ids, not_canceled={})
            sdk.cancel_orders = cancel
            pending = live.cancel_independent((WALLET, CONDITION_ID, TOKEN_ID, 'entry-A'))
            assert entered.wait(2)
        public.failed = True
        for _ in range(9): advance_failed()
        assert failure().data_failures == (9 if applicability == 'eligible' else 0)
        assert sdk.cancels == ([('entry-A',)] if pending else [])
        live.run_protection_once()  # same accepted source is not a distinct failed evaluation
        assert failure().data_failures == (9 if applicability == 'eligible' else 0)
        attempts = advance_failed()
        if applicability == 'eligible':
            for attempt in attempts: attempt.result(timeout=3)
            assert failure().data_failures == 10
            assert sdk.cancels == [('entry-A',)]
            live.run_protection_once()
            assert sdk.cancels == [('entry-A',)]
        else:
            assert attempts == () and failure().data_failures == 0
            if pending: assert not pending.done() and live.status()['native_inflight'] == 1
        assert len(sdk.posts) == 1
    finally:
        release.set()
        if pending: pending.result(timeout=3)
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_first_seen_baseline_and_cancel_scope_remain_current(tmp_path, monkeypatch):
    from threading import Event, Condition
    from tests.test_lp_order_registration_contract import _open_order
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch, submit=False)
    changed, both_entered, entries = Condition(), Event(), set()
    sdk_cancel = sdk.cancel_orders
    def cancel(*, order_ids):
        with changed:
            entries.update(order_ids)
            if entries == {'manual-anchor', 'manual-peer'}:
                both_entered.set()
            changed.notify_all()
        assert both_entered.wait(3), 'both exact first-seen SDK entries watchdog'
        return sdk_cancel(order_ids=order_ids)
    sdk.cancel_orders = cancel
    try:
        sdk.orders = (_open_order('manual-anchor', 'BUY', price='.30', original='2000'),)
        public.failed = True
        clock[0] = 1
        live.run_once()
        assert live.run_protection_once() == ()
        first = next(b for k,b in live.state.snapshot().baselines.items() if k.kind == 'first_seen_episode')
        assert first.anchor_order_ids == ('manual-anchor',)
        assert first.baseline_front.state == 'unknown' and first.baseline_front.value is None
        assert sdk.cancels == []
        public.failed = False
        clock[0] = 2
        valid = live.run_once()
        live.run_protection_once()
        snapshot = live.state.snapshot()
        baseline = snapshot.baselines[first.key]
        assert baseline.baseline_front.value == 8000 and baseline.baseline_source == 'first_observation'
        assert baseline.baseline_book_received_at == valid.acceptance.observation.source_frame.sources[
            'market:'+first.key.scope[1]+':'+first.key.scope[2]].value['book']['received_at']
        assert snapshot.progress[first.key].ratio.value == Decimal('.80')
        failure = snapshot.failure_states[first.key]
        assert failure.scope_kind == 'first_seen_episode' and failure.scope_id == first.key
        assert first.key.scope not in snapshot.failure_states  # no queue-group counter alias
        # Foreign current account identity makes the whole list incomplete: no action authority.
        foreign = _open_order('foreign-buy', 'BUY', price='.30', original='1').model_copy(
            update={'maker_address': '0x'+'f'*40})
        sdk.orders += (foreign,)
        clock[0] = 3
        live.run_once()
        assert live.run_protection_once() == () and sdk.cancels == []
        sdk.orders = (sdk.orders[0], _open_order('manual-peer', 'BUY', price='.30', original='500'),
            _open_order('another-price', 'BUY', price='.29', original='100'),
            _open_order('sell-only', 'SELL', price='.30', original='100'))
        public.level = Decimal('16000')
        clock[0] = 4
        accepted = live.run_once()
        reads = (sdk.order_reads, public.book_reads)
        attempts = live.run_protection_once()
        for attempt in attempts:
            assert attempt.result(timeout=3).status.value == 'accepted'
            attempt._thread.join(timeout=2)
            assert attempt.done(), 'first-seen cancellation must end natively'
        snapshot = live.state.snapshot()
        assert snapshot.baselines[first.key] is baseline  # never renew the original baseline
        progress = snapshot.progress[first.key]
        assert progress.front_estimate.value == 8000 and progress.level_total.value == 16000
        assert progress.ratio.value == Decimal('.50')
        assert progress.data_time == accepted.acceptance.observation.source_frame.sources[
            'market:'+first.key.scope[1]+':'+first.key.scope[2]].value['book']['received_at']
        assert sorted(oid for ids in sdk.cancels for oid in ids) == ['manual-anchor', 'manual-peer']
        assert reads == (sdk.order_reads, public.book_reads)
        live.run_protection_once()
        assert len(sdk.cancels) == 2 and sdk.posts == []
    finally:
        both_entered.set()
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_unknown_yield_protection_cancels_at_600_without_replay(tmp_path, monkeypatch):
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch, submit=False)
    key = (WALLET, CONDITION_ID, TOKEN_ID, 'unknown-risk')
    def uncertain_cancel(*, order_ids):
        sdk.cancels.append(order_ids)
        raise TimeoutError('offline native cancel outcome unknown')
    sdk.cancel_orders = uncertain_cancel
    try:
        sdk.orders = (_open_order('unknown-risk', 'BUY', price='.30', original='10'),
            _open_order('sell-no-timer', 'SELL', price='.31', original='10'))
        public.failed = True
        live.run_once()
        live.run_protection_once()
        assert live.state.snapshot().yields[key].unknown_since_monotonic == 0
        assert len(live.state.snapshot().yields) == 1 and sdk.cancels == []
        clock[0] = 500
        public.failed, public.reward = False, Decimal('0')
        live.run_once()
        live.run_protection_once()
        known = live.state.snapshot().yields[key]
        assert known.yield_pct_per_hour.value == 0 and known.unknown_since_monotonic is None
        clock[0] = 501
        public.failed = True
        live.run_once()
        live.run_protection_once()
        assert live.state.snapshot().yields[key].unknown_since_monotonic == 501
        live.request_running(False)
        settle_control(live)
        clock[0] = 1100
        candidates, _ = candidates_for_fixed_send()
        paused = live.run_once(candidates)
        assert paused.outcomes == () and live.state.snapshot().current.effective_running is False
        assert live.run_protection_once() == () and sdk.cancels == []  # new UNKNOWN +599
        clock[0] = 1101
        live.run_once(candidates)
        attempts = live.run_protection_once()
        for attempt in attempts:
            attempt.result(timeout=3)
            attempt._thread.join(3)
            assert attempt.done()
        assert sdk.cancels == [('unknown-risk',)]  # new UNKNOWN +600, even while autoBUY paused
        assert sdk.posts == [] and sdk.market_orders == []
        assert next(o for o in live.state.snapshot().orders if o.key == key).terminal_proof is None
        assert next(o for o in live.state.snapshot().orders if o.key == key).remaining.value == 10
        assert live.run_protection_once() == ()
        assert sdk.cancels == [('unknown-risk',)]  # the consumed source grants no replay
        clock[0] += 1
        live.run_once()
        fresh, = live.run_protection_once()
        assert fresh.identity.request_id != attempts[0].identity.request_id
        assert fresh.result(timeout=3).status.value == 'unknown_error'
        fresh._thread.join(3)
        assert fresh.done() and sdk.cancels == [('unknown-risk',), ('unknown-risk',)]
        assert live.run_protection_once() == ()
        assert sdk.posts == [] and sdk.market_orders == []
        assert next(o for o in live.state.snapshot().orders if o.key == key).terminal_proof is None
        assert next(o for o in live.state.snapshot().orders if o.key == key).remaining.value == 10
        assert live.close(timeout=3)
        before = (sdk.order_reads, public.book_reads, len(sdk.cancels))
        assert live.run_protection_once() == ()
        assert before == (sdk.order_reads, public.book_reads, len(sdk.cancels))
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_scoring_review_uses_exact_current_order_and_continuous_window(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from polymarket.models.clob.order_response import AcceptedOrder
    from open_trader.polymarket_lp_market_reader import SourceResult
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch)
    entered, release, calls = Event(), Event(), []
    scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
    def score(*, order_id):
        calls.append((clock[0], order_id))
        if order_id == 'entry-A':
            entered.set()
            assert release.wait(5), 'old scoring native release watchdog'
            return True
        assert order_id == 'entry-B'
        return clock[0] == 37
    sdk.get_order_scoring = score
    def opening(): return live.state.snapshot().openings[scope]
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        live.run_once()
        old = pool.submit(live.run_protection_once)
        assert entered.wait(2), 'actual exact-order scoring read not started'
        # A real later BUY receipt changes the target while A is in native I/O.
        saved_orders = sdk.orders
        def post(signed):
            sdk.posts.append(signed)
            sdk.orders = saved_orders + (_open_order('entry-B', 'BUY', price=str(signed['price']), original=str(signed['size'])),)
            return AcceptedOrder(order_id='entry-B', status='live', making_amount=Decimal('0'),
                taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
        sdk.post_order = post
        def capture():
            book = live.trading.lp_order_books((TOKEN_ID,))[TOKEN_ID]
            return SourceResult('new-current-entry-book', (dict(condition_id=CONDITION_ID,
                market_id='market-1', token_id=TOKEN_ID, venue='polymarket', price=Decimal('.30'),
                quantity=Decimal('2000'), order_type='GTC', expiration=None, post_only=True, send_options=(),
                direction=dict(market=dict(reward_min_size=Decimal('1'), reward_max_spread=Decimal('.10')),
                    book=book, daily_pool_usd=Decimal('24'))),), checked_at=book['received_at'])
        captured = capture()
        blocked = live.run_once(captured)
        assert blocked.outcomes == () and len(sdk.posts) == 1  # visible A occupies the market's only slot
        assert opening().entry_order_ids == ('entry-A',)
        clock[0] = 1
        sdk.orders = ()  # fresh complete listing lag permits the real later receipt
        captured = capture()
        posted = live.run_once(captured)
        assert [o.venue_order_id for o in posted.outcomes] == ['entry-B']
        assert posted.outcomes[0].status.value == 'accepted' and len(sdk.posts) == 2
        assert sdk.orders[:-1] == saved_orders and sdk.orders[-1].id == 'entry-B'
        assert opening().entry_order_ids == ('entry-A', 'entry-B')
        assert opening().scoring.order_key[3] == 'entry-B'
        assert opening().scoring.scoring.state == 'unknown' and opening().scoring.checked_at is None
        release.set()
        old.result(timeout=3)
        assert opening().scoring.scoring.state == 'unknown'  # late A=True never attaches to B
        assert opening().scoring.checked_at is None
        scoring_start = 2  # first complete A+B observation after B's receipt
        for offset in (0, 30, 35, 40, 99):
            clock[0] = scoring_start + offset
            live.run_once()
            live.run_protection_once()
            current = opening()
            assert current.scoring.order_key[3] == 'entry-B' and current.scoring.role == 'entry'
            assert current.scoring.scoring.value is (offset == 35)
            assert current.scoring.lost_since_monotonic == (None if offset == 35 else scoring_start+40 if offset >= 40 else scoring_start)
            assert current.review_status == 'none' and not current.stop_requested
            assert sdk.cancels == [] and sdk.market_orders == []
        checked, source = opening().scoring.checked_at, opening().scoring.source
        clock[0] = scoring_start + 100
        live.run_once()
        attempts = live.run_protection_once()
        for attempt in attempts: attempt.result(timeout=3)
        current = opening()
        assert current.phase == 'review' and current.review_status == 'scoring_continuously_lost'
        assert current.stop_requested and (current.review_at.value-checked).total_seconds() == 1
        assert current.scoring.checked_at == checked and current.scoring.source == source
        assert current.scoring.lost_since_monotonic == 42
        assert calls == [(0, 'entry-A'), (2, 'entry-B'), (32, 'entry-B'), (37, 'entry-B'), (42, 'entry-B'), (101, 'entry-B')]
        assert sorted(oid for ids in sdk.cancels for oid in ids) == ['entry-A', 'entry-B']
        assert current.stop_loss_latched.value is False and current.economics.opening_loss_usd.state == 'unknown'
        assert sdk.market_orders == []  # review alone never forces a sale
        live.run_protection_once()
        live.run_once(captured)
        live.run_protection_once()
        assert len(sdk.cancels) == 2 and len(sdk.posts) == 2  # no replay or re-entry
        assert opening().review_at == current.review_at and opening().stop_requested
    finally:
        release.set()
        pool.shutdown(wait=True)
        assert live.close(timeout=3)
        live.trading.close()


def _d2_filled_opening(tmp_path, monkeypatch, *, manual_sale=True):
    from datetime import timedelta
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_order_registration_contract import _open_order, _maker_order, _trade, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch, quantity='100')
    stamp = next(o.source.checked_at for o in live.state.snapshot().orders if o.key[3] == 'entry-A')
    clock[0] = 1
    # A current-process external/manual transaction, observed through the same API.
    manual = dict(token_id=TOKEN_ID, side='SELL', price=Decimal('.25'), size=Decimal('40'),
        expiration=None, post_only=False, order_type='GTC')
    if manual_sale:
        def manual_post(signed):
            sdk.posts.append(signed)
            return AcceptedOrder(order_id='manual-sale', status='live', making_amount=Decimal('0'),
                taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
        sdk.post_order = manual_post
        manual_receipt = sdk.post_order(sdk.create_limit_order(**{k:v for k,v in manual.items() if k != 'order_type'}))
        assert manual_receipt.making_amount == 0 and manual_receipt.taking_amount == 0
        sdk.orders = (sdk.orders[0].model_copy(update={'size_matched': Decimal('100'), 'status': 'FILLED'}),
            _open_order('manual-sale', 'SELL', price='.25', original='40').model_copy(
                update={'created_at': stamp+timedelta(seconds=1)}))
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='100', average_price='.30'),)
        live.run_once()  # truthful LIVE manual order observed before its fill/disappearance
        manual_terminal = sdk.orders[1].model_copy(update={'size_matched': Decimal('40'), 'status': 'FILLED'})
    sdk.orders = (sdk.orders[0].model_copy(update={'size_matched': Decimal('100'), 'status': 'FILLED'}),)
    sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='60' if manual_sale else '100', average_price='.30'),)
    sdk.trades = tuple(_trade(tid, _maker_order(oid, side, qty, price), size=qty).model_copy(
        update={'matched_at': stamp+timedelta(seconds=1), 'updated_at': stamp+timedelta(seconds=1)})
        for tid,oid,side,qty,price in ((('buy-fill','entry-A','BUY','100','.30'), ('manual-fill','manual-sale','SELL','40','.25')) if manual_sale else
            (('buy-fill','entry-A','BUY','100','.30'),)))
    sdk.exact_reads, sdk.fill_queries = [], []
    sdk.allow_exact = False
    sdk.terminal_orders = {'manual-sale': manual_terminal} if manual_sale else {}
    def exact(*, order_id):
        sdk.exact_reads.append(order_id)
        if not sdk.allow_exact and order_id != 'manual-sale': return None
        return sdk.terminal_orders.get(order_id) or next((o for o in sdk.orders if o.id == order_id), None)
    sdk.get_order = exact
    def trades(**kw):
        sdk.fill_queries.append(kw)
        assert kw == dict(market=CONDITION_ID)
        return sdk.trades
    sdk.list_account_trades = trades
    def post(signed):
        sdk.posts.append(signed)
        oid = 'protected-exit' if signed['order_type'] == 'FOK' else 'passive-exit'
        size = signed.get('size', signed.get('shares'))
        price = signed.get('price', signed.get('min_price'))
        sdk.orders += (_open_order(oid, 'SELL', price=str(price), original=str(size),
            order_type=signed['order_type']),)
        return AcceptedOrder(order_id=oid, status='live', making_amount=Decimal('0'),
            taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
    sdk.post_order = post
    def cancel(*, order_ids):
        sdk.cancels.append(order_ids)
        sdk.terminal_orders.update({o.id: o.model_copy(update={'status': 'CANCELED'}) for o in sdk.orders if o.id in order_ids})
        sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    return live, sdk, public, clock


def explicit_historical_protection(live, *, loss=None):
    """Exercise the retained protected-submit boundary for historical obligations.

    Production ticks no longer create this obligation from a loss threshold.
    This fixture explicitly requests it; every real dispatch guard still applies.
    """
    from dataclasses import replace
    from uuid import uuid4
    from open_trader.polymarket_lp_current_state import ProtectionGuard, OpeningProtectionUpdate, Fact
    from open_trader.polymarket_lp_live_execution import _SellCalculationOrigin
    def guard(snapshot, scope):
        return ProtectionGuard(scope, snapshot.current.cycle_generation, snapshot.scope_versions.get(scope,0),
            tuple((o.key,o.terms_version) for o in snapshot.orders if o.key[:3]==scope[:3]),
            None, uuid4().hex)
    for scope,opening in live.state.snapshot().openings.items():
        if opening.phase=='closed':continue
        snapshot=live.state.snapshot();g=guard(snapshot,scope)
        historical=replace(opening,stop_requested=True,stop_loss_latched=Fact('known',True),
            stop_loss_triggered_at=opening.stop_loss_triggered_at or live._utc_now(),
            stop_loss_triggered_loss_usd=opening.stop_loss_triggered_loss_usd if opening.stop_loss_latched.value else
                Fact('known',loss) if loss is not None else opening.stop_loss_triggered_loss_usd,
            scope_version=g.expected_scope_version+1)
        assert live.state.apply_protection(OpeningProtectionUpdate(g,historical))=='applied'
    handles=list(live.run_protection_once())
    for scope,sell in live.state.snapshot().sells.items():
        snapshot=live.state.snapshot();opening=snapshot.openings[scope]
        if opening.phase=='closed' or not sell.position_reconciled or sell.residual_quantity.state!='known' or sell.residual_quantity.value<=0:continue
        g=replace(guard(snapshot,scope),observation_id=snapshot.observation.observation_id)
        sold=opening.buy_filled_quantity.value-sell.residual_quantity.value
        origin=_SellCalculationOrigin(g,snapshot.trade_version,sell.residual_quantity.value,sold)
        passive=sell.passive
        if passive and (passive.native_state!='ended' or passive.state not in ('terminal','rejected')):
            if passive.order_key and passive.native_state=='ended' and not passive.cancel_request_id:
                handles.append(live._cancel_exit(scope,passive,origin))
            continue
        if sell.current_sells or sell.protected and (sell.protected.native_state!='ended' or sell.protected.state not in ('terminal','rejected')):continue
        source=snapshot.observation.source_frame.sources.get('market:'+scope[1]+':'+scope[2])
        book=source.value.get('book') if source and source.complete and not source.error else None
        if not book:continue
        remaining=sell.residual_quantity.value;quantity=Decimal(0);price=None
        for row in sorted(book['bids'],key=lambda row:row['price'],reverse=True):
            used=min(remaining,row['size']);quantity+=used;remaining-=used
            if used:price=row['price']
            if remaining<=0:break
        if quantity and price and quantity>=book.get('min_order_size',0):
            handles.append(live._start_exit(scope,'protected_exit',quantity,sold,price,origin))
    return tuple(handle for handle in handles if handle is not None)


def test_d2_opening_stop_loss_is_current_and_latched_once(tmp_path, monkeypatch):
    from tests.test_lp_order_registration_contract import CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch)
    scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
    def tick():
        live.run_once()
        attempts = (explicit_historical_protection(live, loss=Decimal('5'))
            if public.bid <= Decimal('.25') else live.run_protection_once())
        for attempt in attempts: attempt.result(timeout=3)
        return live.state.snapshot()
    try:
        # Missing actual fee proof remains UNKNOWN; it cannot be defaulted to zero.
        original = sdk.trades
        sdk.trades = tuple(t.model_copy(update={'maker_orders': tuple(m.model_copy(
            update={'fee_rate_bps': None}) for m in t.maker_orders)}) for t in original)
        tick()
        unknown = tick().openings[scope]
        assert unknown.economics.opening_loss_usd.state == 'unknown'
        assert unknown.stop_loss_latched.value is False and len(sdk.posts) == 3
        sdk.trades = original
        public.bid = Decimal('.251')
        clock[0] = 2
        tick()  # real own-fill receipt invalidates this accepted observation
        snapshot = tick()  # only the next ordinary current frame can authorize an exit
        opening, sell = snapshot.openings[scope], snapshot.sells[scope]
        assert opening.economics.buy_cost_usd.value == 30
        assert opening.economics.sold_revenue_usd.value == 10 and opening.economics.fees_usd.value == 0
        assert opening.economics.residual_exit_value_usd.value == Decimal('15.06')
        assert opening.economics.opening_loss_usd.value == Decimal('4.94')
        assert opening.stop_loss_latched.value is False and sell.residual_quantity.value == 60
        assert sell.passive.submit_quantity.value == 60 and sell.passive.submit_sold_quantity.value == 40
        assert sell.passive.price.value == Decimal('.32') and sell.passive.order_type.value == 'GTC'
        assert sdk.posts[-1]['side'] == 'SELL' and sdk.posts[-1]['size'] == 60
        assert sdk.posts[-1]['expiration'] is None and sdk.posts[-1]['post_only'] is True
        clock[0] = 3
        public.bid = Decimal('.25')
        snapshot = tick()
        opening = snapshot.openings[scope]
        assert opening.stop_loss_latched.value is True
        assert opening.stop_loss_triggered_loss_usd.value == Decimal('5.00')
        assert sdk.cancels == [('passive-exit',)] and sdk.market_orders == []
        latched_at = opening.stop_loss_triggered_at
        assert snapshot.sells[scope].passive.cancel_request_id != snapshot.sells[scope].passive.request_id
        tick()  # ID-only ACK / missing exact final quantity is not final zero
        assert sdk.market_orders == []
        sdk.allow_exact = True
        tick()  # authoritative exact final matched0 receipt, old frame now stale
        assert sdk.market_orders == []
        snapshot = tick()  # joint real native-ended/final0 + fresh order/position reconciliation
        assert len(sdk.market_orders) == 1
        assert sdk.market_orders[0]['shares'] == 60 and sdk.market_orders[0]['min_price'] == Decimal('.25')
        passive, protected = snapshot.sells[scope].passive, snapshot.sells[scope].protected
        assert passive.state == 'terminal' and passive.native_state == 'ended' and passive.own_fill_evidence is None
        assert passive.submit_sold_quantity.value == 40 and protected.submit_sold_quantity.value == 40
        assert protected.submit_quantity.value == protected.submit_residual_quantity.value == 60
        clock[0] = 4
        public.bid = Decimal('.30')
        snapshot = tick()
        assert snapshot.openings[scope].stop_loss_latched.value is True
        assert snapshot.openings[scope].stop_loss_triggered_at == latched_at
        assert len(sdk.market_orders) == 1 and sdk.cancels == [('passive-exit',)]
        assert set(sdk.exact_reads) == {'manual-sale', 'passive-exit'}
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_sell_reconciliation_preserves_real_quantity_and_pending_attempt(tmp_path, monkeypatch):
    from concurrent.futures import TimeoutError as NativeTimeout
    from threading import Event
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_order_registration_contract import _open_order, _maker_order, _trade, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
    entered, release = Event(), Event()
    def post(signed):
        sdk.posts.append(signed)
        if signed['order_type'] == 'GTC':
            sdk.orders += (_open_order('held-passive', 'SELL', price='.32', original='100').model_copy(
                update={'created_at': live.state.snapshot().orders[0].source.checked_at}),)
            entered.set()
            assert release.wait(5), 'passive native post completion watchdog'
            return AcceptedOrder(order_id='held-passive', status='live', making_amount=Decimal('0'),
                taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
        sdk.orders += (_open_order('protected-after-held', 'SELL', price=str(signed['min_price']),
            original=str(signed['shares']), order_type='FOK'),)
        return AcceptedOrder(order_id='protected-after-held', status='live', making_amount=Decimal('0'),
            taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
    sdk.post_order = post
    def tick(*, wait=True):
        live.run_once()
        attempts = (explicit_historical_protection(live, loss=Decimal('5'))
            if public.bid <= Decimal('.25') else live.run_protection_once())
        if wait:
            for a in attempts: a.result(timeout=3)
        return attempts
    try:
        tick()  # own BUY100 receipt
        pending, = tick(wait=False)
        assert entered.wait(2)
        before = live.state.snapshot()
        assert before.openings[scope].phase == 'passive_exit'
        submitted = before.sells[scope].passive
        assert submitted.submit_quantity.value == submitted.submit_residual_quantity.value == 100
        assert submitted.submit_sold_quantity.value == 0 and submitted.native_state == 'running'
        with pytest.raises(NativeTimeout): pending.result(timeout=.01)
        assert live.status()['native_inflight'] == 1 and not pending.done()
        # Position60 arrives before this request's sale proof; terminal flag is insufficient.
        clock[0] = 2
        public.bid = Decimal('.20')
        sdk.orders = tuple(o.model_copy(update={'status': 'CANCELED', 'size_matched': Decimal('40')})
            if o.id == 'held-passive' else o for o in sdk.orders)
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='60', average_price='.30'),)
        tick()
        assert live.state.snapshot().sells[scope].position_reconciled is False and sdk.market_orders == []
        sale = _trade('held-sale40', _maker_order('held-passive', 'SELL', '40', '.32'), size='40')
        foreign = sale.model_copy(update={'maker_orders': tuple(m.model_copy(
            update={'maker_address': '0x'+'f'*40, 'owner': WALLET}) for m in sale.maker_orders)})
        sdk.trades += (foreign,)
        tick()
        assert (*scope[:3], 'held-passive') not in live.state.snapshot().own_fills
        assert sdk.market_orders == []  # credential owner is not wallet evidence
        # Exact own sale40 arrives while API still says100: opposite mismatch also blocks.
        sdk.trades = (sdk.trades[0], sale, sale)  # duplicate real trade ID contributes once
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='100', average_price='.30'),)
        tick()
        tick()
        snap = live.state.snapshot()
        assert snap.own_fills[(*scope[:3], 'held-passive')].quantity.value == 40
        assert not snap.sells[scope].position_reconciled and sdk.market_orders == []
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='60', average_price='.30'),)
        tick()
        assert live.state.snapshot().sells[scope].position_reconciled is True
        live.request_running(False)
        settle_control(live)
        live.request_running(True)
        settle_control(live)
        tick()
        assert not pending.done() and live.status()['native_inflight'] == 1
        assert sdk.market_orders == [] and live.state.snapshot().sells[scope].passive.request_id == submitted.request_id
        release.set()
        assert pending.result(timeout=3).status == 'accepted'
        assert live.status()['last_current_evidence'] == (submitted.request_id, 'sell_receipt', 'stale_evidence')
        retained_order = next(o for o in live.state.snapshot().orders if o.key[3] == 'held-passive')
        assert retained_order.size_matched.value == 40 and retained_order.terminal_proof.matched.value == 40
        tick()
        assert len(sdk.market_orders) == 1 and sdk.market_orders[0]['shares'] == 60
        snap = live.state.snapshot()
        passive, protected = snap.sells[scope].passive, snap.sells[scope].protected
        assert passive.request_id == submitted.request_id and passive.state == 'terminal'
        assert passive.native_state == 'ended' and passive.submit_sold_quantity.value == 0
        assert passive.own_fill_evidence.quantity.value == 40 and protected.submit_sold_quantity.value == 40
        assert protected.submit_quantity.value == protected.submit_residual_quantity.value == 60
        tick()
        assert len(sdk.market_orders) == 1  # no duplicate replacement while current protected order unresolved
    finally:
        release.set()
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_protection_evidence_is_exact_and_market_bounded(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from tests.test_lp_order_registration_contract import CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
    release, entered, scoring_calls = Event(), Event(), []
    pool = ThreadPoolExecutor(max_workers=1)
    def tick():
        live.run_once()
        reads = (sdk.balance_reads, sdk.order_reads, sdk.position_reads, public.book_reads)
        attempts = (explicit_historical_protection(live, loss=Decimal('5'))
            if public.bid <= Decimal('.25') else live.run_protection_once())
        for a in attempts:
            outcome = a.result(timeout=3)
            assert getattr(outcome.status, 'value', outcome.status) == 'accepted'
            a._thread.join(timeout=2)
            assert a.done(), 'protection tick must await actual native completion'
        assert reads == (sdk.balance_reads, sdk.order_reads, sdk.position_reads, public.book_reads)
        return live.state.snapshot()
    try:
        trade = sdk.trades[0]
        maker = trade.maker_orders[0]
        bad = (trade.model_copy(update={'maker_orders': (maker.model_copy(update={'maker_address': None, 'owner': WALLET}),)}),
            trade.model_copy(update={'condition_id': '0x'+'f'*64}),
            trade.model_copy(update={'maker_orders': (maker.model_copy(update={'token_id': '0x'+'e'*64}),)}),
            trade.model_copy(update={'maker_orders': (maker.model_copy(update={'price': Decimal('.31')}),)}),
            None)
        for malformed in bad:
            clock[0] += 61
            sdk.trades = None if malformed is None else (malformed,)
            snapshot = tick()
            assert snapshot.openings[scope].economics.opening_loss_usd.state == 'unknown'
            assert sdk.market_orders == [] and len(sdk.posts) == 1
        clock[0] += 61
        sdk.trades = (trade,)
        tick()  # actual complete own-fill receipt
        snapshot = tick()
        assert snapshot.sells[scope].passive.submit_quantity.value == 100
        assert sdk.exact_reads == []  # complete current terminal BUY is not re-read
        public.bid = Decimal('.20')
        tick()
        assert sdk.cancels == [('passive-exit',)] and sdk.market_orders == []
        final = sdk.terminal_orders['passive-exit']
        sdk.allow_exact = True
        for malformed in (final.model_copy(update={'maker_address': '0x'+'f'*40, 'owner': WALLET}),
                final.model_copy(update={'token_id': '0x'+'e'*64}),
                final.model_copy(update={'side': 'BUY'}),
                final.model_copy(update={'original_size': Decimal('200')}),
                final.model_copy(update={'size_matched': None})):
            clock[0] += 61
            sdk.terminal_orders['passive-exit'] = malformed
            tick()
            assert sdk.market_orders == []
            exact = next(o for o in live.state.snapshot().orders if o.key[3] == 'passive-exit')
            assert exact.terminal_proof.matched.state == 'unknown'
        clock[0] += 61
        sdk.terminal_orders['passive-exit'] = final
        tick()
        assert sdk.market_orders == []  # exact receipt invalidates its old ordinary observation
        tick()
        assert len(sdk.market_orders) == 1 and sdk.market_orders[0]['shares'] == 100
        assert set(sdk.exact_reads) == {'passive-exit'}
        assert all(q == dict(market=CONDITION_ID) for q in sdk.fill_queries)
        # A held scoring native call must time out locally and retain the shared
        # reader slot. Logical resume cannot release it or start a replacement.
        def scoring(*, order_id):
            scoring_calls.append(order_id)
            assert order_id == 'protected-exit'
            entered.set()
            assert release.wait(5), 'protection scoring native completion watchdog'
            return True
        sdk.get_order_scoring = scoring
        clock[0] += 5
        live.run_once()
        read = pool.submit(live.run_protection_once, timeout=.02)
        assert entered.wait(2)
        assert read.result(timeout=1) == ()  # independent real watchdog; no business-clock sleep
        assert not live.reader.drain(timeout=0)
        live.request_running(False)
        settle_control(live)
        live.request_running(True)
        settle_control(live)
        live.run_once()
        assert live.run_protection_once(timeout=.02) == ()
        assert scoring_calls == ['protected-exit'] and not live.reader.drain(timeout=0)
        assert len(sdk.market_orders) == 1
        release.set()
        assert live.reader.drain(timeout=2)
        clock[0] += 5
        live.run_once()
        live.run_protection_once()
        assert scoring_calls == ['protected-exit', 'protected-exit']
        assert live.state.snapshot().openings[scope].scoring.role == 'protected_exit'
    finally:
        release.set()
        pool.shutdown(wait=True)
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_d2_complementary_maker_fill_uses_exact_own_leg(tmp_path, monkeypatch, side):
    from polymarket.pagination import Paginator, Page
    from tests.test_lp_order_registration_contract import _trade, _maker_order, CONDITION_ID, TOKEN_ID, NO_TOKEN_ID
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=side == 'SELL')
    pages, queries = [], []
    target_id = 'entry-A' if side == 'BUY' else 'manual-sale'
    target = next(t for t in sdk.trades if t.maker_orders[0].order_id == target_id)
    complementary = target.model_copy(update={'token_id': NO_TOKEN_ID,
        'price': Decimal('.70') if side == 'BUY' else Decimal('.75'), 'size': Decimal('777')})
    related = tuple(complementary if t is target else t for t in sdk.trades)
    unrelated = _trade('unrelated-valid-leg', _maker_order('not-target', 'BUY', '2', '.70'), size='2').model_copy(
        update={'token_id': NO_TOKEN_ID, 'maker_orders': (_maker_order('not-target', 'BUY', '2', '.70').model_copy(
            update={'token_id': NO_TOKEN_ID, 'maker_address': '0x'+'f'*40}),)})
    def trades(**kwargs):
        queries.append(kwargs)
        def fetch(cursor):
            pages.append(cursor)
            if cursor is None: return Page((unrelated,), True, 'related-page2')
            assert cursor == 'related-page2'
            return Page(related, False)
        return Paginator(fetch=fetch)
    sdk.list_account_trades = trades
    try:
        live.run_once()
        assert live.run_protection_once() == ()  # actual own-fill application invalidates old observation
        own = live.state.snapshot().own_fills.get((WALLET, CONDITION_ID, TOKEN_ID, target_id))
        assert own is not None, 'complementary outer token must not erase exact own maker evidence'
        assert own.quantity.value == (100 if side == 'BUY' else 40)
        assert own.notional_usd.value == (30 if side == 'BUY' else 10)
        assert own.fees_usd.value == 0 and own.complete_for_target
        assert own.fill_ids == (target.id,) and own.source.checked_at is not None
        assert pages == [None, 'related-page2'] and queries == [dict(market=CONDITION_ID)]
        assert sdk.exact_reads == (['manual-sale'] if side == 'SELL' else []) and sdk.market_orders == []
        assert len(sdk.posts) == (1 if side == 'BUY' else 2)
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('occupied_slot', [False, True])
def test_d2_queue_cancel_capacity_defers_only_unattempted_targets(tmp_path, monkeypatch, occupied_slot):
    from threading import Event, Condition
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch)
    release, changed = Event(), Condition()
    handles, occupant = [], None
    targets = {'entry-A', 'queue-B', 'queue-C'}
    def cancel(*, order_ids):
        with changed:
            sdk.cancels.append(order_ids)
            changed.notify_all()
        assert release.wait(5), 'capacity-held actual native cancel completion watchdog'
        if order_ids == ('entry-A',):
            raise TimeoutError('actual exact entry cancellation remains UNKNOWN')
        sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    def sent(): return [oid for ids in sdk.cancels for oid in ids if oid in targets]
    try:
        if occupied_slot:
            sdk.orders += (_open_order('occupied-native', 'BUY', price='.29', original='100'),)
        live.run_once()
        live.run_protection_once()
        if occupied_slot:
            occupant = live.cancel_independent((WALLET, CONDITION_ID, TOKEN_ID, 'occupied-native'))
            with changed: assert changed.wait_for(lambda: ('occupied-native',) in sdk.cancels, timeout=2)
        sdk.orders += (_open_order('queue-B', 'BUY', price='.30', original='600', matched='100'),
            _open_order('queue-C', 'BUY', price='.30', original='100'))
        public.level = Decimal('4000')  # 2600 own / 1400 front: current trigger
        clock[0] = 1
        live.run_once()
        handles = list(live.run_protection_once(timeout=.02))
        with changed: assert changed.wait_for(lambda: len(sent()) == 3, timeout=2)
        assert live.status()['native_inflight'] == (4 if occupied_slot else 3)
        initial = set(sent())
        snap = live.state.snapshot()
        requested = {t.order_key[3] for p in snap.progress.values() for t in p.cancel_targets}
        assert requested == initial  # deferred IDs are not fabricated attempted requests
        assert requested == targets
        live.run_protection_once(timeout=.02)
        assert set(sent()) == initial and len(sent()) == len(initial)
        if occupied_slot:
            # A full fill makes deferred C inapplicable; partial B must still continue.
            sdk.orders = tuple(o.model_copy(update={'size_matched': Decimal('100')}) if o.id == 'queue-C' else o for o in sdk.orders)
        live.request_running(False)
        settle_control(live)
        live.request_running(True)
        settle_control(live)
        assert live.run_protection_once(timeout=.02) == ()  # old frame is not current after resume
        assert set(sent()) == initial
        release.set()
        for h in handles:
            h.result(timeout=3);h._thread.join(3)
        if occupant:
            occupant.result(timeout=3);occupant._thread.join(3)
        # Real completion restores capacity; resume cleanup must not replay
        # an attempted UNKNOWN or steal an unattempted protected target.
        clock[0] = 2
        live.run_once()
        more = live.run_protection_once()
        for h in more:
            h.result(timeout=3);h._thread.join(3)
        wanted = targets
        assert set(sent()) == wanted and sent().count('entry-A')==2 and all(sent().count(oid)==1 for oid in wanted-{'entry-A'})
        snap = live.state.snapshot()
        requested = [t for p in snap.progress.values() for t in p.cancel_targets]
        assert {t.order_key[3] for t in requested} == wanted
        assert next(t for t in requested if t.order_key[3] == 'entry-A').local_outcome == 'unknown_error'
        sdk.orders += (_open_order('new-healthy', 'BUY', price='.30', original='100'),
            _open_order('new-filled', 'BUY', price='.30', original='100', matched='100'),
            _open_order('new-terminal', 'BUY', price='.30', original='100', status='CANCELED'),
            _open_order('new-other-price', 'BUY', price='.28', original='100'),
            _open_order('new-sell', 'SELL', price='.30', original='100'))
        public.level = Decimal('10000')
        clock[0] = 3
        live.run_once()
        latest=live.run_protection_once()
        for h in latest:
            h.result(timeout=3);h._thread.join(3)
        assert set(sent()) == wanted and sent().count('entry-A')==3
        assert all(sent().count(oid)==1 for oid in wanted-{'entry-A'})
        assert all(oid in wanted | {'occupied-native'} for ids in sdk.cancels for oid in ids)
    finally:
        release.set()
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('changed_frame', ['position', 'manual_sell'])
def test_d2_sell_admission_rejects_changed_calculation_observation(tmp_path, monkeypatch, changed_frame):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
    entered, release = Event(), Event()
    creates = []
    create = sdk.create_limit_order
    def sign(**kwargs):
        creates.append(kwargs)
        return create(**kwargs)
    sdk.create_limit_order = sign
    original = type(live)._start_exit
    def admit(self, *args, **kwargs):
        entered.set()
        assert release.wait(5), 'calculation-to-admission scheduling watchdog'
        return original(self, *args, **kwargs)
    monkeypatch.setattr(type(live), '_start_exit', admit)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        live.run_once()
        assert live.run_protection_once() == ()  # real BUY own-fill receipt
        live.run_once()
        calculated_observation = live.state.snapshot().observation.observation_id
        pending = pool.submit(live.run_protection_once)
        assert entered.wait(2)
        calculated = live.state.snapshot().sells[scope]
        assert calculated.position_reconciled and calculated.residual_quantity.value == 100
        if changed_frame == 'position':
            sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='60', average_price='.30'),)
        else:
            sdk.orders += (_open_order('concurrent-manual', 'SELL', price='.32', original='100').model_copy(
                update={'created_at': live.state.snapshot().observation.components['positions'].checked_at}),)
        clock[0] += 1
        live.run_once()
        assert live.state.snapshot().observation.observation_id != calculated_observation
        release.set()
        for handle in pending.result(timeout=3): handle.result(timeout=3)
        assert len(sdk.posts) == 1, 'stale SELL100 must never reach SDK create/post'
        assert creates == []
        assert live.state.snapshot().sells[scope].passive is None
        # A fresh calculation retains the current position mismatch or live
        # manual occupancy; neither authorizes a replacement SELL100.
        live.run_once()
        assert live.run_protection_once() == ()
        fresh = live.state.snapshot().sells[scope]
        if changed_frame == 'position':
            assert fresh.residual_quantity.value == 60 and not fresh.position_reconciled
        else:
            assert {o.key[3] for o in fresh.current_sells} == {'concurrent-manual'}
        assert len(sdk.posts) == 1 and sdk.market_orders == []
        # A later complete venue frame can again reconcile the original100;
        # only this new calculation may publish a pending request.
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='100', average_price='.30'),)
        if changed_frame == 'manual_sell':
            sdk.cancel_orders(order_ids=('concurrent-manual',))
            sdk.allow_exact = True
            live.run_once()
            assert live.run_protection_once() == ()  # authoritative final0 invalidates old frame
        clock[0] += 1
        live.run_once()
        attempts = live.run_protection_once()
        for handle in attempts: handle.result(timeout=3)
        fresh = live.state.snapshot().sells[scope].passive
        assert fresh.submit_quantity.value == fresh.submit_residual_quantity.value == 100
        assert fresh.submit_sold_quantity.value == 0 and fresh.native_state == 'ended'
        assert len(sdk.posts) == 2 and sdk.posts[-1]['size'] == 100
    finally:
        release.set()
        pool.shutdown(wait=True)
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('zero_view', ['explicit_zero', 'complete_absent'])
def test_d2_completed_opening_starts_distinct_economics_episode(tmp_path, monkeypatch, zero_view):
    from datetime import timedelta
    from polymarket.models.clob.order_response import AcceptedOrder
    from open_trader.polymarket_lp_market_reader import SourceResult
    from tests.test_lp_order_registration_contract import _open_order, _maker_order, _trade, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    first_scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
    position_queries, retirements = [], []
    original_retire = type(live.state).retire_exit_attempts
    def retire(owner, guard):
        before = owner.snapshot()
        result = original_retire(owner, guard)
        retirements.append((guard, result, before, owner.snapshot()))
        return result
    monkeypatch.setattr(type(live.state), 'retire_exit_attempts', retire)
    if zero_view == 'complete_absent':
        def positions(**kwargs):
            position_queries.append(kwargs)
            if sdk.positions is None: return None
            threshold = Decimal(str(kwargs.get('size_threshold', 1)))
            return tuple(p for p in sdk.positions if Decimal(p['size']) >= threshold)
        sdk.list_positions = positions
    def tick():
        live.run_once()
        for h in live.run_protection_once(): h.result(timeout=3)
        return live.state.snapshot()
    try:
        public.ask = Decimal('.40')
        tick()
        first = tick().sells[first_scope].passive
        assert first.submit_quantity.value == 100 and first.submit_sold_quantity.value == 0
        terminal = next(o for o in sdk.orders if o.id == 'passive-exit').model_copy(
            update={'status': 'FILLED', 'size_matched': Decimal('100')})
        sdk.orders = ()  # real open-order API never retains terminal history
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='0', average_price='.30'),)
        clock[0] += 1
        assert tick().openings[first_scope].phase != 'closed'  # missing exact final fill is unresolved
        sdk.allow_exact = True
        sdk.terminal_orders['passive-exit'] = terminal
        stamp = terminal.created_at+timedelta(seconds=1)
        sale = _trade('first-sale-fill', _maker_order('passive-exit', 'SELL', '100', '.40'), size='100').model_copy(
            update={'matched_at': stamp, 'updated_at': stamp})
        sdk.trades += (sale,)
        tick()  # authoritative terminal + monetary receipts invalidate old frame
        if zero_view == 'complete_absent':
            sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='.5', average_price='.30'),)
            dust = tick()
            assert dust.openings[first_scope].phase != 'closed'
            assert next(p for p in dust.positions if p.token_id == TOKEN_ID).quantity.value == Decimal('.5')
            sdk.positions = None
            clock[0] += 1
            assert tick().openings[first_scope].phase != 'closed'
            sdk.positions = ()
            clock[0] += 61
        closed = tick()
        assert closed.openings[first_scope].phase == 'closed', 'fully resolved zero inventory must close this episode'
        assert closed.openings[first_scope].economics.buy_cost_usd.value == 30
        assert closed.openings[first_scope].economics.sold_revenue_usd.value == 40
        assert closed.openings[first_scope].economics.residual_exit_value_usd.value == 0
        assert closed.openings[first_scope].economics.fees_usd.value == 0
        if zero_view == 'complete_absent':
            assert closed.positions == ()  # no fabricated canonical zero row
            assert position_queries and all(q == dict(size_threshold=0) for q in position_queries)
        assert [result for _,result,_,_ in retirements] == ['applied'], 'completion must call the actual retirement port'
        _, _, before_retire, after_retire = retirements[0]
        original_proof = before_retire.sells[first_scope].passive
        assert original_proof.request_id == first.request_id and original_proof.state == 'terminal'
        for field in ('submit_quantity', 'submit_sold_quantity', 'submit_residual_quantity', 'price', 'order_type', 'expiration'):
            assert getattr(original_proof, field) == getattr(first, field)
        assert closed.sells[first_scope].passive is None and closed.sells[first_scope].protected is None
        assert all(scope != first_scope for scope,_ in closed.resolved_exit_attempts)
        assert after_retire.trade_version == before_retire.trade_version
        assert after_retire.own_fills == before_retire.own_fills and after_retire.orders == before_retire.orders
        def buy(signed):
            assert signed['side'] == 'BUY'
            sdk.posts.append(signed)
            sdk.orders = (_open_order('entry-B', 'BUY', price='.30', original='100').model_copy(
                update={'created_at': live._utc_now()}),)
            return AcceptedOrder(order_id='entry-B', status='live', making_amount=Decimal('0'),
                taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
        sdk.post_order = buy
        clock[0] += 1
        book = live.trading.lp_order_books((TOKEN_ID,))[TOKEN_ID]
        candidate = SourceResult('second-opening-candidate', (dict(condition_id=CONDITION_ID,
            market_id='market-1', token_id=TOKEN_ID, venue='polymarket', price=Decimal('.30'),
            quantity=Decimal('100'), order_type='GTC', expiration=None, post_only=True, send_options=(),
            direction=dict(market=dict(reward_min_size=Decimal('1'), reward_max_spread=Decimal('.10')),
                book=book, daily_pool_usd=Decimal('24'))),), checked_at=book['received_at'])
        result = live.run_once(candidate)
        assert [o.status.value for o in result.outcomes] == ['accepted']
        snapshot = live.state.snapshot()
        second_scope = next(s for s,o in snapshot.openings.items() if 'entry-B' in o.entry_order_ids)
        assert second_scope != first_scope and second_scope[:3] == first_scope[:3]
        assert snapshot.openings[first_scope].phase == 'closed'
        sdk.orders = (sdk.orders[0].model_copy(update={'status': 'FILLED', 'size_matched': Decimal('100')}),)
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='100', average_price='.30'),)
        stamp = sdk.orders[0].created_at+timedelta(seconds=1)
        sdk.trades += (_trade('second-buy-fill', _maker_order('entry-B', 'BUY', '100', '.30'), size='100').model_copy(
            update={'matched_at': stamp, 'updated_at': stamp}),)
        public.bid = Decimal('.25')
        # The original exact history remains returned by account-trades; it
        # cannot become new-episode economics or occupy its exit slot.
        sdk.post_order = lambda signed: dict(accepted=False)
        tick()
        snapshot = tick()
        new = snapshot.openings[second_scope]
        assert new.entry_order_ids == ('entry-B',)
        assert new.economics.buy_cost_usd.value == 30 and new.economics.sold_revenue_usd.value == 0
        assert new.economics.opening_loss_usd.value == 5 and new.stop_loss_latched.value is False
        assert snapshot.sells[second_scope].passive.submit_quantity.value == 100
        assert snapshot.sells[second_scope].passive.submit_sold_quantity.value == 0
        assert snapshot.sells[first_scope].passive is None and snapshot.sells[first_scope].protected is None
        assert original_proof.submit_quantity == first.submit_quantity
        assert [result for _,result,_,_ in retirements] == ['applied']
        assert snapshot.openings[first_scope].phase == 'closed'
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_manual_sell_disappearance_preserves_opening_attribution(tmp_path, monkeypatch):
    from datetime import timedelta
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_order_registration_contract import _open_order, _maker_order, _trade, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
    post = sdk.post_order
    def manual(signed):
        sdk.posts.append(signed)
        sdk.orders += (_open_order('disappearing-manual', 'SELL', price='.25', original='100').model_copy(
            update={'created_at': live.state.snapshot().observation.components['positions'].checked_at}),)
        return AcceptedOrder(order_id='disappearing-manual', status='live', making_amount=Decimal('0'),
            taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
    def tick():
        live.run_once()
        for h in live.run_protection_once(): h.result(timeout=3)
        return live.state.snapshot()
    try:
        sdk.post_order = manual
        manual(sdk.create_limit_order(token_id=TOKEN_ID, side='SELL', price=Decimal('.25'), size=Decimal('100'),
            expiration=None, post_only=False))
        sdk.post_order = post
        tick()  # current-process BUY proof and first fully covering manual order
        snapshot = tick()
        assert {o.key[3] for o in snapshot.sells[scope].current_sells} == {'disappearing-manual'}
        assert len(sdk.posts) == 2
        actual = next(o for o in sdk.orders if o.id == 'disappearing-manual')
        terminal = actual.model_copy(update={'status': 'CANCELED', 'size_matched': Decimal('40')})
        sdk.orders = tuple(o for o in sdk.orders if o.id != actual.id)
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='60', average_price='.30'),)
        public.bid = Decimal('.251')
        clock[0] += 1
        unknown = tick()
        assert not unknown.sells[scope].position_reconciled
        assert unknown.openings[scope].economics.opening_loss_usd.state == 'unknown'
        assert len(sdk.posts) == 2 and sdk.market_orders == []
        sdk.allow_exact = True
        sdk.terminal_orders[actual.id] = terminal
        stamp = actual.created_at+timedelta(seconds=1)
        fill = _trade('disappearing-fill', _maker_order(actual.id, 'SELL', '40', '.25'), size='40').model_copy(
            update={'matched_at': stamp, 'updated_at': stamp})
        # Foreign/historical exact IDs in the same market are not this episode.
        foreign = _trade('foreign-old-sale', _maker_order('unobserved-old-sale', 'SELL', '90', '.90'), size='90')
        sdk.trades += (fill, foreign)
        clock[0] += 61  # real reader retry deadline, not proof retimestamping
        tick()
        snapshot = tick()
        opening, sell = snapshot.openings[scope], snapshot.sells[scope]
        assert opening.economics.sold_revenue_usd.value == 10, 'disappearance must retain exact manual-sale ownership'
        assert sell.position_reconciled and sell.residual_quantity.value == 60
        assert opening.economics.buy_cost_usd.value == 30 and opening.economics.fees_usd.value == 0
        assert opening.economics.opening_loss_usd.value == Decimal('4.94')
        assert opening.stop_loss_latched.value is False
        assert sell.passive.submit_quantity.value == 60 and sell.passive.submit_sold_quantity.value == 40
        assert sdk.posts[-1]['size'] == 60 and sdk.posts[-1]['price'] == Decimal('.32')
        assert set(sdk.exact_reads) == {actual.id}
        assert all(q == dict(market=CONDITION_ID) for q in sdk.fill_queries)
        public.ask, public.bid = Decimal('.40'), Decimal('.25')
        clock[0] += 1
        snapshot = tick()
        assert snapshot.openings[scope].economics.opening_loss_usd.value == 5
        assert snapshot.openings[scope].stop_loss_latched.value is False
        assert sdk.cancels == [] and sdk.market_orders == []
        assert 'unobserved-old-sale' not in {o.key[3] for o in snapshot.orders}
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('first_result', ['filled', 'rejected', 'not_sent', 'unknown', 'native_pending'])
def test_d2_resolved_protected_exit_can_sell_only_reconciled_remainder(tmp_path, monkeypatch, first_result):
    from datetime import timedelta
    from threading import Event
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_order_registration_contract import _open_order, _maker_order, _trade, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
    entered, release = Event(), Event()
    retirement_observations = []
    original_retire = type(live.state).retire_exit_attempts
    def retire(owner, guard):
        before = owner.snapshot()
        result = original_retire(owner, guard)
        retirement_observations.append((result, before, owner.snapshot()))
        return result
    monkeypatch.setattr(type(live.state), 'retire_exit_attempts', retire)
    original_execute = type(live)._execute_exit
    def execute(self, *args, **kwargs):
        if first_result == 'not_sent' and not entered.is_set():
            entered.set()
            assert release.wait(5), 'pre-post native scheduling watchdog'
        return original_execute(self, *args, **kwargs)
    monkeypatch.setattr(type(live), '_execute_exit', execute)
    def post(signed):
        sdk.posts.append(signed)
        if signed['order_type'] != 'FOK':
            sdk.orders += (_open_order('manual-risk-blocker', 'SELL', price='.40', original='100').model_copy(
                update={'created_at': live.state.snapshot().observation.components['positions'].checked_at}),)
            return AcceptedOrder(order_id='manual-risk-blocker', status='live', making_amount=Decimal('0'),
                taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
        if len(sdk.market_orders) == 1:
            if first_result == 'rejected': return dict(accepted=False)
            if first_result == 'unknown': raise TimeoutError('actual first post UNKNOWN')
            if first_result == 'native_pending':
                entered.set()
                assert release.wait(5), 'actual pending post completion watchdog'
                raise TimeoutError('actual late post UNKNOWN')
        oid = 'protected-first' if len(sdk.market_orders) == 1 else 'protected-next'
        sdk.orders += (_open_order(oid, 'SELL', price=str(signed['min_price']), original=str(signed['shares']),
            order_type='FOK').model_copy(update={'created_at': live.state.snapshot().observation.components['positions'].checked_at}),)
        return AcceptedOrder(order_id=oid, status='live', making_amount=Decimal('0'),
            taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
    sdk.post_order = post
    handles = ()
    def tick():
        live.run_once()
        for handle in explicit_historical_protection(live): handle.result(timeout=3)
        return live.state.snapshot()
    try:
        sdk.post_order(sdk.create_limit_order(token_id=TOKEN_ID, side='SELL', price=Decimal('.40'), size=Decimal('100'),
            expiration=None, post_only=False))
        public.bid, public.level = Decimal('.20'), Decimal('1000')
        tick()  # actual BUY own-fill proof
        assert tick().openings[scope].stop_loss_latched.value is True
        assert sdk.market_orders == []  # real current manual occupancy prevents an exit
        sdk.cancel_orders(order_ids=('manual-risk-blocker',))
        sdk.allow_exact = True
        tick()  # exact manual terminal0 receipt; no invented cancellation fill
        public.level = Decimal('40')
        live.run_once()
        handles = explicit_historical_protection(live)
        assert len(handles) == 1
        if first_result in ('native_pending', 'not_sent'):
            assert entered.wait(2)
        else:
            handles[0].result(timeout=3)
        first = live.state.snapshot().sells[scope].protected
        assert first.submit_quantity.value == 40 and first.submit_residual_quantity.value == 100
        assert first.submit_sold_quantity.value == 0 and first.price.value == Decimal('.20')
        if first_result == 'filled':
            assert live.status()['last_current_evidence'] == (first.request_id, 'sell_receipt', 'applied')
        if first_result in ('not_sent', 'unknown', 'native_pending'):
            live.request_running(False)
            settle_control(live)
            live.request_running(True)
            settle_control(live)
        if first_result == 'not_sent':
            release.set()
            assert handles[0].result(timeout=3).status == 'not_sent'
            assert sdk.market_orders == [] and len(sdk.posts) == 2
        elif first_result == 'filled':
            order = next(o for o in sdk.orders if o.id == 'protected-first')
            sdk.orders = tuple(o for o in sdk.orders if o.id != order.id)
            sdk.allow_exact = True
            sdk.terminal_orders[order.id] = order.model_copy(update={'status': 'FILLED', 'size_matched': Decimal('40')})
            stamp = order.created_at+timedelta(seconds=1)
            sdk.trades += (_trade('first-protected-fill', _maker_order(order.id, 'SELL', '40', '.20').model_copy(
                update={'fee_rate_bps': None}), size='40').model_copy(
                update={'matched_at': stamp, 'updated_at': stamp}),)
            sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='60', average_price='.30'),)
            clock[0] += 1
            filled_snapshot = tick()  # real final quantities/money invalidate old observation
            own = filled_snapshot.own_fills.get(first.order_key)
            assert own is not None and own.quantity.value == 40, (live.status()['last_current_evidence'],
                tuple(o for o in filled_snapshot.orders if o.key == first.order_key), sdk.fill_queries)
        public.level = Decimal('1000')
        clock[0] += 1
        snapshot = tick()
        current = snapshot.sells[scope].protected
        if first_result in ('unknown', 'native_pending'):
            assert current.request_id == first.request_id
            assert len(sdk.market_orders) == 1 and len(sdk.posts) == 3
            assert current.native_state == ('running' if first_result == 'native_pending' else 'ended')
            assert live.status()['native_inflight'] == (1 if first_result == 'native_pending' else 0)
            assert (scope, first.request_id) not in snapshot.resolved_exit_attempts
        else:
            expected = 60 if first_result == 'filled' else 100
            assert current.request_id != first.request_id, 'a fully resolved protected request must not permanently block its remainder'
            assert current.submit_quantity.value == current.submit_residual_quantity.value == expected
            assert current.submit_sold_quantity.value == (40 if first_result == 'filled' else 0)
            assert snapshot.openings[scope].stop_loss_latched.value is True
            assert first.submit_quantity.value == 40 and first.submit_residual_quantity.value == 100
            retained = snapshot.resolved_exit_attempts[(scope, first.request_id)]
            assert retained.request_id == first.request_id and retained.native_state == 'ended'
            assert retained.state == ('terminal' if first_result == 'filled' else 'rejected')
            for field in ('submit_quantity', 'submit_sold_quantity', 'submit_residual_quantity', 'price', 'order_type', 'expiration'):
                assert getattr(retained, field) == getattr(first, field)
            if first_result != 'filled':
                assert retained.order_key is None and retained.own_fill_evidence is None
            else:
                assert retained.order_key == first.order_key and retained.own_fill_evidence.quantity.value == 40
                assert retained.own_fill_evidence.fees_usd.state == 'unknown'
                sdk.trades = tuple(t.model_copy(update={'maker_orders': tuple(m.model_copy(
                    update={'fee_rate_bps': Decimal('0')}) for m in t.maker_orders)})
                    if t.id == 'first-protected-fill' else t for t in sdk.trades)
                clock[0] += 1
                live.run_once()
                before_late = live.state.snapshot()
                assert explicit_historical_protection(live) == ()  # actual late fee proof fences this ordinary frame
                assert live.status()['last_current_evidence'] == (first.request_id, 'protection_own_fill', 'applied')
                late = live.state.snapshot()
                assert late.sells[scope] == before_late.sells[scope]  # current native/quantity/source projection is untouched
                assert late.trade_version == before_late.trade_version+1
                improved = late.resolved_exit_attempts[(scope, first.request_id)]
                assert improved.own_fill_evidence.fees_usd.value == 0
                assert improved.own_fill_evidence.quantity.value == 40
                assert late.own_fills[first.order_key] == improved.own_fill_evidence
                assert snapshot.resolved_exit_attempts[(scope, first.request_id)].own_fill_evidence.fees_usd.state == 'unknown'
                for field in ('submit_quantity', 'submit_sold_quantity', 'submit_residual_quantity', 'price', 'order_type', 'expiration'):
                    assert getattr(improved, field) == getattr(first, field)
                # Finish the real second60, then retire the full old/current
                # submit context only after fresh complete zero coverage.
                next_order = next(o for o in sdk.orders if o.id == 'protected-next')
                sdk.orders = tuple(o for o in sdk.orders if o.id != next_order.id)
                sdk.terminal_orders[next_order.id] = next_order.model_copy(
                    update={'status': 'FILLED', 'size_matched': Decimal('60')})
                stamp = next_order.created_at+timedelta(seconds=1)
                sdk.trades += (_trade('next-protected-fill', _maker_order(next_order.id, 'SELL', '60', '.20'), size='60').model_copy(
                    update={'matched_at': stamp, 'updated_at': stamp}),)
                sdk.positions = ()
                clock[0] += 1
                tick()  # exact terminal and monetary receipts
                completed = tick()
                assert completed.openings[scope].phase == 'closed'
                assert [result for result,_,_ in retirement_observations] == ['applied']
                _, before_retire, after_retire = retirement_observations[0]
                assert before_retire.resolved_exit_attempts[(scope, first.request_id)] == improved
                assert before_retire.sells[scope].protected.request_id == current.request_id
                assert before_retire.sells[scope].protected.own_fill_evidence.quantity.value == 60
                assert after_retire.resolved_exit_attempts == {}
                assert completed.sells[scope].passive is None and completed.sells[scope].protected is None
                assert completed.sells[scope].residual_quantity.value == 0
                assert completed.openings[scope].economics.sold_revenue_usd.value == 20
                assert completed.openings[scope].stop_loss_latched.value is True
                assert after_retire.orders == before_retire.orders and after_retire.own_fills == before_retire.own_fills
                # Actual old SDK proof has no authority to resurrect retired
                # submission context. Observe the real public disposition.
                from uuid import uuid4
                from open_trader.polymarket_lp_current_state import ReceiptEvidence
                old_order = next(o for o in completed.orders if o.key == first.order_key)
                receipt = ReceiptEvidence(uuid4().hex, first.request_id, WALLET, live.process_epoch,
                    completed.current.cycle_generation, first.order_key, 'SELL', old_order.terms_version,
                    own_fill=improved.own_fill_evidence, exit_attempt=improved, scope=scope)
                assert live.state.accept_receipt_evidence(receipt) == 'stale_evidence'
                assert live.state.snapshot() == completed
                assert len(sdk.market_orders) == 2  # no third send after discharged zero
    finally:
        release.set()
        for handle in handles: handle.result(timeout=3)
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('anchor_origin', ['manual', 'submitted'])
@pytest.mark.parametrize('final_matched', ['0', '1'])
def test_d2_deferred_queue_cancel_survives_successful_anchor_removal(tmp_path, monkeypatch, final_matched, anchor_origin):
    from threading import Event, Condition
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch, submit=anchor_origin == 'submitted')
    manual_trade_queries = []
    if anchor_origin == 'manual':
        trades = sdk.list_account_trades
        def account_trades(**kwargs):
            manual_trade_queries.append(kwargs)
            return trades(**kwargs)
        sdk.list_account_trades = account_trades
        sdk.orders = (_open_order('entry-A', 'BUY', price='.30', original='2000'),)
        assert live.state.snapshot().openings == {} and sdk.posts == []
    releases = {oid: Event() for oid in ('entry-A', 'success-B', 'deferred-C')}
    changed, terminal, exact_reads = Condition(), {}, []
    allow_final = [False]
    handles = {}
    def cancel(*, order_ids):
        oid, = order_ids
        with changed:
            sdk.cancels.append(order_ids)
            changed.notify_all()
        if oid in releases:
            assert releases[oid].wait(5), 'actual anchor/peer cancellation completion watchdog'
        original = next(o for o in sdk.orders if o.id == oid)
        terminal[oid] = original.model_copy(update={'status': 'CANCELED',
            'size_matched': Decimal(final_matched) if oid == 'entry-A' else original.size_matched})
        sdk.orders = tuple(o for o in sdk.orders if o.id != oid)
        return dict(canceled=order_ids, not_canceled={})  # exact ID ACK has no final quantity
    def exact(*, order_id):
        exact_reads.append(order_id)
        return terminal.get(order_id) if allow_final[0] else None
    sdk.cancel_orders, sdk.get_order = cancel, exact
    def sent(): return [oid for ids in sdk.cancels for oid in ids]
    try:
        live.run_once()
        live.run_protection_once()
        baseline = next(b for b in live.state.snapshot().baselines.values()
            if b.key.kind == ('queue_bucket' if anchor_origin == 'submitted' else 'first_seen_episode'))
        if anchor_origin == 'manual':
            assert baseline.key.episode_id == 'entry-A'
            assert live.state.snapshot().openings == {} and live.state.snapshot().own_fills == {} and sdk.posts == []
        assert baseline.anchor_order_ids == ('entry-A',)
        sdk.orders += (_open_order('success-B', 'BUY', price='.30', original='600', matched='100'),
            _open_order('deferred-C', 'BUY', price='.30', original='100'))
        public.level, clock[0] = Decimal('4000'), 1
        live.run_once()
        attempts = live.run_protection_once()
        handles = {h.identity.target.existing_order_id: h for h in attempts}
        with changed: assert changed.wait_for(lambda: set(sent()) == {'entry-A', 'success-B', 'deferred-C'}, timeout=2)
        assert len(attempts) == 3
        releases['deferred-C'].set()
        handles['deferred-C']._thread.join(2)
        assert handles['deferred-C'].done() and live.status()['native_inflight'] == 2
        assert {t.order_key[3] for t in live.state.snapshot().progress[baseline.key].cancel_targets} == {'entry-A', 'success-B', 'deferred-C'}
        releases['entry-A'].set()
        assert handles['entry-A'].result(timeout=3).status.value == 'accepted'
        handles['entry-A']._thread.join(timeout=2)
        assert handles['entry-A'].done() and not handles['success-B'].done()
        assert live.status()['native_inflight'] == 1
        # A receipt invalidates its source observation; it grants no fresh send.
        assert live.run_protection_once() == ()
        releases['success-B'].set()
        handles['success-B'].result(timeout=3)
        handles['success-B']._thread.join(timeout=2)
        assert all(h.done() for h in handles.values()) and live.status()['native_inflight'] == 0
        live.run_once()
        assert live.run_protection_once() == ()
        assert set(sent()) == {'entry-A', 'success-B', 'deferred-C'} and len(sent()) == 3
        snapshot = live.state.snapshot()
        anchor = next(o for o in snapshot.orders if o.key[3] == 'entry-A')
        assert anchor not in snapshot.active_orders and anchor.presence == 'absent'
        assert anchor.terminal_proof.matched.state == 'unknown'
        unknown_source = anchor.terminal_proof.matched.sources
        assert unknown_source and anchor.size_matched.value == 0  # observed lower bound is not final0
        assert set(sent()) == {'entry-A', 'success-B', 'deferred-C'} and len(sent())==3
        assert {t.order_key[3] for t in snapshot.progress[baseline.key].cancel_targets} == {'entry-A', 'success-B', 'deferred-C'}
        allow_final[0] = True
        clock[0] += 61  # reader source retry deadline; no source proof is re-stamped by this test
        live.run_once()
        before_proof = live.state.snapshot()
        assert live.run_protection_once() == ()  # real exact final receipt fences old observation
        proven = live.state.snapshot()
        assert exact_reads and set(exact_reads) == {'entry-A','success-B','deferred-C'}, 'accepted deferred manual anchor needs its exact Reader proof without an opening'
        anchor = next(o for o in proven.orders if o.key[3] == 'entry-A')
        assert anchor.terminal_proof.identity_verified and anchor.terminal_proof.matched.value == Decimal(final_matched)
        assert anchor.terminal_proof.matched.sources != unknown_source
        assert proven.trade_version > before_proof.trade_version and anchor not in proven.active_orders
        assert live.run_protection_once() == ()  # no dispatch from the now-stale ordinary frame
        assert len(sent()) == 3
        clock[0] += 1
        live.run_once()
        remaining = live.run_protection_once()
        for h in remaining:
            h.result(timeout=3)
            h._thread.join(timeout=2)
            assert h.done()
        wanted = {'entry-A', 'success-B', 'deferred-C'}
        assert set(sent()) == wanted and len(sent()) == len(wanted), 'final0 anchor cancellation must preserve the original deferred target'
        snapshot = live.state.snapshot()
        assert {t.order_key[3] for t in snapshot.progress[baseline.key].cancel_targets} == wanted
        assert next(o for o in snapshot.orders if o.key[3] == 'entry-A') not in snapshot.active_orders
        sdk.orders += (_open_order('new-healthy-after-anchor', 'BUY', price='.30', original='100'),)
        public.level = Decimal('4000')  # C's genuine first-seen front3900; front3800/level4000=.95
        for _ in range(2):
            clock[0] += 1
            live.run_once()
            extra = live.run_protection_once()
            assert extra == (), tuple((p.key.kind, p.key.episode_id, p.reason_codes, p.ratio.value, tuple(t.order_key[3] for t in p.cancel_targets))
                for p in live.state.snapshot().progress.values())
        assert set(sent()) == wanted and len(sent()) == len(wanted)
        assert set(exact_reads) == {'entry-A','success-B','deferred-C'} and live.status()['native_inflight'] == 0
        assert len(sdk.posts) == (1 if anchor_origin == 'submitted' else 0)
        if anchor_origin == 'manual':
            assert live.state.snapshot().openings == {} and live.state.snapshot().own_fills == {}
            assert manual_trade_queries == []
    finally:
        for release in releases.values(): release.set()
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_deferred_manual_anchor_proof_is_not_gated_by_prior_dispatch(tmp_path, monkeypatch):
    from threading import Event, Condition
    from tests.test_lp_cancel_lock_regressions import PermissionPort
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch, submit=False)
    permission = PermissionPort()
    live.bind_mutation_admission(permission, permission.acquire)
    releases = {oid: Event() for oid in ('unrelated-X', 'unrelated-Y')}
    changed, exact_reads, terminal, trade_queries = Condition(), [], {}, []
    allow_final = [False]
    b_entered, c_entered = Event(), Event()
    trades = sdk.list_account_trades
    def account_trades(**kwargs):
        trade_queries.append(kwargs)
        return trades(**kwargs)
    sdk.list_account_trades = account_trades
    def cancel(*, order_ids):
        oid, = order_ids
        assert oid != 'entry-A', 'externally canceled anchor must never be dispatched'
        with changed:
            sdk.cancels.append(order_ids)
            changed.notify_all()
        if oid in releases:
            assert releases[oid].wait(5), 'unrelated native cancellation completion watchdog'
        elif oid == 'success-B':
            b_entered.set()
            assert c_entered.wait(5), 'original second native target entry watchdog'
        elif oid == 'deferred-C':
            assert b_entered.wait(5), 'original first native target entry watchdog'
            c_entered.set()
        with changed:
            original = next(o for o in sdk.orders if o.id == oid)
            terminal[oid] = original.model_copy(update={'status': 'CANCELED'})
            sdk.orders = tuple(o for o in sdk.orders if o.id != oid)
        return dict(canceled=order_ids, not_canceled={})
    def exact(*, order_id):
        exact_reads.append(order_id)
        assert order_id in {'entry-A', 'success-B', 'deferred-C', 'unrelated-X', 'unrelated-Y'}
        return terminal.get(order_id) if allow_final[0] else None
    sdk.cancel_orders, sdk.get_order = cancel, exact
    def sent(): return [oid for ids in sdk.cancels for oid in ids]
    try:
        sdk.orders = (_open_order('entry-A', 'BUY', price='.30', original='2000'),)
        live.run_once()
        live.run_protection_once()
        baseline = next(b for b in live.state.snapshot().baselines.values()
            if b.key.kind == 'first_seen_episode' and b.key.episode_id == 'entry-A')
        assert baseline.anchor_order_ids == ('entry-A',)
        sdk.orders += (_open_order('unrelated-X', 'BUY', price='.29', original='20'),
            _open_order('unrelated-Y', 'BUY', price='.28', original='20'))
        live.run_once()
        handles = {oid: live.cancel_independent((WALLET, CONDITION_ID, TOKEN_ID, oid)) for oid in releases}
        with changed: assert changed.wait_for(lambda: set(sent()) == set(releases), timeout=2)
        assert live.status()['native_inflight'] == 2 and all(not h.done() for h in handles.values())
        permission.admit = False
        sdk.orders += (_open_order('success-B', 'BUY', price='.30', original='600', matched='100'),
            _open_order('deferred-C', 'BUY', price='.30', original='100'))
        public.level, clock[0] = Decimal('4000'), 1
        live.run_once()
        assert live.run_protection_once() == ()
        snapshot = live.state.snapshot()
        progress = snapshot.progress[baseline.key]
        assert progress.state == 'triggered' and progress.front_estimate.value == Decimal('1400')
        assert progress.level_total.value == Decimal('4000') and progress.ratio.value == Decimal('.35')
        assert progress.cancel_targets == () and set(sent()) == set(releases)
        assert snapshot.openings == {} and snapshot.own_fills == {} and sdk.posts == []
        # The external API, not an own cancel request, supplies the terminal fact.
        original = next(o for o in sdk.orders if o.id == 'entry-A')
        terminal['entry-A'] = original.model_copy(update={'status': 'CANCELED', 'size_matched': Decimal('0')})
        sdk.orders = tuple(o for o in sdk.orders if o.id != 'entry-A')
        live.run_once()
        assert live.run_protection_once() == ()
        unknown = next(o for o in live.state.snapshot().orders if o.key[3] == 'entry-A')
        assert unknown.presence == 'absent' and unknown.terminal_proof is None
        assert live.state.snapshot().progress[baseline.key].cancel_targets == ()
        assert set(sent()) == set(releases) and live.status()['native_inflight'] == 2
        allow_final[0] = True
        clock[0] += 61
        live.run_once()
        before_proof = live.state.snapshot()
        assert live.run_protection_once() == ()
        assert exact_reads and set(exact_reads) == {'entry-A'}, 'accepted deferred anchor needs exact proof before any own target dispatch'
        proven = live.state.snapshot()
        anchor = next(o for o in proven.orders if o.key[3] == 'entry-A')
        assert anchor.terminal_proof.identity_verified and anchor.terminal_proof.matched.value == 0
        assert proven.trade_version > before_proof.trade_version and anchor not in proven.active_orders
        assert proven.progress[baseline.key].cancel_targets == ()
        assert live.run_protection_once() == ()  # receipt invalidated the old ordinary frame
        live.run_once()
        assert live.run_protection_once() == ()  # external admission still denies mutation
        assert live.state.snapshot().progress[baseline.key].cancel_targets == ()
        assert set(sent()) == set(releases) and all(not h.done() for h in handles.values())
        permission.admit = True
        attempts = live.run_protection_once()
        assert {h.identity.target.existing_order_id for h in attempts} == {'success-B', 'deferred-C'}
        for handle in attempts:
            assert handle.result(timeout=3).status.value == 'accepted'
            handle._thread.join(timeout=2)
            assert handle.done()
        assert all(not h.done() for h in handles.values()), 'pending cancels cannot consume placement capacity'
        assert live.status()['native_inflight'] == 2
        assert {t.order_key[3] for t in live.state.snapshot().progress[baseline.key].cancel_targets} == {'success-B', 'deferred-C'}
        for oid, handle in handles.items():
            releases[oid].set()
            assert handle.result(timeout=3).status.value == 'accepted'
            handle._thread.join(timeout=2)
            assert handle.done()
        assert live.status()['native_inflight'] == 0
        assert set(sent()) == {'unrelated-X', 'unrelated-Y', 'success-B', 'deferred-C'} and len(sent()) == 4
        sdk.orders += (_open_order('new-healthy-after-anchor', 'BUY', price='.30', original='100'),)
        for _ in range(2):
            clock[0] += 1
            live.run_once()
            assert live.run_protection_once() == ()
        assert set(sent()) == {'unrelated-X', 'unrelated-Y', 'success-B', 'deferred-C'} and len(sent()) == 4
        assert set(exact_reads) == {'entry-A', 'success-B', 'deferred-C', 'unrelated-X', 'unrelated-Y'}
        assert live.status()['native_inflight'] == 0
        assert live.state.snapshot().openings == {} and live.state.snapshot().own_fills == {}
        assert sdk.posts == [] and trade_queries == []
    finally:
        b_entered.set(); c_entered.set()
        for release in releases.values(): release.set()
        assert live.close(timeout=3)
        live.trading.close()


def test_current_settings_cas_preserves_control_and_unrelated_fields(tmp_path, monkeypatch):
    import hashlib, json
    from open_trader.polymarket_lp_accounting import default_account_pool_document
    account = hashlib.sha256(WALLET.strip().casefold().encode()).hexdigest()
    store = PredictionArbitrageStore(tmp_path)
    document = default_account_pool_document(store.path, account, datetime.now(UTC))
    document.update(budget_usd='40',target_buy_count=2,buy_price_level=1,config_version=7,
        trading_config_version=5,legacy_keep=dict(intent='unrelated',amount='999'),round_interval_seconds=30)
    with store._transaction() as c: c.execute('INSERT INTO lp_auto_pool(singleton,payload) VALUES(1,?)',(json.dumps(document),))
    loaded = store.lp_load_current_settings(account)
    assert (loaded['budget_usd'],loaded['target_buy_count'],loaded['buy_price_level'],loaded['config_version'])==('40',2,1,7)
    assert (loaded['round_interval_seconds'],loaded['api_retry_interval_seconds'],loaded['order_check_interval_seconds'])==(30,60,10)
    with pytest.raises(TypeError): loaded['budget_usd']='999'
    # Actual separate narrow control commit between settings read and CAS.
    store.lp_save_current_control(dict(account_id=account,process_epoch='settings-seed',request_version=1,
        request_id='control-between',desired_running=True,requested_at=datetime.now(UTC),actor=None))
    current_control = dict(store.lp_load_current_control(account))
    saved = store.lp_save_current_settings(account,expected_config_version=7,
        changes=dict(budget_usd='50',target_buy_count=3,buy_price_level=2),actor='test')
    assert (saved['budget_usd'],saved['target_buy_count'],saved['buy_price_level'],saved['config_version'],saved['trading_config_version'])==('50',3,2,8,6)
    assert dict(store.lp_load_current_control(account))==current_control
    with store._read_connection() as c: actual=json.loads(c.execute('SELECT payload FROM lp_auto_pool').fetchone()[0])
    assert actual['legacy_keep']==document['legacy_keep'] and actual['intents']==document['intents']
    timing=store.lp_save_current_settings(account,expected_config_version=8,
        changes=dict(api_retry_interval_seconds=61,order_check_interval_seconds=11))
    assert (timing['config_version'],timing['trading_config_version'])==(9,6)
    assert loaded['config_version']==7 and saved['config_version']==8
    for expected,changes in [(7,dict(budget_usd='1')), (True,dict(api_retry_interval_seconds=2)),
            (9,dict(budget_usd=True)),(9,dict(budget_usd='NaN')),(9,dict(budget_usd='Infinity')),
            (9,dict(target_buy_count=True)),(9,dict(buy_price_level=True)),(9,dict(buy_price_level=3)),
            (9,dict(api_retry_interval_seconds=0)),(9,dict(order_check_interval_seconds=True)),
            (9,dict(round_interval_seconds=31)),(9,dict(desired_running=False))]:
        with pytest.raises(ValueError): store.lp_save_current_settings(account,expected_config_version=expected,changes=changes)
        assert store.lp_load_current_settings(account)==timing
    for foreign in ('foreign',WALLET,'0'*64):
        with pytest.raises(ValueError):store.lp_load_current_settings(foreign)
        with pytest.raises(ValueError):store.lp_save_current_settings(foreign,expected_config_version=9,changes=dict(budget_usd='1'))
    initial=PredictionArbitrageStore(tmp_path/'initial')
    defaults=initial.lp_load_current_settings(account)
    assert defaults['budget_usd'] is None and defaults['config_version']==0 and defaults['trading_config_version']==0
    assert (defaults['round_interval_seconds'],defaults['api_retry_interval_seconds'],defaults['order_check_interval_seconds'])==(30,60,10)
    with store._transaction() as c:
        bad={**actual,'budget_usd':False}
        c.execute('UPDATE lp_auto_pool SET payload=?',(json.dumps(bad),))
    with pytest.raises(ValueError): store.lp_load_current_settings(account)
    def failed_read(): raise OSError('actual SQLite read failure')
    monkeypatch.setattr(initial,'_read_connection',failed_read)
    with pytest.raises(OSError): initial.lp_load_current_settings(account)


def test_settings_publish_after_commit_without_blocking_current_facts(tmp_path, monkeypatch):
    import hashlib, sqlite3, threading
    from threading import Event, Thread
    from tests.test_lp_d2_current_inputs import _qualified_service
    from tests.test_lp_order_registration_contract import _open_order, CONDITION_ID, TOKEN_ID
    initial,_,_,_ = _d2_protection_runtime(tmp_path/'initial',monkeypatch,submit=False)
    try:
        assert initial.load_settings().budget is None
        initial.request_running(False);settle_control(initial);initial.run_once()
        assert initial.state.snapshot().new_capacity.state=='unknown'
        assert initial.state.snapshot().current_exposure.state=='known'
        initial.configure(dict(budget_usd='40',target_buy_count=2))
        deadline=time.monotonic()+3
        while initial.settings_status()['pending'] and time.monotonic()<deadline:time.sleep(.001)
        assert initial.settings_status()['effective'].budget==Decimal('40')
    finally:
        assert initial.close(timeout=3)
        initial.trading.close()
    live,sdk,public,clock = _d2_protection_runtime(tmp_path,monkeypatch,submit=False)
    account=hashlib.sha256(WALLET.strip().casefold().encode()).hexdigest()
    live.store.lp_save_current_settings(account,expected_config_version=0,
        changes=dict(budget_usd='40',target_buy_count=2,buy_price_level=1))
    entered,release,read_entered,read_release=Event(),Event(),Event(),Event()
    mode=['hold']; commits=[];control_entered=Event()
    real_connect=sqlite3.connect
    class SettingsConnection(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if sql.startswith('BEGIN') and threading.current_thread().name=='lp-control-save':control_entered.set()
            if sql=='COMMIT' and threading.current_thread().name=='lp-settings-save':
                commits.append(mode[0]);entered.set()
                if mode[0]=='hold':assert release.wait(5),'actual settings commit watchdog'
                if mode[0]=='fail':raise sqlite3.OperationalError('known settings commit failure')
            return super().execute(sql,*args,**kwargs)
    def connect(*args,**kwargs):return real_connect(*args,**(kwargs|dict(factory=SettingsConnection)))
    def wait_settings(predicate):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            status=live.settings_status()
            if predicate(status):return status
            time.sleep(.001)
        raise AssertionError('actual settings completion watchdog')
    try:
        old=live.load_settings()
        assert (old.budget,old.target_count,old.buy_price_level,old.config_version)==(Decimal('40'),2,1,1)
        live.request_running(False);settle_control(live);live.run_once()
        cycle=live.state.snapshot().current.cycle_generation
        monkeypatch.setattr(sqlite3,'connect',connect)
        started=live.configure(dict(budget_usd='50',target_buy_count=3,buy_price_level=2,expected_config_version=1))
        assert entered.wait(2) and started['pending']
        assert live.policy is old and live.settings_status()['native_inflight']
        assert live.request_running(True).status=='rejected'
        with pytest.raises(ValueError):live.configure(dict(api_retry_interval_seconds=62))
        before=live.state.snapshot().snapshot_version
        assert live.run_once().acceptance.observation is not None
        assert live.state.snapshot().snapshot_version>before
        assert live.run_protection_once()==() and live.policy is old
        # Actual settings completion while a real Reader component is pending.
        balance=sdk.get_balance_allowance
        def held_balance(**kwargs):
            read_entered.set();assert read_release.wait(5),'current account read watchdog'
            return balance(**kwargs)
        sdk.get_balance_allowance=held_balance
        now=live.state.snapshot().observation.accepted_at
        producer,_=_qualified_service(tmp_path/'pool',now,condition=CONDITION_ID,token=TOKEN_ID)
        pool=producer.capture_qualified_pool()
        results=[]
        read_thread=Thread(target=lambda:results.append(live.run_once(pool)),name='settings-current-frame')
        read_thread.start();assert read_entered.wait(2)
        release.set()
        committed=wait_settings(lambda s:not s['native_inflight'])
        assert committed['committed'].config_version==2 and committed['effective'] is old and committed['pending']
        assert live.policy is old and live.request_running(True).status=='rejected'
        read_release.set();read_thread.join(timeout=3);assert not read_thread.is_alive() and len(results)==1
        observation=results[0].acceptance.observation
        assert observation.calculation.new_capacity==Decimal('40') and observation.calculation.policy_version==1
        current=wait_settings(lambda s:not s['pending'])['effective']
        assert (current.budget,current.target_count,current.buy_price_level,current.config_version,current.trading_config_version)==(Decimal('50'),3,2,2,2)
        assert old.budget==Decimal('40') and live.state.snapshot().current.cycle_generation==cycle
        sdk.get_balance_allowance=balance
        fresh=live.run_once(pool).acceptance.observation
        assert fresh.calculation.new_capacity==Decimal('50') and fresh.calculation.policy_version==2
        assert commits==['hold']
        # An actual SQLite failure is visible, leaves old policy, and is never retried.
        mode[0]='fail';entered.clear()
        live.configure(dict(budget_usd='60',target_buy_count=4,buy_price_level=1,expected_config_version=2))
        failed=wait_settings(lambda s:not s['pending'])
        assert failed['error']=='OperationalError' and failed['effective'] is current
        assert live.store.lp_load_current_settings(account)['config_version']==2
        for _ in range(2):live.poll();live.settings_status()
        assert commits==['hold','fail']
        # A current BUY forbids trading settings; timing-only retains running semantics.
        sdk.orders=(_open_order('manual-settings-guard','BUY',price='.30',original='20'),)
        live.run_once()
        with pytest.raises(ValueError):live.configure(dict(budget_usd='60',target_buy_count=4))
        sdk.orders=();live.run_once()
        mode[0]='pass'
        assert live.request_running(True).status=='accepted';settle_control(live)
        live.run_once()
        sent=live.run_once(pool)
        assert [o.status.value for o in sent.outcomes]==['accepted']
        terms=sent.outcomes[0].identity.target.terms
        assert (terms.price,terms.quantity,terms.order_type,terms.post_only)==(Decimal('.39'),Decimal('20'),'GTD',True)
        assert sdk.posts[-1]['price']==Decimal('.39') and sdk.posts[-1]['size']==Decimal('20')
        assert sent.acceptance.observation.calculation.policy_version==2
        resumed=live.state.snapshot().current.cycle_generation
        version=live.control.status().requested_version
        live.configure(dict(api_retry_interval_seconds=65,order_check_interval_seconds=12,expected_config_version=2))
        timing=wait_settings(lambda s:not s['pending'])['effective']
        assert (timing.config_version,timing.trading_config_version,timing.api_retry_interval_seconds,timing.order_check_interval_seconds)==(3,2,65,12)
        assert live.state.snapshot().current.cycle_generation==resumed and live.control.status().requested_version==version
        assert live.settings_status()['effective_round_interval_seconds'] is None
        with pytest.raises(ValueError):live.configure(dict(round_interval_seconds=30))
        # Bounded close retains the actual native settings writer until it ends.
        mode[0]='hold';release.clear();entered.clear()
        live.configure(dict(api_retry_interval_seconds=66,expected_config_version=3))
        assert entered.wait(2)
        control_entered.clear()
        from contextlib import contextmanager
        from types import SimpleNamespace
        from tests.test_lp_d3_persistence import wait_for
        owner=live.store._lp_write_coordinator
        acquire=owner.acquire
        control_queued=(Event(),Event())
        control_admissions=[]
        @contextmanager
        def observe_control_admission(priority='background',**kwargs):
            if priority=='control' and threading.current_thread().name=='lp-control-save':
                admission=kwargs['cancellation']
                observed=False
                def is_cancelled():
                    nonlocal observed
                    if not observed:
                        observed=True
                        control_admissions.append(admission)
                        control_queued[len(control_admissions)-1].set()
                    return admission.is_set()
                # acquire checks cancellation only after appending this ticket.
                # Delegate every decision to the real attempt's admission.
                kwargs=kwargs|dict(cancellation=SimpleNamespace(is_set=is_cancelled))
            with acquire(priority,**kwargs) as permit:
                yield permit
        monkeypatch.setattr(owner,'acquire',observe_control_admission)
        assert live.request_running(False).status=='accepted'
        # Settings retains its real native COMMIT hold and the sole permit.
        # Match the exact control attempt; business may legitimately queue too.
        assert control_queued[0].wait(5), 'first control admission watchdog'
        assert owner.status()['holder'] and live.settings_status()['native_inflight']
        assert live.control.status().attempt_number==1
        assert not control_entered.is_set()
        clock[0]+=11
        # A queued control is known-not-written, so one cancellation is safe.
        # Exhaust its actual second attempt while settings still holds COMMIT;
        # the late settings result must preserve this peer fatal and shutdown.
        cancelled=wait_for(live.poll, lambda s:s.save_state=='retry_wait')
        assert cancelled.fatal is None and cancelled.error_code=='AdmissionCancelled'
        assert len(control_admissions)==1 and control_admissions[0].is_set()
        clock[0]=cancelled.retry_at_monotonic
        live.poll()
        assert control_queued[1].wait(5), 'second control admission watchdog'
        assert control_admissions[1] is not control_admissions[0]
        assert owner.status()['holder'] and live.settings_status()['native_inflight']
        assert live.control.status().attempt_number==2 and not control_entered.is_set()
        clock[0]+=11
        fatal=wait_for(live.poll, lambda s:s.fatal is not None).fatal
        assert fatal.origin=='save_retry_exhausted' and fatal.outcome=='failed'
        assert len(control_admissions)==2 and all(a.is_set() for a in control_admissions)
        assert live.control.status().error_code=='AdmissionCancelled' and not control_entered.is_set()
        assert live.state.snapshot().current.shutdown_requested
        assert live.close(timeout=.01) is False and live.settings_status()['native_inflight']
        release.set();wait_settings(lambda s:not s['pending'])
        assert live.close(timeout=3) and live.settings_status()['effective'].config_version==4
        assert live.state.snapshot().current.effective_running is False
        assert live.control.status().fatal==fatal  # late settings commit never clears peer fatal or resumes
    finally:
        release.set();read_release.set()
        assert live.close(timeout=3)
        live.trading.close()


def test_captured_production_candidates_seal_real_gtd_terms_without_legacy_gates(tmp_path, monkeypatch):
    import hashlib, json, sqlite3, threading
    from open_trader import polymarket_lp_market_reader, polymarket_trading
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_d2_current_inputs import _qualified_service
    from tests.test_lp_order_registration_contract import _SDKPublicClient, CONDITION_ID, TOKEN_ID
    now=datetime(2026,10,16,1,tzinfo=UTC)
    class FixedClockMeta(type):
        def __instancecheck__(cls,value):return isinstance(value,datetime)
    class FixedDateTime(datetime,metaclass=FixedClockMeta):
        @classmethod
        def now(cls,tz=None):return now if tz is not None else now.replace(tzinfo=None)
    monkeypatch.setattr(polymarket_lp_market_reader,'datetime',FixedDateTime)
    monkeypatch.setattr(polymarket_trading,'datetime',FixedDateTime)
    producer,_=_qualified_service(tmp_path/'producer',now,condition=CONDITION_ID,token=TOKEN_ID)
    capture=producer.capture_qualified_pool()
    public=_SDKPublicClient(now)
    adapter,sdk=_adapter(tmp_path/'runtime',public_client=public)
    sdk.orders=();sdk.positions=()
    creates=[]
    def create(**kwargs):
        creates.append(kwargs)
        return dict(kwargs,order_type='GTD')
    def post(signed):
        sdk.posts.append(signed)
        return AcceptedOrder(order_id='real-captured-buy',status='live',making_amount=Decimal('0'),
            taking_amount=Decimal('0'),trade_ids=(),transactions_hashes=())
    sdk.create_limit_order,sdk.post_order=create,post
    store=PredictionArbitrageStore(tmp_path/'runtime')
    account=hashlib.sha256(WALLET.strip().casefold().encode()).hexdigest()
    store.lp_save_current_settings(account,expected_config_version=0,
        changes=dict(budget_usd='40',target_buy_count=1,buy_price_level=1))
    seed_running(store)
    live=LPD2LiveExecution(store,adapter,account_id=WALLET,venue='polymarket',process_epoch='captured-policy',
        policy=CalculationPolicy(None),utc_now=lambda:now)
    def prohibited(*args,**kwargs):pytest.fail('legacy permission or extra candidate acquisition attempted')
    for name in ('lp_active_sessions','lp_sessions','lp_register_account_snapshot'):
        if hasattr(store,name):monkeypatch.setattr(store,name,prohibited)
    public.get_market=public.get_order_book=prohibited
    real_connect=sqlite3.connect
    class BrokenBusinessLog(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if threading.current_thread().name=='lp-business-writer' and 'lp_sessions' in sql:
                raise sqlite3.OperationalError('business logging unavailable')
            return super().execute(sql,*args,**kwargs)
    def connect(*args,**kwargs):return real_connect(*args,**(kwargs|dict(factory=BrokenBusinessLog)))
    monkeypatch.setattr(sqlite3,'connect',connect)
    frames=[];read=live.reader.read_current
    def current(*args,**kwargs):
        frame=read(*args,**kwargs);frames.append(frame);return frame
    live.reader.read_current=current
    try:
        policy=live.load_settings();assert policy.config_version==1
        live.initialize(ValidatedStartAdmission('captured-policy','offline-permit'))
        first=live.run_once(capture)
        assert first.outcomes==() and sdk.posts==[] and len(frames)==1
        result=live.run_once(capture)
        assert len(frames)==2 and result.acceptance.observation.source_frame is frames[-1]
        assert [o.status.value for o in result.outcomes]==['accepted']
        outcome,=result.outcomes;terms=outcome.identity.target.terms
        assert outcome.identity.account_id==WALLET and outcome.identity.target.venue=='polymarket'
        assert outcome.identity.target.token_id==TOKEN_ID
        assert (terms.price,terms.quantity,terms.order_type,terms.post_only,terms.send_options)==(Decimal('.40'),Decimal('20'),'GTD',True,())
        assert terms.expiration==1792195260
        assert datetime.fromtimestamp(terms.expiration-60,UTC)==datetime(2026,10,17,0,tzinfo=UTC)
        assert creates==[dict(token_id=TOKEN_ID,price=Decimal('.40'),size=Decimal('20'),side='BUY',post_only=True,expiration=1792195260)]
        assert sdk.posts==[dict(creates[0],order_type='GTD')]
        snapshot=live.state.snapshot();order,=snapshot.orders
        assert (order.key,order.order_type.value,order.expiration.value,order.original_size.value,order.size_matched.value)==((WALLET,CONDITION_ID,TOKEN_ID,'real-captured-buy'),'GTD',1792195260,Decimal('20'),Decimal('0'))
        baseline=next(b for b in snapshot.baselines.values() if b.key.kind=='queue_bucket')
        assert baseline.source.source_id==capture.rows[CONDITION_ID].source_id
        assert baseline.source.checked_at==capture.rows[CONDITION_ID].checked_at
        assert baseline.source.checked_at!=capture.captured_at
        assert result.acceptance.observation.calculation.policy_version==policy.config_version
        # Current API UNKNOWN blocks new BUY authority even with a healthy pool.
        real_balance=sdk.get_balance_allowance
        sdk.get_balance_allowance=lambda **kwargs:(_ for _ in ()).throw(TimeoutError('current API unavailable'))
        before=len(sdk.posts)
        assert live.run_once(capture).outcomes==() and len(sdk.posts)==before
        assert live.state.snapshot().new_capacity.state=='unknown'
        # The existing SDK three-minute GTD minimum rejects a near review,
        # while current facts still reach the state owner.
        now=datetime(2026,10,16,23,59,tzinfo=UTC)
        near_producer,_=_qualified_service(tmp_path/'near-review',now,condition=CONDITION_ID,token=TOKEN_ID)
        near_pool=near_producer.capture_qualified_pool()
        sdk.get_balance_allowance=real_balance
        near=LPD2LiveExecution(store,adapter,account_id=WALLET,venue='polymarket',
            process_epoch='near-review',policy=CalculationPolicy(None),utc_now=lambda:now)
        try:
            near.load_settings();near.initialize(ValidatedStartAdmission('near-review','offline-permit'))
            near_result=near.run_once(near_pool)
            assert near_result.acceptance.observation is not None
            assert near_result.acceptance.observation.calculation.target_tokens==()
            assert near_result.outcomes==() and len(sdk.posts)==before
        finally:
            assert near.close(timeout=3)
        # A malformed saved policy surfaces and cannot authorize a new process BUY.
        with store._transaction() as c:
            d=json.loads(c.execute('SELECT payload FROM lp_auto_pool').fetchone()[0]);d['budget_usd']=False
            c.execute('UPDATE lp_auto_pool SET payload=?',(json.dumps(d),))
        sdk.get_balance_allowance=real_balance
        now=datetime(2026,10,17,1,tzinfo=UTC)
        invalid_producer,_=_qualified_service(tmp_path/'invalid-policy-pool',now,condition=CONDITION_ID,token=TOKEN_ID)
        invalid_pool=invalid_producer.capture_qualified_pool()
        invalid=LPD2LiveExecution(store,adapter,account_id=WALLET,venue='polymarket',
            process_epoch='invalid-policy',policy=CalculationPolicy(Decimal('40'),1),utc_now=lambda:now)
        try:
            with pytest.raises(ValueError):invalid.load_settings()
            invalid.initialize(ValidatedStartAdmission('invalid-policy','offline-permit'))
            for _ in range(2):
                assert invalid.run_once(invalid_pool).outcomes==() and len(sdk.posts)==before
        finally:
            assert invalid.close(timeout=3)
    finally:
        assert live.close(timeout=3)
        adapter.close()


def test_sdk_and_plain_cached_accounts_have_identical_end_to_end_targets(tmp_path, monkeypatch):
    import hashlib
    from collections.abc import Mapping
    from dataclasses import replace
    from types import MappingProxyType

    from open_trader import polymarket_lp_market_reader, polymarket_trading
    from polymarket.models.clob.account import ClobTrade
    from polymarket.models.clob.order_response import AcceptedOrder
    from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_calculation import calculate_current
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
    from tests.test_lp_d2_current_inputs import _qualified_service
    from tests.test_polymarket_lp import _SDKAccountClient
    from tests.test_lp_order_registration_contract import _SDKPublicClient, CONDITION_ID, TOKEN_ID

    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    class FixedClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class FixedDateTime(datetime, metaclass=FixedClockMeta):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(polymarket_lp_market_reader, 'datetime', FixedDateTime)
    monkeypatch.setattr(polymarket_trading, 'datetime', FixedDateTime)
    cache_store = PredictionArbitrageStore(tmp_path / 'cache')
    cache_store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    account_client = _SDKAccountClient(now)
    account_client.list_open_orders = lambda **_: []
    account_client.list_positions = lambda **_: []
    trade_reads = [0]
    original_trade_reader = account_client.list_account_trades

    def counted_trade_reader(*args, **kwargs):
        trade_reads[0] += 1
        return original_trade_reader(*args, **kwargs)

    account_client.list_account_trades = counted_trade_reader
    cache_adapter = PolymarketTradingClient(
        TradingConfig('0x' + '1' * 40, '0x' + '2' * 40), account_client
    )

    class ControlledTime:
        def __init__(self, delegate):
            self._delegate = delegate

        def monotonic(self):
            return 1000.0

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    try:
        with monkeypatch.context() as controlled:
            controlled.setattr(polymarket_trading, 'time', ControlledTime(polymarket_trading.time))
            cache_adapter.lp_account_snapshot_shared(
                trade_generation_provider=cache_store.lp_trade_generation
            )
            cached_account = cache_adapter.lp_account_snapshot_shared()
        assert trade_reads[0] == 1
        account_model = cached_account['raw_trades'][0]
    finally:
        cache_adapter.close()
    assert isinstance(account_model, ClobTrade)
    assert cached_account['balance'] > 0
    assert cached_account['allowance'] > 0
    assert cached_account['balance_complete'] is True
    assert cached_account['open_orders'] == ()
    assert cached_account['positions'] == ()
    sdk_account = dict(cached_account)
    plain_account = dict(cached_account)
    plain_account['raw_trades'] = (
        account_model.model_dump(mode='python'),
    )

    class FactsTripwire(Mapping):
        allowed = frozenset({'directions', 'checked_at', 'global_recovery_generation'})

        def __init__(self, values):
            self._values = dict(values)

        def _key(self, key):
            if key not in self.allowed:
                raise AssertionError(f'removed facts key accessed: {key}')
            return key

        def __getitem__(self, key):
            return self._values[self._key(key)]

        def __iter__(self):
            return iter(self._values)

        def __len__(self):
            return len(self._values)

        def get(self, key, default=None):
            key = self._key(key)
            return self._values.get(key, default)

        def __contains__(self, key):
            return self._key(key) in self._values

    def run_representation(root, account):
        producer, _ = _qualified_service(root / 'producer', now,
                                          condition=CONDITION_ID, token=TOKEN_ID)
        producer._candidate_qualification_facts[CONDITION_ID]['account'] = account
        capture = producer.capture_qualified_pool()
        source = capture.rows[CONDITION_ID]
        facts = FactsTripwire(source.value['facts'])
        tripwire_value = MappingProxyType({'row': source.value['row'], 'facts': facts})
        tripwire_source = replace(source, value=tripwire_value)
        capture = replace(capture, rows=MappingProxyType({CONDITION_ID: tripwire_source}))

        public = _SDKPublicClient(now)
        adapter, sdk = _adapter(root / 'runtime', public_client=public)
        sdk.orders = ()
        sdk.positions = ()
        creates = []
        sdk.create_limit_order = lambda **kwargs: (creates.append(dict(kwargs)) or
                                                    dict(kwargs, order_type='GTD'))

        def post(signed):
            sdk.posts.append(signed)
            return AcceptedOrder(order_id='equivalence-buy', status='live',
                making_amount=Decimal('0'), taking_amount=Decimal('0'),
                trade_ids=(), transactions_hashes=())

        sdk.post_order = post
        store = PredictionArbitrageStore(root / 'runtime')
        account_id = hashlib.sha256(WALLET.strip().casefold().encode()).hexdigest()
        store.lp_save_current_settings(account_id, expected_config_version=0,
            changes=dict(budget_usd='40', target_buy_count=1, buy_price_level=1))
        seed_running(store)
        live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
            process_epoch='equivalence', policy=CalculationPolicy(None), utc_now=lambda: now)
        try:
            live.load_settings()
            live.initialize(ValidatedStartAdmission('equivalence', 'offline-permit'))
            first = live.run_once(capture)
            assert first.outcomes == ()
            result = live.run_once(capture)
            assert [outcome.status.value for outcome in result.outcomes] == ['accepted']
            assert creates == [dict(token_id=TOKEN_ID, price=Decimal('.40'), size=Decimal('20'),
                                    side='BUY', post_only=True, expiration=1792195260)]
            assert sdk.posts == [dict(creates[0], order_type='GTD')]
            observation = result.acceptance.observation
            assert observation.source_frame.orders == ()
            assert observation.source_frame.positions == ()
            assert observation.source_frame.sources['balance'].value == (
                Decimal('100'), Decimal('100'))
            public_policy = CalculationPolicy(Decimal('40'), 1)
            prepared_again = prepare_current_inputs(
                observation.source_frame, capture, public_policy, now)
            candidate_rows = prepared_again.sources['candidates'].value
            assert tuple(row['token_id'] for row in candidate_rows) == (TOKEN_ID,)
            eligible = prepared_again.sources['candidate_eligibility'].value
            assert eligible[f'{CONDITION_ID}:{TOKEN_ID}']['state'] == 'eligible'
            recalculated = calculate_current(prepared_again, public_policy, now)
            assert (recalculated.buy_reserved, recalculated.inventory_cost,
                    recalculated.new_capacity) == (
                        Decimal('0'), Decimal('0'), Decimal('40'))
            assert recalculated.target_tokens == (TOKEN_ID,)
            assert observation.calculation.target_tokens == (TOKEN_ID,)
            assert (observation.calculation.buy_reserved,
                    observation.calculation.inventory_cost,
                    observation.calculation.new_capacity) == (
                        Decimal('0'), Decimal('0'), Decimal('40'))
            terms = result.outcomes[0].identity.target.terms
            assert (terms.price, terms.quantity, terms.order_type, terms.post_only,
                    terms.expiration) == (Decimal('.40'), Decimal('20'), 'GTD', True, 1792195260)
            baseline = next(b for b in live.state.snapshot().baselines.values()
                            if b.key.kind == 'queue_bucket')
            assert baseline.source.source_id == source.source_id
            assert baseline.source.checked_at == source.checked_at
            before_posts = len(sdk.posts)
            sdk.get_balance_allowance = lambda **kwargs: (_ for _ in ()).throw(
                TimeoutError('current funds unavailable'))
            unknown_funds = live.run_once(capture)
            assert unknown_funds.outcomes == () and len(sdk.posts) == before_posts
            assert live.state.snapshot().new_capacity.state == 'unknown'
            return (terms, observation.calculation.target_tokens, sdk.posts, baseline.source)
        finally:
            assert live.close(timeout=3)
            adapter.close()

    plain = run_representation(tmp_path / 'plain', plain_account)
    sdk = run_representation(tmp_path / 'sdk', sdk_account)
    assert (plain[0].price, plain[0].quantity, plain[0].expiration) == (
        sdk[0].price, sdk[0].quantity, sdk[0].expiration)
    assert plain[1:] == sdk[1:]


@pytest.mark.parametrize('yield_mode', ['known_current_yield', 'unknown_current_yield'])
def test_persistently_invalid_incumbent_qualification_preserves_existing_protection(
    tmp_path, monkeypatch, yield_mode
):
    from copy import deepcopy
    from datetime import timedelta
    from tests.test_polymarket_lp import _SDKAccountClient
    from tests.test_lp_d2_current_inputs import _qualified_service
    from tests.test_lp_order_registration_contract import CONDITION_ID, TOKEN_ID

    live, sdk, public, clock = _d2_protection_runtime(
        tmp_path / 'runtime', monkeypatch, quantity='10', submit=True,
        budget='7', bid='.40', ask='.42'
    )
    # The independent holding lane must receive its own real venue identity;
    # invalid candidate capture still grants no new BUY/session authority.
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_order_registration_contract import _open_order
    initial_post = sdk.post_order
    def post(signed):
        if signed['side'] != 'SELL':
            return initial_post(signed)
        sdk.posts.append(signed)
        sdk.orders += (_open_order('holding-independent', 'SELL', price=str(signed['price']), original=str(signed['size'])),)
        return AcceptedOrder(order_id='holding-independent', status='live', making_amount=Decimal(0),
            taking_amount=Decimal(0), trade_ids=(), transactions_hashes=())
    sdk.post_order = post
    origin = public.now
    producer, direction = _qualified_service(
        tmp_path / 'capture', origin, condition=CONDITION_ID, token=TOKEN_ID
    )
    producer.clock = lambda: origin + timedelta(seconds=clock[0])
    sdk_trade = _SDKAccountClient(origin).trade
    sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID,
                          size='10', average_price='.30'),)
    if yield_mode == 'unknown_current_yield':
        public.reward_unknown = True

        def uncertain_cancel(*, order_ids):
            sdk.cancels.append(order_ids)
            raise TimeoutError('offline unknown cancellation')

        sdk.cancel_orders = uncertain_cancel
    else:
        public.reward = Decimal('0')
    try:
        order_ids = tuple(order.id for order in sdk.orders)
        assert order_ids == ('entry-A',)
        baseline_before = next(iter(live.state.snapshot().baselines.values()))
        template = deepcopy(direction)
        previous_observation_id = None
        previous_read_sequence = None
        cancel_requests, all_requests = set(), set()
        for second in (0, 599, 600, 601, 1200):
            clock[0] = second
            current = origin + timedelta(seconds=second)
            fresh = deepcopy(template)
            fresh['market']['metadata_checked_at'] = current
            fresh['market']['fees_checked_at'] = current
            fresh['book']['received_at'] = current
            fresh['book']['timestamp'] = current
            fresh['reward_checked_at'] = current
            fresh['history_summary']['checked_at'] = current
            fresh['book']['sdk_extra'] = sdk_trade
            assert producer._candidate_pool_record_success(
                CONDITION_ID,
                dict(condition_id=CONDITION_ID, state='eligible',
                     selected_direction=dict(token_id=TOKEN_ID)),
                judged_at=current,
                facts=dict(checked_at=current, directions=[fresh],
                           account=dict(raw_trades=(fresh['book']['sdk_extra'],)),
                           reservations=(dict(amount='999999'),)),
                global_recovery_generation=7)
            before_pool = deepcopy(producer._candidate_pool)
            before_rotation = deepcopy(producer._candidate_rotation)
            invalid_capture = producer.capture_qualified_pool()
            assert invalid_capture.rows[CONDITION_ID].error == 'qualification_value_invalid'
            assert invalid_capture.rows[CONDITION_ID].value is None
            assert invalid_capture.captured_at == current
            assert invalid_capture.rows[CONDITION_ID].checked_at == current
            assert producer._candidate_pool == before_pool
            assert producer._candidate_rotation == before_rotation
            result = live.run_once(invalid_capture)
            attempts = live.run_protection_once()
            for attempt in attempts:
                attempt.result(timeout=3)
                attempt._thread.join(3)
                assert attempt.done() and attempt.identity.request_id not in all_requests
                all_requests.add(attempt.identity.request_id)
                if getattr(attempt.identity, 'kind', None) != 'holding_sell':
                    cancel_requests.add(attempt.identity.request_id)
            if any(getattr(attempt.identity, 'kind', None) == 'holding_sell' for attempt in attempts):
                # An actual independent holding receipt invalidates financial
                # authority. Only a fresh complete account read restores it.
                assert live.state.snapshot().new_capacity.state == 'unknown'
                assert live.run_once(invalid_capture).outcomes == ()
            assert live.run_protection_once()==()  # no new request from the consumed raw source
            assert result.outcomes == ()
            observation = result.acceptance.observation
            assert observation is not None
            assert observation.cycle_generation == live.state.snapshot().current.cycle_generation
            assert observation.observation_id != previous_observation_id
            assert (previous_read_sequence is None
                    or observation.read_ticket.read_sequence > previous_read_sequence)
            previous_observation_id = observation.observation_id
            previous_read_sequence = observation.read_ticket.read_sequence
            for source_name in ('balance', 'open_orders', 'positions',
                                f'market:{CONDITION_ID}:{TOKEN_ID}'):
                source = observation.source_frame.sources[source_name]
                assert source.checked_at == current
            assert {order.id for order in sdk.orders} == {'entry-A', 'holding-independent'}
            assert [o for o in sdk.posts if o['side'] == 'BUY'] == [dict(token_id=TOKEN_ID, price=Decimal('.40'),
                                      size=Decimal('10'), side='BUY', post_only=True,
                                      expiration=None, order_type='GTC')]
            holding, = [o for o in sdk.posts if o['side'] == 'SELL']
            assert holding['price'] == Decimal('.42') and holding['size'] == 10 and holding['post_only'] is True
            assert sdk.market_orders == []
            assert sdk.positions[0]['size'] == '10'
            snapshot = live.state.snapshot()
            assert snapshot.positions and snapshot.positions[0].quantity.value == Decimal('10')
            assert snapshot.buy_reserved.value == Decimal('4')
            assert snapshot.inventory_cost.value == Decimal('3')
            assert snapshot.new_capacity.value == Decimal('3')
            baseline = next(iter(snapshot.baselines.values()))
            assert baseline is baseline_before
            entry_order = next(o for o in snapshot.orders if o.side == 'BUY')
            assert entry_order.key[3] == 'entry-A'
            assert entry_order.price.value == Decimal('.40')
            assert entry_order.remaining.value == Decimal('10')
            yield_key = (WALLET, CONDITION_ID, TOKEN_ID, 'entry-A')
            assert yield_key in snapshot.yields
            yield_state = snapshot.yields[yield_key]
            assert all(failure.data_failures == 0
                       for failure in snapshot.failure_states.values())
            assert {order.key[3] for order in snapshot.orders if order.side == 'SELL'} == {'holding-independent'}
            assert len(snapshot.sells) == 1
            sell_state = next(iter(snapshot.sells.values()))
            assert {o.key[3] for o in sell_state.current_sells} <= {'holding-independent'}
            assert sell_state.passive is None and sell_state.protected is None
            if yield_mode == 'known_current_yield':
                assert yield_state.yield_pct_per_hour.state == 'known'
                assert yield_state.yield_pct_per_hour.value == Decimal('0')
                assert yield_state.unknown_since_monotonic is None
                assert sdk.cancels == []
            elif second < 600:
                assert yield_state.unknown_since_monotonic == 0
                assert yield_state.unknown_deadline_monotonic == 600
                assert sdk.cancels == []
            else:
                assert yield_state.unknown_since_monotonic == 0
                assert yield_state.unknown_deadline_monotonic == 600
                expected = {600:1,601:2,1200:3}[second]
                assert sdk.cancels == [('entry-A',)]*expected and len(cancel_requests)==expected
                timeout_reasons = tuple(
                    target.reason for progress in snapshot.progress.values()
                    for target in progress.cancel_targets
                )
                assert 'buy_yield_unknown_timeout' in timeout_reasons
        if yield_mode == 'unknown_current_yield':
            final_yield = live.state.snapshot().yields[(WALLET, CONDITION_ID, TOKEN_ID, 'entry-A')]
            assert final_yield.unknown_since_monotonic == 0
            assert final_yield.unknown_deadline_monotonic == 600
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('proof_case', ['final0_complete_absent', 'final0_explicit_zero',
    'unknown_final', 'native_pending', 'positive_residual', 'incomplete_positions'])
def test_trading_settings_accept_conclusive_zero_fill_without_discarding_obligations(tmp_path, monkeypatch, proof_case):
    import hashlib, sqlite3, threading
    from threading import Event
    from tests.test_lp_order_registration_contract import CONDITION_ID, TOKEN_ID
    live,sdk,public,clock = _d2_protection_runtime(tmp_path,monkeypatch,quantity='20')
    account=hashlib.sha256(WALLET.strip().casefold().encode()).hexdigest()
    entered,release=Event(),Event()
    original=sdk.orders[0]
    terminal=original.model_copy(update={'status':'CANCELED','size_matched':Decimal('0')})
    exact_reads,position_queries,trade_queries,settings_commits=[],[],[],[]
    def cancel(*,order_ids):
        assert order_ids==('entry-A',)
        sdk.cancels.append(order_ids);entered.set()
        assert release.wait(5),'actual cancel native completion watchdog'
        return dict(canceled=order_ids,not_canceled={})
    def exact(*,order_id):
        exact_reads.append(order_id);assert order_id=='entry-A'
        return None if proof_case=='unknown_final' else terminal
    def positions(**kwargs):
        position_queries.append(kwargs);assert kwargs==dict(size_threshold=0)
        return sdk.positions
    def trades(**kwargs):
        trade_queries.append(kwargs);assert kwargs==dict(market=CONDITION_ID)
        return ()
    sdk.cancel_orders,sdk.get_order,sdk.list_positions,sdk.list_account_trades=cancel,exact,positions,trades
    real_connect=sqlite3.connect
    class SettingsCommit(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            result=super().execute(sql,*args,**kwargs)
            if sql=='COMMIT' and threading.current_thread().name=='lp-settings-save':settings_commits.append(sql)
            return result
    def connect(*args,**kwargs):return real_connect(*args,**(kwargs|dict(factory=SettingsCommit)))
    try:
        live.store.lp_save_current_settings(account,expected_config_version=0,
            changes=dict(budget_usd='40',target_buy_count=2,buy_price_level=1))
        old=live.load_settings()
        live.run_once()
        assert live.state.snapshot().buy_reserved.value==Decimal('6')  # actual20*.30
        handle=live.cancel_independent((WALLET,CONDITION_ID,TOKEN_ID,'entry-A'))
        assert entered.wait(2) and not handle.done()
        live.request_running(False);paused=settle_control(live)
        assert paused.current.effective_running is False and paused.requested_running is False
        cycle=live.state.snapshot().current.cycle_generation
        sdk.orders=()  # current API excludes the externally terminal exact order
        sdk.positions=(dict(condition_id=CONDITION_ID,token_id=TOKEN_ID,size='0',average_price='.30'),) if proof_case=='final0_explicit_zero' else (
            (dict(condition_id=CONDITION_ID,token_id=TOKEN_ID,size='1',average_price='.30'),) if proof_case=='positive_residual' else None if proof_case=='incomplete_positions' else ())
        if proof_case!='native_pending':
            release.set();assert handle.result(timeout=3).status.value=='accepted'
            handle._thread.join(timeout=2);assert handle.done()
        clock[0]+=1
        live.run_once()
        assert live.run_protection_once()==()  # exact receipt invalidates old ordinary frame
        clock[0]+=1
        live.run_once()
        assert live.run_protection_once()==()
        before=live.state.snapshot();opening,=before.openings.values()
        order=next(o for o in before.orders if o.key[3]=='entry-A')
        assert opening.phase!='closed' and opening.entry_order_ids==('entry-A',)
        assert before.own_fills=={} and opening.economics.buy_cost_usd.state=='unknown'
        assert opening.economics.fees_usd.state=='unknown' and trade_queries==[]
        assert exact_reads and set(exact_reads)=={'entry-A'}
        if proof_case=='unknown_final':
            assert order.terminal_proof.matched.state=='unknown' and order.size_matched.value==0
        else:
            assert order.terminal_proof.identity_verified and order.terminal_proof.matched.value==0
            assert order.terminal_proof.original_size.value==Decimal('20')
            assert before.observation.order_view.orders_source.checked_at>=order.terminal_proof.source.checked_at
        if proof_case=='native_pending':
            assert not handle.done() and live.status()['native_inflight']==1
        if proof_case=='positive_residual':
            assert next(p for p in before.positions if p.token_id==TOKEN_ID).quantity.value==Decimal('1')
        if proof_case=='incomplete_positions':assert not before.observation.positions_complete
        positive=proof_case in ('final0_complete_absent','final0_explicit_zero')
        if positive:
            assert all(f.state=='known' and f.value==0 for f in (before.buy_reserved,before.inventory_cost,before.current_exposure))
            assert before.new_capacity.value==Decimal('40') and handle.done()
            assert before.observation.orders_complete and before.observation.positions_complete
            if proof_case=='final0_complete_absent':assert before.positions==()
            else:assert before.positions[0].quantity.value==0
        monkeypatch.setattr(sqlite3,'connect',connect)
        if positive:
            live.configure(dict(budget_usd='50',target_buy_count=3,buy_price_level=2,expected_config_version=1))
            deadline=time.monotonic()+3
            while live.settings_status()['pending'] and time.monotonic()<deadline:time.sleep(.001)
            new=live.settings_status()['effective']
            assert (new.budget,new.target_count,new.buy_price_level,new.config_version,new.trading_config_version)==(Decimal('50'),3,2,2,2)
            assert settings_commits==['COMMIT']
        else:
            with pytest.raises(ValueError):live.configure(dict(budget_usd='50',target_buy_count=3,buy_price_level=2,expected_config_version=1))
            assert live.policy is old and live.settings_status()['pending'] is False
            assert settings_commits==[]
        saved=live.store.lp_load_current_settings(account)
        assert (saved['budget_usd'],saved['config_version'],saved['trading_config_version'])==(('50',2,2) if positive else ('40',1,1))
        after=live.state.snapshot()
        assert after.openings==before.openings and after.sells==before.sells
        assert after.own_fills==before.own_fills and after.resolved_exit_attempts==before.resolved_exit_attempts
        assert after.positions==before.positions and after.orders==before.orders
        assert after.trade_version==before.trade_version and after.current==before.current
        assert after.current.cycle_generation==cycle and after.current.effective_running is False
        assert live.control.status().requested_version==paused.requested_version
        assert sdk.cancels==[('entry-A',)] and len(sdk.posts)==1 and position_queries
        if proof_case=='native_pending':assert not handle.done() and live.status()['native_inflight']==1
    finally:
        release.set()
        assert live.close(timeout=3)
        live.trading.close()
