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
    original_order = current.snapshot().orders[0]
    stamp = cs.SourceStamp('exact-newer-O1', checked_at=T + timedelta(seconds=1), complete=True, state='known')
    newer_terms = {name: replace(getattr(original_order, name), sources=(stamp,))
        for name in ('price', 'original_size', 'size_matched', 'remaining', 'order_type', 'expiration')}
    receipt_order = replace(original_order, source=stamp, **newer_terms)
    receipt = cs.ReceiptEvidence('newer-O1-facts', 'O1-request', 'A', 'process',
        'opaque-generation', KEY, 'BUY', 1, order_fact=receipt_order)
    assert current.accept_receipt_evidence(receipt) == 'applied'
    clock.value = 600
    # The whole component is current; only O1 has newer exact receipt evidence.
    source = frame([order(matched='0'), order('O2', matched='0')], checked_at=T)
    prepared = calculation(source, {'O1': D('0'), 'O2': D('0.10')}, capacity=D('11'))
    ticket = current.begin_observation('opaque-generation', tuple(source.sources))
    result = current.accept(ticket, source, prepared, evaluated_at=T)
    snapshot = current.snapshot()
    retained = next(o for o in snapshot.orders if o.key == KEY)
    assert retained.size_matched.value == D('3')
    assert retained.source.checked_at == T + timedelta(seconds=1)
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


def test_old_complete_order_source_cannot_remove_or_revive_current_orders():
    clock = Clock(100)
    current = state(clock)
    observe(current, yields={'O1': None})
    original = current.snapshot()
    clock.value = 600
    stale_empty = frame([], checked_at=T - timedelta(seconds=1))
    result = observe(current, stale_empty, yields={}, capacity=D('20'))
    snapshot = current.snapshot()
    assert snapshot.active_orders == original.active_orders
    assert snapshot.orders[0].source.checked_at == T
    assert snapshot.yields[KEY] == original.yields[KEY]
    assert snapshot.yields[KEY].unknown_deadline_monotonic == 700
    assert snapshot.buy_reserved.state == 'unknown'
    assert snapshot.buy_reserved.value is None
    assert snapshot.new_capacity.state == 'unknown'
    assert snapshot.new_capacity.value is None
    assert result.observation.source_frame is stale_empty
    assert stale_empty.sources['open_orders'].checked_at == T - timedelta(seconds=1)
    # Valid independent position facts remain applicable even when order input is stale.
    assert snapshot.positions[0].quantity.value == D('2')
    # An old whole source also cannot introduce an ID never observed before.
    stale_base = frame([order(matched='0'), order('O2', matched='0')], checked_at=T - timedelta(seconds=1))
    stale_with_current_positions = MarketReader.from_captured_sources(CapturedSources('A', {
        **stale_base.sources,
        'positions': SourceResult('independent-current-positions', [dict(account_id='A', condition_id='M',
            token_id='TOKEN', quantity='3', cost_usd='1.50')], checked_at=T + timedelta(seconds=1))}))
    stale_result = observe(current, stale_with_current_positions, yields={'O1': D('0'), 'O2': D('0.10')})
    before_empty = current.snapshot()
    assert tuple(o.key for o in before_empty.active_orders) == (KEY,)
    assert all(o.key[3] != 'O2' for o in before_empty.orders)
    assert ('A', 'M', 'TOKEN', 'O2') not in before_empty.yields
    assert before_empty.yields[KEY] == original.yields[KEY]
    assert before_empty.yields[KEY].unknown_deadline_monotonic == 700
    assert before_empty.orders[0].source.checked_at == T
    assert before_empty.buy_reserved.state == 'unknown'
    assert before_empty.new_capacity.state == 'unknown'
    assert before_empty.positions[0].quantity.value == D('3')
    assert before_empty.positions[0].inventory_cost_usd.value == D('1.50')
    assert before_empty.positions[0].source.checked_at == T + timedelta(seconds=1)
    assert stale_result.observation.source_frame is stale_with_current_positions
    assert stale_with_current_positions.sources['open_orders'].checked_at == T - timedelta(seconds=1)
    newer_empty = frame([], checked_at=T + timedelta(seconds=1))
    observe(current, newer_empty, yields={}, capacity=D('19'))
    empty = current.snapshot()
    assert empty.active_orders == ()
    assert empty.yields == {}
    assert empty.buy_reserved.value == D('0')
    assert empty.orders[0].terminal_proof is None
    assert empty.own_fills.get(KEY) is None
    stale_nonempty = frame(checked_at=T)
    observe(current, stale_nonempty, yields={'O1': D('0')})
    final = current.snapshot()
    assert final.active_orders == ()
    assert final.yields == {}
    assert final.orders[0].presence == 'absent'
    assert final.buy_reserved.state == 'unknown'
    assert final.new_capacity.state == 'unknown'
    assert stale_nonempty.sources['open_orders'].checked_at == T


def test_old_position_source_cannot_replace_current_inventory_or_budget():
    current = state()

    def position_frame(at, cost, *, order_time=T):
        base = frame([order('O2', matched='0')], checked_at=order_time)
        rows = [] if cost is None else [dict(account_id='A', condition_id='M', token_id='TOKEN',
            quantity='20' if cost == '10' else '2', cost_usd=cost)]
        return MarketReader.from_captured_sources(CapturedSources('A', {
            **base.sources, 'positions': SourceResult('positions-' + at.isoformat(), rows, checked_at=at)}))

    initial = position_frame(T, '10')
    observe(current, initial, yields={'O2': D('0.10')}, capacity=D('10'))
    original = current.snapshot().positions
    assert original[0].inventory_cost_usd.value == D('10')
    for cost, prepared_inventory, prepared_capacity in (('1', D('1'), D('19')), (None, D('0'), D('20'))):
        source = position_frame(T - timedelta(seconds=1), cost, order_time=T + timedelta(seconds=1))
        prepared = calculation(source, {'O2': D('0.10')}, capacity=prepared_capacity)
        assert prepared.inventory_cost == prepared_inventory
        ticket = current.begin_observation('opaque-generation', tuple(source.sources))
        result = current.accept(ticket, source, prepared, evaluated_at=T)
        snapshot = current.snapshot()
        assert snapshot.positions == original
        assert snapshot.positions[0].source.checked_at == T
        assert snapshot.inventory_cost.state == 'unknown'
        assert snapshot.inventory_cost.value is None
        assert snapshot.new_capacity.state == 'unknown'
        assert snapshot.new_capacity.value is None
        assert snapshot.yields[('A', 'M', 'TOKEN', 'O2')].yield_pct_per_hour.value == D('0.10')
        assert result.observation.order_view.orders_complete is True
        assert result.observation.source_frame is source
        assert result.observation.calculation is prepared
        assert source.sources['positions'].checked_at == T - timedelta(seconds=1)
    newer = position_frame(T + timedelta(seconds=1), '1', order_time=T + timedelta(seconds=1))
    observe(current, newer, yields={'O2': D('0.10')}, capacity=D('15'))
    final = current.snapshot()
    assert final.positions[0].inventory_cost_usd.value == D('1')
    assert final.positions[0].source.checked_at == T + timedelta(seconds=1)
    assert final.inventory_cost.value == D('1')
    assert final.new_capacity.value == D('15')


def test_fresh_matching_terminal_source_keeps_valid_financial_capacity():
    current = state()
    first_proof = None
    for index in (0, 1):
        at = T + timedelta(seconds=index)
        row = {**order(), 'status': 'CANCELED'}
        source = MarketReader.from_captured_sources(CapturedSources('A', {
            'open_orders': SourceResult('terminal-orders-' + str(index), [row], checked_at=at),
            'positions': SourceResult('terminal-positions-' + str(index), [], checked_at=at),
            'balance': SourceResult('terminal-balance-' + str(index), ('20', '20'), checked_at=at)}))
        prepared = calculation(source, {}, capacity=D('20'))
        ticket = current.begin_observation('opaque-generation', tuple(source.sources))
        accepted = current.accept(ticket, source, prepared, evaluated_at=at)
        snapshot = current.snapshot()
        assert snapshot.buy_reserved.state == 'known'
        assert snapshot.buy_reserved.value == D('0')
        assert snapshot.inventory_cost.value == D('0')
        assert snapshot.new_capacity.state == 'known'
        assert snapshot.new_capacity.value == D('20')
        assert accepted.status == 'accepted'
        assert snapshot.active_orders == ()
        assert snapshot.yields == {}
        assert snapshot.unknown_yield_due == ()
        assert snapshot.orders[0].terminal_proof.status == 'CANCELED'
        assert snapshot.orders[0].size_matched.value == D('3')
        assert snapshot.own_fills.get(KEY) is None
        if first_proof is None:
            first_proof = snapshot.orders[0].terminal_proof
        assert snapshot.orders[0].terminal_proof is first_proof
        assert accepted.observation.source_frame is source
        assert accepted.observation.calculation is prepared
        assert source.sources['open_orders'].checked_at == at


def test_expanded_fill_coverage_does_not_promote_partial_money_to_totals():
    current = state()
    sell_key = KEY
    scope = ('A', 'M', 'TOKEN', 'opening')
    source = frame([{**order(side='SELL', matched='1'), 'original_size': '2'}])
    observe(current, source, yields={})
    stamp = cs.SourceStamp('exact-fill', checked_at=T, complete=True, state='known')
    known = lambda value: cs.Fact('known', value, (stamp,))
    unknown = cs.Fact('unknown', sources=(stamp,))
    first = cs.OwnFillEvidence(sell_key, known(D('1')), known(D('0.40')), known(D('0.01')),
        ('fill-1',), stamp, True)
    attempt = cs.ExitAttempt('sell-request', 'passive_exit', sell_key, 'accepted', False,
        known(D('2')), known(D('0')), known(D('2')), known(D('0.40')), known('GTC'),
        known(None), None, first, 'ended')
    sell = cs.SellState(scope, known(D('2')), known(D('1')), True,
        current.snapshot().orders, stamp, stamp, attempt, None)
    guard = cs.ProtectionGuard(scope, 'opaque-generation', 0, ((sell_key, 1),),
        current.snapshot().observation.observation_id, 'register-fill')
    assert current.apply_protection(cs.ExitEvidenceUpdate(guard, sell)) == 'applied'
    register = cs.ReceiptEvidence('canonical-fill', 'sell-request', 'A', 'process', 'opaque-generation',
        sell_key, 'SELL', 1, own_fill=first, exit_attempt=attempt, scope=scope)
    assert current.accept_receipt_evidence(register) == 'applied'
    # Same-coverage sparse evidence may retain exactly its own proven amounts.
    sparse = replace(first, notional_usd=unknown, fees_usd=unknown, complete_for_target=False)
    same_coverage = replace(register, event_id='same-coverage-sparse', own_fill=sparse,
        exit_attempt=replace(attempt, own_fill_evidence=sparse))
    assert current.accept_receipt_evidence(same_coverage) == 'applied'
    same = current.snapshot()
    assert same.sells[scope].passive.own_fill_evidence.notional_usd.value == D('0.40')
    assert same.sells[scope].passive.own_fill_evidence.fees_usd.value == D('0.01')
    assert same.own_fills[sell_key] == same.sells[scope].passive.own_fill_evidence
    expanded = replace(first, quantity=known(D('2')), notional_usd=unknown, fees_usd=unknown,
        fill_ids=('fill-1', 'fill-2'), complete_for_target=False)
    final_attempt = replace(attempt, state='terminal', own_fill_evidence=expanded)
    proof = cs.TerminalProof(sell_key, 'FILLED', known(D('2')), known(D('2')), stamp, True)
    forward = replace(register, event_id='expanded-coverage', terminal_proof=proof,
        own_fill=expanded, exit_attempt=final_attempt)
    assert current.accept_receipt_evidence(forward) == 'applied'
    snapshot = current.snapshot()
    canonical = snapshot.own_fills[sell_key]
    merged = snapshot.sells[scope].passive.own_fill_evidence
    assert canonical == merged
    assert merged.fill_ids == ('fill-1', 'fill-2')
    assert merged.quantity.value == D('2')
    assert merged.notional_usd.state == 'unknown'
    assert merged.notional_usd.value is None
    assert merged.fees_usd.state == 'unknown'
    assert merged.fees_usd.value is None
    assert merged.complete_for_target is False
    assert snapshot.sells[scope].passive.native_state == 'ended'
    assert snapshot.sells[scope].passive.request_id == 'sell-request'
    assert snapshot.orders[0].terminal_proof == proof
    assert first.notional_usd.value == D('0.40')
    assert expanded.notional_usd.state == 'unknown'


def test_id_only_cancel_unknown_final_quantity_resolves_from_fresh_terminal():
    current = state()
    original_frame = frame([order(matched='0')])
    observe(current, original_frame, yields={'O1': None})
    before = current.snapshot()
    original = before.orders[0]
    ack_stamp = cs.SourceStamp('cancel-ID-only', checked_at=T + timedelta(seconds=1),
        complete=True, state='known')
    proof = cs.TerminalProof(KEY, 'CANCELED', original.original_size,
        cs.Fact('unknown', sources=(ack_stamp,)), ack_stamp, True)
    assert current.accept_receipt_evidence(cs.ReceiptEvidence('cancel-unknown', 'cancel-O1',
        'A', 'process', 'opaque-generation', KEY, 'BUY', original.terms_version,
        terminal_proof=proof)) == 'applied'
    acknowledged = current.snapshot()
    canceled = acknowledged.orders[0]
    assert canceled.terminal_proof is proof
    assert canceled.terminal_proof.matched.state == 'unknown'
    assert canceled.original_size is original.original_size
    assert canceled.size_matched is original.size_matched
    assert canceled.size_matched.value == D('0')
    assert canceled.size_matched.sources[0].checked_at == T
    assert canceled.remaining.state == 'unknown' and canceled.remaining.value is None
    assert acknowledged.active_orders == acknowledged.ordinary_poll_order_keys == ()
    assert KEY not in acknowledged.yields and not acknowledged.own_fills
    assert acknowledged.observation is before.observation

    terminal_frame = frame([dict(order(matched='3'), status='CANCELED')],
        checked_at=T + timedelta(seconds=2))
    prepared = calculation(terminal_frame, {}, D('19'))
    ticket = current.begin_observation('opaque-generation', tuple(terminal_frame.sources))
    assert current.accept(ticket, terminal_frame, prepared, evaluated_at=T + timedelta(seconds=2)).status == 'accepted'
    resolved = current.snapshot()
    final = resolved.orders[0]
    assert final.venue_status == 'CANCELED'
    assert final.terminal_proof.matched.value == final.size_matched.value == D('3')
    assert final.remaining.value == D('7')
    assert final.source.checked_at == T + timedelta(seconds=2)
    assert resolved.buy_reserved.value == D('0') and resolved.new_capacity.value == D('19')
    assert not resolved.own_fills and not resolved.yields and not resolved.active_orders
    assert resolved.observation.source_frame is terminal_frame
    assert resolved.observation.calculation is prepared
    assert before.observation.source_frame is original_frame
    assert before.orders[0].size_matched.value == D('0')


def test_cancel_unknown_final_quantity_preserves_progress_and_terminal_guards():
    def acknowledged_state():
        current = state()
        observe(current)
        initial = current.snapshot().orders[0]
        stamp = cs.SourceStamp('ID-only', checked_at=T + timedelta(seconds=1), complete=True, state='known')
        proof = cs.TerminalProof(KEY, 'CANCELED', initial.original_size,
            cs.Fact('unknown', sources=(stamp,)), stamp, True)
        assert current.accept_receipt_evidence(cs.ReceiptEvidence('ack', 'cancel', 'A', 'process',
            'opaque-generation', KEY, 'BUY', initial.terms_version, terminal_proof=proof)) == 'applied'
        canceled = current.snapshot().orders[0]
        assert canceled.size_matched is initial.size_matched
        assert canceled.size_matched.value == D('3') and canceled.size_matched.sources[0].checked_at == T
        assert canceled.terminal_proof.matched.state == canceled.remaining.state == 'unknown'
        return current

    current = acknowledged_state()
    acknowledged = current.snapshot().orders[0]
    for second, changes in [(0, {}), (2, {'status': 'LIVE'}), (3, {'price': '0.50'}),
            (4, {'original_size': '11'}), (5, {'order_type': 'GTD', 'expiration': 123}),
            (6, {'size_matched': '2'})]:
        row = dict(order(matched='4'), status='CANCELED')
        row.update(changes)
        source = frame([row], checked_at=T + timedelta(seconds=second))
        assert observe(current, source).status == 'partial'
        snapshot = current.snapshot()
        assert snapshot.orders[0] is acknowledged
        assert snapshot.orders[0].terminal_proof.matched.state == 'unknown'
        assert snapshot.orders[0].source.checked_at == T + timedelta(seconds=1)
        assert snapshot.buy_reserved.state == snapshot.new_capacity.state == 'unknown'
    source = frame([dict(order(matched='4'), status='CANCELED')], checked_at=T + timedelta(seconds=7))
    assert observe(current, source, capacity=D('19')).status == 'accepted'
    final = current.snapshot().orders[0]
    assert final.terminal_proof.matched.value == final.size_matched.value == D('4')
    assert final.remaining.value == D('6')
    for index, (second, value) in enumerate([(6, '4'), (8, None), (9, '3')]):
        stamp = cs.SourceStamp('late-proof', checked_at=T + timedelta(seconds=second), complete=True, state='known')
        proof = cs.TerminalProof(KEY, 'CANCELED', final.original_size,
            cs.Fact('unknown', sources=(stamp,)) if value is None else cs.Fact('known', D(value), (stamp,)), stamp, True)
        before = current.snapshot()
        result = current.accept_receipt_evidence(cs.ReceiptEvidence(f'late-{index}', 'cancel', 'A', 'process',
            'opaque-generation', KEY, 'BUY', 1, terminal_proof=proof))
        assert result in ('stale_evidence', 'identity_error')
        after = current.snapshot()
        assert after.orders == before.orders and after.snapshot_version == before.snapshot_version
        assert after.new_capacity == before.new_capacity

    receipt_state = acknowledged_state()
    acknowledged = receipt_state.snapshot().orders[0]
    for second, value in [(0, '4'), (2, '2')]:
        stamp = cs.SourceStamp('unusable-proof', checked_at=T + timedelta(seconds=second), complete=True, state='known')
        proof = cs.TerminalProof(KEY, 'CANCELED', acknowledged.original_size, cs.Fact('known', D(value), (stamp,)), stamp, True)
        before = receipt_state.snapshot()
        assert receipt_state.accept_receipt_evidence(cs.ReceiptEvidence(f'bad-{second}', 'cancel', 'A', 'process',
            'opaque-generation', KEY, 'BUY', 1, terminal_proof=proof)) in ('stale_evidence', 'identity_error')
        assert receipt_state.snapshot().snapshot_version == before.snapshot_version
        assert receipt_state.snapshot().orders[0] is acknowledged
    stamp = cs.SourceStamp('final-proof', checked_at=T + timedelta(seconds=3), complete=True, state='known')
    proof = cs.TerminalProof(KEY, 'CANCELED', acknowledged.original_size, cs.Fact('known', D('4'), (stamp,)), stamp, True)
    assert receipt_state.accept_receipt_evidence(cs.ReceiptEvidence('final', 'cancel', 'A', 'process',
        'opaque-generation', KEY, 'BUY', 1, terminal_proof=proof)) == 'applied'
    final = receipt_state.snapshot().orders[0]
    assert final.terminal_proof is proof and final.size_matched.value == D('4') and final.remaining.value == D('6')
    assert final.size_matched.sources == (stamp,)
    assert not receipt_state.snapshot().own_fills


class ExecutionClock:
    def __init__(self):
        self.second = 0

    def utc_now(self):
        return T + timedelta(seconds=self.second)

    def monotonic(self):
        return 100 + self.second


def execution_state():
    clock = ExecutionClock()
    current = cs.CurrentLPState('A', 'process', clock=clock.monotonic, utc_now=clock.utc_now)
    current.apply_effective_control(cs.EffectiveControlTransition('A', 'process',
        ('process', 0), 0, None, None, True, 'startup'), 'opaque-generation')
    source = cash_frame()
    ticket = current.begin_observation('opaque-generation', tuple(source.sources))
    assert current.accept(ticket, source, calculation(source, {}, D('40')), evaluated_at=T).status == 'accepted'
    return current, clock


def cash_frame(orders=(), *, orders_at=T, positions_at=None, balance_at=None):
    return MarketReader.from_captured_sources(CapturedSources('A', {
        'open_orders': SourceResult('cash-orders', list(orders), checked_at=orders_at),
        'positions': SourceResult('cash-positions', [], checked_at=positions_at or orders_at),
        'balance': SourceResult('cash-balance', ('40', '40'), checked_at=balance_at or orders_at),
    }))


def buy_uncertainty(**changes):
    values = dict(event_id='uncertain-1', request_id='native-buy-1', account_id='A',
        process_epoch='process', cycle_generation='opaque-generation', batch_id='batch-1',
        round_number=0, observation_id='sealed-observation', venue='polymarket',
        condition_id='M', token_id='TOKEN', price=D('0.40'), quantity=D('10'),
        order_type='GTC', expiration=None, post_only=True,
        send_options=(('tick_size', '0.01'), ('neg_risk', False)),
        native_started_at=T + timedelta(seconds=1), observed_at=T + timedelta(seconds=2),
        reason='unknown_timeout', venue_order_id=None)
    values.update(changes)
    return cs.BuyExecutionUncertainty(**values)


def test_uncertain_buy_without_order_id_invalidates_financial_authority_and_prior_read():
    current, clock = execution_state()
    before = current.snapshot()
    assert [f.value for f in (before.buy_reserved, before.inventory_cost, before.current_exposure,
        before.new_capacity)] == [D('0'), D('0'), D('0'), D('40')]
    historical = before.observation
    old_ticket = current.begin_observation('opaque-generation', tuple(historical.source_frame.sources))
    clock.second = 2
    event = buy_uncertainty()
    assert current.accept_buy_execution_uncertainty(event) == 'applied'
    after = current.snapshot()
    assert after.trade_version == before.trade_version + 1
    assert after.snapshot_version == before.snapshot_version + 1
    for fact in (after.buy_reserved, after.inventory_cost, after.current_exposure, after.new_capacity):
        assert fact.state == 'unknown' and fact.value is None
    assert after.orders == after.positions == () and not after.own_fills
    assert after.current == before.current and after.yields == before.yields
    assert after.observation is historical
    assert after.observation.source_frame is historical.source_frame
    assert after.observation.calculation is historical.calculation
    assert after.observation.components is historical.components
    assert historical.calculation.new_capacity == D('40')
    assert current.accept(old_ticket, historical.source_frame, historical.calculation, evaluated_at=clock.utc_now()).status == 'stale'
    assert current.snapshot().snapshot_version == after.snapshot_version


def test_uncertain_buy_unusable_receipt_identity_and_replay_are_guarded():
    current, clock = execution_state()
    clock.second = 2
    evidence = buy_uncertainty(reason='invalid_receipt', venue_order_id='real-venue-ID')
    assert current.accept_buy_execution_uncertainty(evidence) == 'applied'
    after = current.snapshot()
    assert not after.orders and not after.own_fills
    assert after.new_capacity.state == after.buy_reserved.state == after.inventory_cost.state == 'unknown'
    for replay in (evidence, replace(evidence, event_id='same-request-new-event', observed_at=T + timedelta(seconds=3))):
        assert current.accept_buy_execution_uncertainty(replay) == 'stale_evidence'
        assert current.snapshot() == after
    for conflict in (replace(evidence, event_id='conflicting-request', quantity=D('11')),
            replace(evidence, request_id='different-request-same-event')):
        assert current.accept_buy_execution_uncertainty(conflict) == 'identity_error'
        assert current.snapshot() == after
    malformed = [
        {'account_id': 'foreign'}, {'process_epoch': 'foreign'}, {'cycle_generation': 'unissued'},
        *[{name: ''} for name in ('event_id', 'request_id', 'batch_id', 'observation_id', 'venue', 'condition_id', 'token_id')],
        {'condition_id': '   '}, {'round_number': True}, {'round_number': -1}, {'round_number': '0'},
        {'price': D('NaN')}, {'price': D('Infinity')}, {'price': D('0')}, {'price': D('1')}, {'price': '0.40'},
        {'quantity': D('0')}, {'quantity': D('-1')}, {'quantity': D('NaN')}, {'quantity': 10},
        {'order_type': 'gtc'}, {'expiration': True}, {'expiration': 123},
        {'order_type': 'GTD', 'expiration': None}, {'order_type': 'GTD', 'expiration': -1},
        {'order_type': 'GTD', 'expiration': '123'}, {'order_type': 'GTD', 'expiration': True}, {'post_only': 1},
        {'send_options': [('tick_size', '0.01')]}, {'send_options': (('x', 1), ('x', 2))},
        {'send_options': (('', True),)}, {'send_options': (('x', D('1')),)},
        {'send_options': (('x',),)}, {'native_started_at': None}, {'native_started_at': T.replace(tzinfo=None)},
        {'observed_at': T}, {'observed_at': 'later'}, {'reason': 'not_sent'},
        {'venue_order_id': ''}, {'venue_order_id': 1},
    ]
    for index, changes in enumerate(malformed):
        fields = dict(event_id=f'bad-event-{index}', request_id=f'bad-request-{index}')
        fields.update(changes)
        candidate = replace(evidence, **fields)
        assert current.accept_buy_execution_uncertainty(candidate) == 'identity_error', changes
        assert current.snapshot() == after, changes
    # Canonical GTD scalar expiry and all allowed option scalar types remain valid.
    valid = buy_uncertainty(event_id='valid-gtd', request_id='valid-gtd-request', order_type='GTD',
        expiration=123, send_options=(('s', 'x'), ('i', 1), ('b', True), ('n', None)))
    assert current.accept_buy_execution_uncertainty(valid) == 'applied'
    assert current.snapshot().trade_version == after.trade_version + 1

    # This records an actual post using the consumer's nonnegative GTD expiry.
    zero_state, zero_clock = execution_state()
    zero_clock.second = 2
    before_zero = zero_state.snapshot()
    assert before_zero.new_capacity.value == D('40')
    zero_expiry = buy_uncertainty(event_id='actual-gtd-zero', request_id='native-gtd-zero',
        order_type='GTD', expiration=0)
    assert zero_state.accept_buy_execution_uncertainty(zero_expiry) == 'applied'
    after_zero = zero_state.snapshot()
    for fact in (after_zero.buy_reserved, after_zero.inventory_cost,
            after_zero.current_exposure, after_zero.new_capacity):
        assert fact.state == 'unknown' and fact.value is None
    assert after_zero.trade_version == before_zero.trade_version + 1
    assert after_zero.snapshot_version == before_zero.snapshot_version + 1
    assert not after_zero.orders and not after_zero.own_fills
    assert after_zero.observation is before_zero.observation


def test_uncertain_buy_financial_reconciliation_requires_fresh_sources():
    current, clock = execution_state()
    historical = current.snapshot().observation
    clock.second = 2
    evidence = buy_uncertainty()
    assert current.accept_buy_execution_uncertainty(evidence) == 'applied'
    clock.second = 3
    cached = cash_frame(orders_at=T + timedelta(seconds=1))
    cached_calculation = calculation(cached, {}, D('40'))
    ticket = current.begin_observation('opaque-generation', tuple(cached.sources))
    assert current.accept(ticket, cached, cached_calculation, evaluated_at=clock.utc_now()).status == 'partial'
    cached_snapshot = current.snapshot()
    for fact in (cached_snapshot.buy_reserved, cached_snapshot.inventory_cost,
            cached_snapshot.current_exposure, cached_snapshot.new_capacity):
        assert fact.state == 'unknown' and fact.value is None
    assert cached_snapshot.observation.source_frame is cached
    assert cached_snapshot.observation.calculation is cached_calculation
    assert cached.sources['balance'].checked_at == T + timedelta(seconds=1)
    assert historical.calculation.new_capacity == D('40')

    # Both source time and ticket start must be strictly after the uncertainty.
    clock.second = 2
    ticket = current.begin_observation('opaque-generation', tuple(cached.sources))
    clock.second = 3
    later_sources = cash_frame(orders_at=T + timedelta(seconds=3))
    assert current.accept(ticket, later_sources, calculation(later_sources, {}, D('40')),
        evaluated_at=clock.utc_now()).status == 'partial'
    assert current.snapshot().buy_reserved.state == current.snapshot().inventory_cost.state == current.snapshot().new_capacity.state == 'unknown'

    clock.second = 4
    partial = cash_frame([order('O2', matched='3')], orders_at=clock.utc_now(),
        balance_at=T + timedelta(seconds=1))
    ticket = current.begin_observation('opaque-generation', tuple(partial.sources))
    prepared = calculation(partial, {'O2': D('0.10')}, D('37.20'))
    assert current.accept(ticket, partial, prepared, evaluated_at=clock.utc_now()).status == 'partial'
    snapshot = current.snapshot()
    o2 = ('A', 'M', 'TOKEN', 'O2')
    assert snapshot.orders[0].key == o2 and snapshot.orders[0].size_matched.value == D('3')
    assert snapshot.yields[o2].yield_pct_per_hour.value == D('0.10')
    assert snapshot.buy_reserved.value == D('2.80') and snapshot.inventory_cost.value == D('0')
    assert snapshot.current_exposure.value == D('2.80')
    assert snapshot.new_capacity.state == 'unknown' and snapshot.new_capacity.value is None
    assert snapshot.observation.source_frame is partial and snapshot.observation.calculation is prepared
    assert partial.sources['balance'].checked_at == T + timedelta(seconds=1)

    clock.second = 5
    fresh = cash_frame([order('O2', matched='3')], orders_at=clock.utc_now())
    prepared = calculation(fresh, {'O2': D('0.10')}, D('37.20'))
    ticket = current.begin_observation('opaque-generation', tuple(fresh.sources))
    assert current.accept(ticket, fresh, prepared, evaluated_at=clock.utc_now()).status == 'accepted'
    reconciled = current.snapshot()
    assert reconciled.buy_reserved.value == D('2.80') and reconciled.inventory_cost.value == D('0')
    assert reconciled.new_capacity.value == D('37.20')
    assert reconciled.observation.source_frame is fresh and reconciled.observation.calculation is prepared
    assert current.accept_buy_execution_uncertainty(replace(evidence, event_id='replayed-after-reconciliation')) == 'stale_evidence'
    assert current.snapshot() == reconciled


def test_old_generation_buy_uncertainty_and_receipt_preserve_resumed_control():
    current, clock = execution_state()
    # The real native request started under the issued original generation.
    clock.second = 2
    current.apply_effective_control(cs.EffectiveControlTransition('A', 'process',
        ('process', 1), 1, 0, True, False, 'pause'), 'opaque-generation')
    clock.second = 3
    current.apply_effective_control(cs.EffectiveControlTransition('A', 'process',
        ('process', 2), 2, 1, False, True, 'explicit_resume'), 'resumed-generation')
    source = cash_frame([order('O2', matched='0')], orders_at=clock.utc_now())
    ticket = current.begin_observation('resumed-generation', tuple(source.sources))
    assert current.accept(ticket, source, calculation(source, {'O2': None}, D('36')),
        evaluated_at=clock.utc_now()).status == 'accepted'
    before = current.snapshot()
    o2 = ('A', 'M', 'TOKEN', 'O2')
    timer = before.yields[o2]
    assert timer.unknown_since_monotonic == 103 and timer.unknown_deadline_monotonic == 703
    clock.second = 4
    evidence = buy_uncertainty(observed_at=clock.utc_now())
    assert evidence.native_started_at == T + timedelta(seconds=1)
    assert current.accept_buy_execution_uncertainty(evidence) == 'applied'
    uncertain = current.snapshot()
    assert uncertain.current == before.current
    assert uncertain.current.cycle_generation == 'resumed-generation' and uncertain.current.transition_id == ('process', 2)
    assert uncertain.yields[o2] is timer
    assert uncertain.buy_reserved.state == uncertain.inventory_cost.state == uncertain.new_capacity.state == 'unknown'
    assert uncertain.trade_version == before.trade_version + 1
    assert uncertain.snapshot_version == before.snapshot_version + 1

    clock.second = 5
    stamp = cs.SourceStamp('real-late-BUY-receipt', checked_at=clock.utc_now(), complete=True, state='known')
    def known(value):
        return cs.Fact('known', value, (stamp,))
    incoming = cs.CurrentOrderFact(KEY, 'BUY', 'LIVE', known(D('0.40')), known(D('10')),
        known(D('3')), known(D('7')), 'present', None, known('GTC'), known(None), 1, stamp)
    receipt = cs.ReceiptEvidence('real-receipt', evidence.request_id, 'A', 'process',
        evidence.cycle_generation, KEY, 'BUY', 1, order_fact=incoming)
    assert current.accept_receipt_evidence(receipt) == 'applied'
    received = current.snapshot()
    assert next(o for o in received.orders if o.key == KEY).size_matched.value == D('3')
    assert next(o for o in received.orders if o.key == KEY).source is stamp
    assert received.current == before.current and received.yields[o2] is timer
    assert received.inventory_cost.state == received.new_capacity.state == 'unknown'
    assert received.observation is before.observation
    for changes in ({'process_epoch': 'foreign'}, {'cycle_generation': 'never-issued'}):
        invalid = replace(evidence, event_id='foreign-event', request_id='foreign-request', **changes)
        assert current.accept_buy_execution_uncertainty(invalid) == 'identity_error'
        assert current.snapshot() == received
    assert current.accept_buy_execution_uncertainty(evidence) == 'stale_evidence'
    assert current.snapshot() == received

    clock.second = 6
    source = cash_frame([order(), order('O2', matched='0')], orders_at=clock.utc_now())
    ticket = current.begin_observation('resumed-generation', tuple(source.sources))
    assert current.accept(ticket, source, calculation(source, {'O1': D('0.20'), 'O2': None}, D('33.20')),
        evaluated_at=clock.utc_now()).status == 'accepted'
    final = current.snapshot()
    assert final.current == before.current
    assert final.yields[o2].unknown_since_monotonic == 103 and final.yields[o2].unknown_deadline_monotonic == 703
    assert final.buy_reserved.value == D('6.80') and final.inventory_cost.value == D('0')
    assert final.new_capacity.value == D('33.20')
