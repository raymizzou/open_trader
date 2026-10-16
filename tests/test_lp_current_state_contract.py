from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import open_trader.polymarket_lp_current_state as cs
from open_trader.polymarket_lp_market_reader import CapturedSources, MarketReader, SourceResult
from open_trader.polymarket_lp_market_calculation import CalculationResult

T = datetime(2026, 10, 16, tzinfo=UTC)
D = Decimal
KEY = ('A', 'M', 'TOKEN', 'O1')


class Clock:
    def __init__(self, value=100):
        self.value = value

    def __call__(self):
        return self.value


def order(order_id='O1', matched='3', side='BUY', price='0.40'):
    return dict(account_id='A', condition_id='M', token_id='TOKEN', order_id=order_id,
                side=side, status='LIVE', price=price, original_size='10', size_matched=matched,
                order_type='GTC', expiration=None)


def frame(orders=None, *, balance=True, checked_at=T):
    return MarketReader.from_captured_sources(CapturedSources('A', {
        'open_orders': SourceResult('orders-1', [order()] if orders is None else orders, checked_at=checked_at),
        'positions': SourceResult('positions-1', [dict(account_id='A', condition_id='M', token_id='TOKEN',
            quantity='2', cost_usd='1.00', average_price='0.50', redeemable=False)], checked_at=checked_at),
        'balance': SourceResult('balance-1', ('20', '20') if balance else None,
            complete=balance, checked_at=checked_at, error=None if balance else 'unavailable'),
    }))


def calculation(source, yields=None, capacity=D('16.20')):
    return CalculationResult(source.buy_reserved, source.inventory_cost, capacity,
        MappingProxyType({'O1': D('0')} if yields is None else yields))


def state(clock=None):
    result = cs.CurrentLPState('A', 'process', clock=clock or Clock(), utc_now=lambda: T)
    result.apply_effective_control(cs.EffectiveControlTransition(
        'A', 'process', ('process', 0), 0, None, None, True, 'startup'), 'opaque-generation')
    return result


def observe(current, source=None, *, yields=None, capacity=D('16.20')):
    source = source or frame()
    ticket = current.begin_observation('opaque-generation', tuple(source.sources))
    return current.accept(ticket, source, calculation(source, yields, capacity), evaluated_at=T)


def test_snapshot_preserves_current_identity_quantity_cost_and_source_time():
    current = state()
    source = frame()
    prepared = calculation(source)
    ticket = current.begin_observation('opaque-generation', tuple(source.sources))
    accepted = current.accept(ticket, source, prepared, evaluated_at=T)
    snapshot = current.snapshot()
    assert accepted.status == 'accepted'
    assert snapshot.observation.source_frame is source
    assert snapshot.observation.calculation is prepared
    assert snapshot.orders[0].key == KEY
    assert snapshot.orders[0].remaining.value == D('7')
    assert snapshot.orders[0].source.checked_at == T
    assert snapshot.orders[0].source.source_id == 'orders-1'
    assert snapshot.buy_reserved.value == D('2.80')
    assert snapshot.inventory_cost.value == D('1.00')
    assert snapshot.current_exposure.value == D('3.80')
    assert snapshot.positions[0].quantity.value == D('2')
    assert snapshot.observation.order_view.source_frame is source


def test_late_observation_cannot_replace_new_order_or_pause():
    current = state()
    old = current.begin_observation('opaque-generation', ('open_orders',))
    newer = current.begin_observation('opaque-generation', ('open_orders',))
    current.apply_effective_control(cs.EffectiveControlTransition(
        'A', 'process', ('process', 2), 2, 0, True, False, 'pause'), 'opaque-generation')
    new_frame = frame()
    assert current.accept(newer, new_frame, calculation(new_frame), evaluated_at=T).status == 'accepted'
    old_frame = frame([order(matched='0')], checked_at=datetime(2026, 10, 15, tzinfo=UTC))
    result = current.accept(old, old_frame, calculation(old_frame), evaluated_at=T)
    assert result.status == 'stale'
    snapshot = current.snapshot()
    assert snapshot.orders[0].size_matched.value == D('3')
    assert snapshot.orders[0].source.checked_at == T
    assert snapshot.current.effective_running is False
    assert snapshot.current.control_version == 2


def test_partial_failure_keeps_independent_current_yield_unknown_budget():
    current = state()
    source = frame(balance=False)
    result = observe(current, source, yields={'O1': D('0')}, capacity=None)
    assert result.status == 'partial'
    snapshot = current.snapshot()
    assert snapshot.yields[KEY].yield_pct_per_hour.state == 'known'
    assert snapshot.yields[KEY].yield_pct_per_hour.value == D('0')
    assert snapshot.new_capacity.state == 'unknown'
    assert snapshot.new_capacity.value is None
    assert [o.key for o in snapshot.orders] == [KEY]
    assert snapshot.observation.source_frame is source
    assert snapshot.observation.order_view.orders_complete is True
    assert snapshot.observation.components['balance'].error_code == 'unavailable'


def test_terminal_order_leaves_active_observation_without_invented_fill():
    current = state()
    observe(current, frame([order(), order('O2', matched='0')]), yields={'O1': None, 'O2': D('1')})
    stamp = cs.SourceStamp('exact-O1', checked_at=T, complete=True, state='known')
    proof = cs.TerminalProof(KEY, 'CANCELED', cs.Fact('known', D('10'), (stamp,)),
        cs.Fact('unknown', sources=(stamp,)), stamp, True)
    evidence = cs.ReceiptEvidence('receipt-1', 'request-cancel-O1', 'A', 'process',
        'older-generation', KEY, 'BUY', 1, terminal_proof=proof)
    assert current.accept_receipt_evidence(evidence) == 'applied'
    snapshot = current.snapshot()
    assert [o.key[3] for o in snapshot.active_orders] == ['O2']
    assert KEY not in snapshot.yields
    assert snapshot.ordinary_poll_order_keys == (('A', 'M', 'TOKEN', 'O2'),)
    terminal = next(o for o in snapshot.orders if o.key == KEY)
    assert terminal.terminal_proof == proof
    assert terminal.terminal_proof.matched.state == 'unknown'
    assert terminal.size_matched.value == D('3')
    assert snapshot.own_fills.get(KEY) is None
    assert snapshot.current.cycle_generation == 'opaque-generation'


def test_unknown_yield_deadline_known_zero_new_id_and_restart():
    clock = Clock(100)
    current = state(clock)
    source = frame([order(matched='0'), order('SELL', side='SELL')])
    observe(current, source, yields={'O1': None, 'SELL': None})
    assert current.snapshot().yields[KEY].unknown_since_monotonic == 100
    clock.value = 699
    observe(current, source, yields={'O1': None})
    assert current.snapshot().unknown_yield_due == ()
    clock.value = 700
    observe(current, source, yields={'O1': None})
    assert current.snapshot().unknown_yield_due == (KEY,)
    assert len(current.snapshot().yields) == 1
    observe(current, source, yields={'O1': D('0')})
    assert current.snapshot().yields[KEY].unknown_since_monotonic is None
    clock.value = 800
    observe(current, source, yields={'O1': None})
    assert current.snapshot().yields[KEY].unknown_deadline_monotonic == 1400
    clock.value = 900
    observe(current, frame([order('O2', matched='0')]), yields={'O2': None})
    assert current.snapshot().yields[('A', 'M', 'TOKEN', 'O2')].unknown_deadline_monotonic == 1500
    restarted = state(Clock(1000))
    observe(restarted, source, yields={'O1': None})
    assert restarted.snapshot().yields[KEY].unknown_deadline_monotonic == 1600


def test_protection_view_uses_current_terms_and_honest_baseline():
    current = state()
    observe(current, frame([order(), order('O2', matched='0')]))
    snapshot = current.snapshot()
    key = cs.ProtectionKey('queue_bucket', ('A', 'M', 'TOKEN', 'group'), D('0.40'), None)
    stamp = cs.SourceStamp('current-book', checked_at=T, source_timestamp=T, complete=True, state='known')
    baseline = cs.ProtectionBaseline(key, ('O1',), D('0.40'), cs.Fact('known', D('12'), (stamp,)),
        'submit', T, T, 'hash-1', 1, T, D('0.5'), stamp)
    guard = cs.ProtectionGuard(key, 'opaque-generation', 0, ((KEY, 1),),
        snapshot.observation.observation_id, 'baseline-event')
    assert current.apply_protection(cs.SetBaseline(guard, baseline)) == 'applied'
    protected = current.snapshot()
    assert protected.orders[0].key == KEY
    assert protected.orders[0].remaining.value == D('7')
    assert protected.orders[0].source.checked_at == T
    assert protected.baselines[key] == baseline
    assert all('O2' not in b.anchor_order_ids for b in protected.baselines.values())
    # Audit payloads have no state restoration entry point.
    audit_sink = [{'order_id': 'O2', 'baseline_front': '999'}]
    assert audit_sink[0]['baseline_front'] == '999'
    observe(current, frame([order(matched='4'), order('O2', matched='0')]))
    late = cs.SetBaseline(cs.ProtectionGuard(key, 'opaque-generation', 1, ((KEY, 1),),
        None, 'late-old-terms'), baseline)
    assert current.apply_protection(late) == 'stale'
    assert current.snapshot().baselines[key] == baseline


def test_resume_restarts_unknown_timer_at_first_new_observation():
    clock = Clock(100)
    current = state(clock)
    source = frame()
    observe(current, source, yields={'O1': None})
    clock.value = 600
    old = current.begin_observation('opaque-generation', tuple(source.sources))
    current.apply_effective_control(cs.EffectiveControlTransition(
        'A', 'process', ('process', 1), 1, 0, True, False, 'pause'), 'opaque-generation')
    current.apply_effective_control(cs.EffectiveControlTransition(
        'A', 'process', ('process', 2), 2, 1, False, True, 'explicit_resume'), 'new-opaque-cycle')
    assert current.snapshot().yields[KEY].unknown_since_monotonic is None
    assert current.accept(old, source, calculation(source, {'O1': None}), evaluated_at=T).status == 'stale'
    assert current.snapshot().yields[KEY].unknown_since_monotonic is None
    assert current.snapshot().orders[0].remaining.value == D('7')
    assert current.snapshot().inventory_cost.value == D('1.00')
    clock.value = 900
    fresh = current.begin_observation('new-opaque-cycle', tuple(source.sources))
    current.accept(fresh, source, calculation(source, {'O1': None}), evaluated_at=T)
    assert current.snapshot().yields[KEY].unknown_since_monotonic == 900
    assert current.snapshot().yields[KEY].yield_pct_per_hour.state == 'unknown'
    clock.value = 1499
    assert current.snapshot().unknown_yield_due == ()
    clock.value = 1500
    assert current.snapshot().unknown_yield_due == (KEY,)



def test_rejected_order_terms_cannot_clear_unknown_yield_timer():
    clock = Clock(100)
    current = state(clock)
    observe(current, yields={'O1': None})
    original_yield = current.snapshot().yields[KEY]
    clock.value = 600
    source = frame([order(matched='0'), order('O2', matched='0')], checked_at=T - timedelta(seconds=1))
    prepared = calculation(source, {'O1': D('0'), 'O2': D('0.10')}, capacity=D('11'))
    ticket = current.begin_observation('opaque-generation', tuple(source.sources))
    result = current.accept(ticket, source, prepared, evaluated_at=T)
    snapshot = current.snapshot()
    retained = next(o for o in snapshot.orders if o.key == KEY)
    assert retained.size_matched.value == D('3')
    assert retained.source.checked_at == T
    assert snapshot.yields[KEY].yield_pct_per_hour.state == 'unknown'
    assert snapshot.yields[KEY].unknown_since_monotonic == 100
    assert snapshot.yields[KEY].unknown_deadline_monotonic == 700
    assert snapshot.yields[KEY] == original_yield
    independent = snapshot.yields[('A', 'M', 'TOKEN', 'O2')]
    assert independent.yield_pct_per_hour.state == 'known'
    assert independent.yield_pct_per_hour.value == D('0.10')
    assert snapshot.buy_reserved.state == 'unknown'
    assert snapshot.buy_reserved.value is None
    assert snapshot.new_capacity.state == 'unknown'
    assert snapshot.new_capacity.value is None
    assert result.observation.source_frame is source
    assert result.observation.calculation is prepared
    assert source.orders[0]['size_matched'] == '0'
    assert prepared.yields['O1'] == D('0')
    assert prepared.buy_reserved == D('8.00')


def test_terminal_order_list_echo_cannot_restore_calculated_reservation():
    current = state()
    source = frame()
    observe(current, source)
    stamp = cs.SourceStamp('exact-terminal', checked_at=T, complete=True, state='known')
    proof = cs.TerminalProof(KEY, 'CANCELED', cs.Fact('known', D('10'), (stamp,)),
        cs.Fact('known', D('3'), (stamp,)), stamp, True)
    receipt = cs.ReceiptEvidence('terminal', 'cancel-O1', 'A', 'process',
        'opaque-generation', KEY, 'BUY', 1, terminal_proof=proof)
    assert current.accept_receipt_evidence(receipt) == 'applied'
    assert current.snapshot().buy_reserved.value == D('0')
    prepared = calculation(source, {'O1': D('0')}, capacity=D('17.20'))
    ticket = current.begin_observation('opaque-generation', tuple(source.sources))
    result = current.accept(ticket, source, prepared, evaluated_at=T)
    snapshot = current.snapshot()
    assert snapshot.active_orders == ()
    assert snapshot.orders[0].terminal_proof is proof
    assert snapshot.orders[0].source == stamp
    assert snapshot.buy_reserved.state == 'unknown'
    assert snapshot.buy_reserved.value is None
    assert snapshot.new_capacity.state == 'unknown'
    assert snapshot.new_capacity.value is None
    assert KEY not in snapshot.yields
    assert snapshot.unknown_yield_due == ()
    assert result.observation.source_frame is source
    assert result.observation.calculation is prepared
    assert source.orders[0]['status'] == 'LIVE'
    assert prepared.buy_reserved == D('2.80')
    assert prepared.new_capacity == D('17.20')


def test_late_exit_receipt_cannot_regress_completed_native_state():
    current = state()
    sell_row = {**order('SELL1', matched='1', side='SELL'), 'original_size': '2'}
    observe(current, frame([sell_row]), yields={})
    sell_key = ('A', 'M', 'TOKEN', 'SELL1')
    scope = ('A', 'M', 'TOKEN', 'opening')
    stamp = cs.SourceStamp('exact-sell', checked_at=T, complete=True, state='known')
    known = lambda value: cs.Fact('known', value, (stamp,))
    fill = cs.OwnFillEvidence(sell_key, known(D('1')), known(D('0.40')), known(D('0.01')),
        ('fill-1',), stamp, False)
    attempt = cs.ExitAttempt('sell-request', 'passive_exit', sell_key, 'accepted', False,
        known(D('2')), known(D('0')), known(D('2')), known(D('0.40')), known('GTC'),
        known(None), None, fill, 'ended')
    sell = cs.SellState(scope, known(D('2')), known(D('1.00')), True,
        current.snapshot().orders, stamp, stamp, attempt, None)
    guard = cs.ProtectionGuard(scope, 'opaque-generation', 0, ((sell_key, 1),),
        current.snapshot().observation.observation_id, 'exit-created')
    assert current.apply_protection(cs.ExitEvidenceUpdate(guard, sell)) == 'applied'
    old_attempt = replace(attempt, native_state='running', own_fill_evidence=None)
    old_guard = replace(guard, expected_scope_version=1, event_id='guarded-old-status')
    assert current.apply_protection(cs.ExitEvidenceUpdate(old_guard, replace(sell, passive=old_attempt))) == 'invalid_identity'
    receipt = cs.ReceiptEvidence('late-native-running', 'sell-request', 'A', 'process', 'old-cycle',
        sell_key, 'SELL', 1, exit_attempt=old_attempt, scope=scope)
    assert current.accept_receipt_evidence(receipt) == 'stale_evidence'
    snapshot = current.snapshot()
    assert snapshot.sells[scope].passive == attempt
    assert snapshot.sells[scope].passive.own_fill_evidence == fill
    assert snapshot.sells[scope].protected is None
    wrong_terms = replace(receipt, event_id='wrong-price', exit_attempt=replace(attempt, price=known(D('0.50'))))
    assert current.accept_receipt_evidence(wrong_terms) == 'identity_error'
    # Actual forward terminal/fill evidence is still admitted for this same request.
    final_fill = replace(fill, quantity=known(D('2')), notional_usd=known(D('0.80')),
        fees_usd=known(D('0.02')), fill_ids=('fill-1', 'fill-2'), complete_for_target=True)
    proof = cs.TerminalProof(sell_key, 'FILLED', known(D('2')), known(D('2')), stamp, True)
    final_attempt = replace(attempt, state='terminal', own_fill_evidence=final_fill)
    forward = replace(receipt, event_id='actual-terminal', terminal_proof=proof,
        own_fill=final_fill, exit_attempt=final_attempt)
    assert current.accept_receipt_evidence(forward) == 'applied'
    final = current.snapshot()
    assert final.sells[scope].passive == final_attempt
    assert final.orders[0].terminal_proof == proof
    assert final.own_fills[sell_key] == final_fill
    late = replace(receipt, event_id='late-pre-terminal', exit_attempt=replace(attempt, own_fill_evidence=None))
    assert current.accept_receipt_evidence(late) == 'stale_evidence'
    assert current.snapshot().sells[scope].passive == final_attempt
    assert current.snapshot().orders[0].terminal_proof == proof
    assert current.snapshot().own_fills[sell_key] == final_fill
