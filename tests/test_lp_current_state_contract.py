from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import pytest

import open_trader.polymarket_lp_current_state as cs
from open_trader.polymarket_lp_market_reader import CapturedSources, MarketReader, SourceResult
from open_trader.polymarket_lp_market_calculation import CalculationResult

T = datetime(2026, 10, 16, tzinfo=UTC)
D = Decimal
KEY = ('A', 'M', 'TOKEN', 'O1')
WALLET = '0x1AF9e3D4141676Af4c6858509D71471561e00562'


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


def wallet_state(owner):
    current = cs.CurrentLPState(owner, 'process', clock=Clock(), utc_now=lambda: T)
    current.apply_effective_control(cs.EffectiveControlTransition(
        owner, 'process', ('process', 0), 0, None, None, True, 'startup'), 'opaque-generation')
    return current


def wallet_frame(owner, row_account, *, with_orders=True):
    return MarketReader.from_captured_sources(CapturedSources(owner, {
        'open_orders': SourceResult('wallet-orders',
            [dict(order(), account_id=row_account)] if with_orders else [], checked_at=T),
        'positions': SourceResult('wallet-positions', [dict(account_id=row_account,
            condition_id='M', token_id='TOKEN', quantity='2', cost_usd='1.00',
            average_price='0.50', redeemable=False)], checked_at=T),
        'balance': SourceResult('wallet-balance', ('20', '20'), checked_at=T),
    }))


@pytest.mark.parametrize('owner,row_account', [
    (WALLET, WALLET.lower()), (WALLET.lower(), WALLET), (WALLET, WALLET.upper()),
])
def test_wallet_case_variants_accept_orders(owner, row_account):
    current = wallet_state(owner)
    source = wallet_frame(owner, row_account)
    # Isolate the order boundary; the position boundary has its own next case.
    source = replace(source, positions=tuple(dict(r, account_id=owner) for r in source.positions))
    accepted = observe(current, source)
    assert accepted.status == 'accepted'
    snapshot = current.snapshot()
    assert snapshot.buy_reserved.value == D('2.80')
    assert snapshot.inventory_cost.value == D('1.00')
    assert snapshot.current_exposure.value == D('3.80')
    assert snapshot.new_capacity.value == D('16.20')
    assert snapshot.orders[0].key == (owner, 'M', 'TOKEN', 'O1')
    assert snapshot.observation.account_id == owner
    assert snapshot.observation.source_frame is source
    assert source.orders[0]['account_id'] == row_account
    assert source.sources['open_orders'].value[0]['account_id'] == row_account
    assert snapshot.orders[0].source.source_id == 'wallet-orders'
    assert snapshot.orders[0].source.checked_at == T


@pytest.mark.parametrize('owner,row_account', [
    (WALLET, WALLET.lower()), (WALLET.lower(), WALLET), (WALLET, WALLET.upper()),
])
def test_wallet_case_variants_accept_positions(owner, row_account):
    current = wallet_state(owner)
    source = wallet_frame(owner, row_account, with_orders=False)
    accepted = observe(current, source, capacity=D('19'))
    assert accepted.status == 'accepted'
    snapshot = current.snapshot()
    assert snapshot.buy_reserved.value == D('0')
    assert snapshot.inventory_cost.value == D('1')
    assert snapshot.current_exposure.value == D('1')
    assert snapshot.new_capacity.value == D('19')
    assert snapshot.positions[0].account_id == owner
    assert snapshot.positions[0].quantity.value == D('2')
    assert snapshot.observation.account_id == owner
    assert snapshot.observation.source_frame is source
    assert source.positions[0]['account_id'] == row_account
    assert source.sources['positions'].value[0]['account_id'] == row_account
    assert snapshot.positions[0].source.source_id == 'wallet-positions'
    assert snapshot.positions[0].source.checked_at == T


@pytest.mark.parametrize('component', ['orders', 'positions'])
@pytest.mark.parametrize('mixed', [False, True], ids=['wrong-only', 'mixed-batch'])
def test_different_wallet_row_is_invalid_identity(component, mixed):
    current = wallet_state(WALLET)
    original = wallet_frame(WALLET, WALLET)
    assert observe(current, original).status == 'accepted'
    wrong = WALLET[:-1] + '3'  # One hex digit differs; this is a different wallet.
    rows = getattr(original, component)
    bad = dict(rows[0], account_id=wrong)
    if component == 'orders':
        bad['order_id'] = 'wrong-wallet-order'
    source = replace(original, **{component: (*rows, bad) if mixed else (bad,)})
    before = current.snapshot()
    result = observe(current, source)
    assert result.status == 'invalid_identity'
    assert result.observation is None
    assert current.snapshot() == before
    # The real reader must mark wrong-wallet input incomplete, never a valid empty account.
    name = 'open_orders' if component == 'orders' else 'positions'
    captured = dict(original.sources)
    captured[name] = replace(captured[name], value=[bad])
    filtered = MarketReader.from_captured_sources(CapturedSources(WALLET, captured))
    assert getattr(filtered, component + '_complete') is False
    assert getattr(filtered, component) == ()
    assert (filtered.buy_reserved if component == 'orders' else filtered.inventory_cost) is None


@pytest.mark.parametrize('component', ['orders', 'positions'])
@pytest.mark.parametrize('owner,row_account,expected', [
    (WALLET, None, 'invalid_identity'),
    (WALLET, WALLET.encode(), 'invalid_identity'),
    (WALLET, '', 'invalid_identity'),
    (WALLET, WALLET[:-1], 'invalid_identity'),
    (WALLET, WALLET + '0', 'invalid_identity'),
    (WALLET, WALLET.lower() + '\n', 'invalid_identity'),
    (WALLET, ' ' + WALLET.lower(), 'invalid_identity'),
    (WALLET, WALLET.lower() + ' ', 'invalid_identity'),
    ('0x' + 'f' * 40, '0x' + 'ｆ' * 40, 'invalid_identity'),
    ('0x' + 'a' * 39 + 'ſ', '0x' + 'a' * 39 + 's', 'invalid_identity'),
    ('0x' + 'A' * 39, '0x' + 'a' * 39, 'invalid_identity'),
    (' ' + WALLET, WALLET.lower(), 'invalid_identity'),
    ('Alice', 'alice', 'invalid_identity'),
    ('A', 'a', 'invalid_identity'),
    ('Alice', 'Alice', 'accepted'),
    (WALLET, 'missing', 'accepted'),
])
def test_wallet_case_rule_does_not_relax_ticket_or_opaque_identity(component, owner, row_account, expected):
    current = wallet_state(owner)
    source = wallet_frame(owner, owner)
    rows = [dict(r) for r in getattr(source, component)]
    if row_account == 'missing':
        rows[0].pop('account_id')
    else:
        rows[0]['account_id'] = row_account
    source = replace(source, **{component: tuple(rows)})
    before = current.snapshot()
    result = observe(current, source)
    assert result.status == expected
    if expected == 'invalid_identity':
        assert result.observation is None
        assert current.snapshot() == before
    else:
        assert result.observation.account_id == owner
    # Wallet equivalence is never authority to alter a frame or read ticket.
    for field, value, status in (
        ('account_id', owner.swapcase(), 'invalid_identity'),
        ('process_epoch', 'other-process', 'invalid_identity'),
        ('cycle_generation', 'other-generation', 'stale'),
        ('trade_version_at_start', 999, 'stale'),
        ('observation_id', 'unissued-read', 'invalid_identity'),
    ):
        valid = wallet_frame(owner, owner)
        ticket = current.begin_observation('opaque-generation', tuple(valid.sources))
        snapshot = current.snapshot()
        rejected = current.accept(replace(ticket, **{field: value}), valid, calculation(valid), evaluated_at=T)
        assert rejected.status == status
        assert rejected.observation is None
        assert current.snapshot() == snapshot
    valid = wallet_frame(owner, owner)
    ticket = current.begin_observation('opaque-generation', tuple(valid.sources))
    snapshot = current.snapshot()
    result = current.accept(ticket, replace(valid, account=owner.swapcase()), calculation(valid), evaluated_at=T)
    assert result.status == 'invalid_identity'
    assert result.observation is None
    assert current.snapshot() == snapshot


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
        'submit', T, T, 'hash-1', 1, T, D('0.5'), stamp, 1, ((D('.40'), D('12')),))
    guard = cs.ProtectionGuard(key, 'opaque-generation', 0, ((KEY, 1),),
        snapshot.observation.observation_id, 'baseline-event')
    assert current.apply_protection(cs.SetBaseline(guard, baseline)) == 'applied'
    protected = current.snapshot()
    assert protected.orders[0].key == KEY
    assert protected.orders[0].remaining.value == D('7')
    assert protected.orders[0].source.checked_at == T
    assert protected.baselines[key] == baseline
    changed_guard = cs.ProtectionGuard(key, 'opaque-generation', 1, ((KEY, 1),),
        snapshot.observation.observation_id, 'replace-fixed-levels')
    changed = replace(baseline, bid_level=2, bid_levels=((D('.41'), D('1')), (D('.40'), D('12'))))
    assert current.apply_protection(cs.SetBaseline(changed_guard, changed)) == 'stale'
    assert current.snapshot().baselines[key] is baseline
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


EXIT_SCOPE = ('A', 'M', 'TOKEN', 'exit-episode')


def exact_exit_attempt(request_id, order_id, *, quantity='100', sold='0', residual='100',
                       kind='protected_exit', disposition='terminal', native='ended', own=None):
    def submitted(name, value):
        stamp = cs.SourceStamp(f'{request_id}:submit:{name}', checked_at=T - timedelta(seconds=5),
            complete=True, state='known')
        return cs.Fact('known', value, (stamp,))
    return cs.ExitAttempt(request_id, kind, None if order_id is None else ('A', 'M', 'TOKEN', order_id),
        disposition, False, submitted('quantity', D(quantity)), submitted('sold', D(sold)),
        submitted('residual', D(residual)), submitted('price', D('0.40')),
        submitted('type', 'FOK' if kind == 'protected_exit' else 'GTC'), submitted('expiry', None),
        None, own, native)


def exit_guard(current, scope=EXIT_SCOPE, *, event='exit-update', observation=False):
    snapshot = current.snapshot()
    return cs.ProtectionGuard(scope, snapshot.current.cycle_generation,
        snapshot.scope_versions.get(scope, 0), tuple((o.key, o.terms_version)
            for o in snapshot.orders if o.key[:3] == scope[:3]),
        snapshot.observation.observation_id if observation and snapshot.observation else None, event)


def exit_fixture(*, disposition='terminal', native='ended'):
    current = state()
    rows = [dict(order('S1', side='SELL', matched='40'), original_size='100', status='CANCELED', order_type='FOK'),
        dict(order('S2', side='SELL', matched='0'), original_size='60', status='CANCELED', order_type='FOK'),
        dict(order('S3', side='SELL', matched='0'), original_size='60', status='CANCELED', order_type='FOK')]
    source = MarketReader.from_captured_sources(CapturedSources('A', {
        'open_orders': SourceResult('exit-orders', rows, checked_at=T),
        'positions': SourceResult('exit-positions', [dict(account_id='A', condition_id='M', token_id='TOKEN',
            quantity='60', cost_usd='24', average_price='0.40', redeemable=False)], checked_at=T),
        'balance': SourceResult('exit-balance', ('40', '40'), checked_at=T)}))
    observe(current, source, yields={}, capacity=D('16'))
    snapshot = current.snapshot()
    stamp = snapshot.orders[0].source
    known = lambda value: cs.Fact('known', value, (stamp,))
    fill = cs.OwnFillEvidence(('A', 'M', 'TOKEN', 'S1'), known(D('40')), known(D('16')),
        known(D('0.10')), ('S1-fill40',), stamp, True)
    p1 = exact_exit_attempt('P1', 'S1', own=fill, disposition=disposition, native=native)
    sell = cs.SellState(EXIT_SCOPE, known(D('60')), known(D('24')), True, snapshot.orders,
        snapshot.observation.components['open_orders'], snapshot.observation.components['positions'], None, p1)
    assert current.apply_protection(cs.ExitEvidenceUpdate(exit_guard(current, event='introduce-P1'), sell)) == 'applied'
    return current, p1


def replace_exit(current, attempt, *, scope=EXIT_SCOPE, event='replace-exit', passive=None):
    sell = current.snapshot().sells[scope]
    return current.apply_protection(cs.ExitEvidenceUpdate(exit_guard(current, scope, event=event),
        replace(sell, protected=attempt, passive=passive)))


def test_resolved_exit_replacement_retains_exact_submit_facts():
    import pytest
    current, p1 = exit_fixture()
    before = current.snapshot()
    p2 = exact_exit_attempt('P2', 'S2', quantity='60', sold='40', residual='60')
    assert replace_exit(current, p2, event='P1-to-P2') == 'applied'
    replaced = current.snapshot()
    assert replaced.sells[EXIT_SCOPE].protected == p2
    retained = replaced.resolved_exit_attempts[(EXIT_SCOPE, 'P1')]
    assert retained == p1
    for name in ('submit_quantity', 'submit_sold_quantity', 'submit_residual_quantity', 'price', 'order_type', 'expiration'):
        assert getattr(retained, name) is getattr(p1, name)
        assert getattr(retained, name).sources[0].source_id.startswith('P1:submit:')
    assert retained.submit_quantity.value == D('100') and retained.submit_sold_quantity.value == D('0')
    assert retained.submit_residual_quantity.value == D('100')
    assert replaced.sells[EXIT_SCOPE].residual_quantity.value == D('60')
    assert replaced.orders == before.orders and replaced.own_fills == before.own_fills
    assert (EXIT_SCOPE, 'P2') not in replaced.resolved_exit_attempts
    assert not before.resolved_exit_attempts
    with pytest.raises(TypeError):
        replaced.resolved_exit_attempts[(EXIT_SCOPE, 'fake')] = p1
    p3 = exact_exit_attempt('P3', 'S3', quantity='60', sold='40', residual='60')
    assert replace_exit(current, p3, event='P2-to-P3') == 'applied'
    assert current.snapshot().resolved_exit_attempts == {(EXIT_SCOPE, 'P1'): p1, (EXIT_SCOPE, 'P2'): p2}
    assert replace_exit(current, None, event='remove-P3') == 'applied'
    final = current.snapshot()
    assert final.resolved_exit_attempts == {(EXIT_SCOPE, 'P1'): p1, (EXIT_SCOPE, 'P2'): p2, (EXIT_SCOPE, 'P3'): p3}
    assert final.sells[EXIT_SCOPE].protected is None
    assert list(replaced.resolved_exit_attempts) == [(EXIT_SCOPE, 'P1')]
    assert final.sells[EXIT_SCOPE].residual_quantity == before.sells[EXIT_SCOPE].residual_quantity
    assert final.orders == before.orders and final.own_fills == before.own_fills


def test_resolved_exit_replacement_rejects_unresolved_and_request_reuse():
    p2 = exact_exit_attempt('P2', 'S2', quantity='60', sold='40', residual='60')
    for disposition, native in [('terminal', 'running'), ('unknown', 'ended'),
            ('accepted_without_order_id', 'ended'), ('accepted', 'ended')]:
        current, p1 = exit_fixture(disposition=disposition, native=native)
        before = current.snapshot()
        for replacement in (p2, None):
            assert replace_exit(current, replacement, event='unresolved-replacement') == 'stale'
            assert current.snapshot() == before
            assert not current.snapshot().resolved_exit_attempts
    current, p1 = exit_fixture()
    assert replace_exit(current, p2, event='resolved-replacement') == 'applied'
    before = current.snapshot()
    assert replace_exit(current, p1, event='replay-P1') in ('stale', 'invalid_identity')
    assert current.snapshot() == before
    # Moving a retired request to another kind or episode is not a new send.
    moved = replace(p1, kind='passive_exit')
    assert replace_exit(current, p2, passive=moved, event='move-P1-kind') in ('stale', 'invalid_identity')
    assert current.snapshot() == before
    other_scope = ('A', 'M', 'TOKEN', 'another-episode')
    assert current.apply_protection(cs.ExitEvidenceUpdate(exit_guard(current, other_scope, event='rebind-P1'),
        replace(before.sells[EXIT_SCOPE], scope=other_scope, protected=p1))) in ('stale', 'invalid_identity')
    assert current.snapshot() == before

    q1 = exact_exit_attempt('Q1', 'S3', quantity='60', sold='40', residual='60', kind='passive_exit')
    assert replace_exit(current, p2, passive=q1, event='introduce-passive') == 'applied'
    before = current.snapshot()
    q2 = exact_exit_attempt('Q2', 'S3', quantity='60', sold='40', residual='60', kind='passive_exit')
    invalid = exact_exit_attempt('invalid-P3', 'S3', quantity='61', sold='40', residual='60')
    assert replace_exit(current, invalid, passive=q2, event='two-slot-atomic') == 'invalid_identity'
    assert current.snapshot() == before
    assert (EXIT_SCOPE, 'Q1') not in current.snapshot().resolved_exit_attempts
    # A still-current request is also bound to its first exact scope/kind.
    assert current.apply_protection(cs.ExitEvidenceUpdate(exit_guard(current, other_scope, event='rebind-current-P2'),
        replace(before.sells[EXIT_SCOPE], scope=other_scope))) in ('stale', 'invalid_identity')
    assert current.snapshot() == before


def test_late_resolved_exit_receipt_updates_exact_old_attempt_only():
    current, p1 = exit_fixture()
    p2 = exact_exit_attempt('P2', 'S2', quantity='60', sold='40', residual='60')
    assert replace_exit(current, p2, event='archive-P1') == 'applied'
    before = current.snapshot()
    old_map = before.resolved_exit_attempts
    stamp = cs.SourceStamp('late-P1-fill', checked_at=T + timedelta(seconds=1), complete=True, state='known')
    known = lambda value: cs.Fact('known', value, (stamp,))
    fill = replace(p1.own_fill_evidence, quantity=known(D('45')), notional_usd=known(D('18')),
        fees_usd=known(D('0.11')), fill_ids=('S1-fill40', 'S1-fill5'), source=stamp)
    proof = cs.TerminalProof(p1.order_key, 'CANCELED', before.orders[0].original_size, known(D('45')), stamp, True)
    late = replace(p1, own_fill_evidence=fill)
    receipt = cs.ReceiptEvidence('late-P1-explicit', 'P1', 'A', 'process', 'old-issued-generation',
        p1.order_key, 'SELL', 1, terminal_proof=proof, own_fill=fill, exit_attempt=late, scope=EXIT_SCOPE)
    assert current.accept_receipt_evidence(receipt) == 'applied'
    after = current.snapshot()
    assert after.resolved_exit_attempts[(EXIT_SCOPE, 'P1')] == late
    assert after.own_fills[p1.order_key] == fill
    assert after.sells[EXIT_SCOPE] == before.sells[EXIT_SCOPE]
    assert after.sells[EXIT_SCOPE].protected == p2
    assert after.orders[0].terminal_proof == proof
    assert after.current == before.current
    assert after.trade_version == before.trade_version + 1 and after.snapshot_version == before.snapshot_version + 1
    assert after.new_capacity.state == 'unknown'
    assert old_map[(EXIT_SCOPE, 'P1')] == p1
    newer_stamp = replace(stamp, source_id='late-P1-fees', checked_at=T + timedelta(seconds=2))
    final_fill = replace(fill, fees_usd=cs.Fact('known', D('0.12'), (newer_stamp,)), source=newer_stamp)
    own_only = replace(receipt, event_id='late-P1-own-only', exit_attempt=None, terminal_proof=None, own_fill=final_fill)
    assert current.accept_receipt_evidence(own_only) == 'applied'
    final = current.snapshot()
    final_attempt = final.resolved_exit_attempts[(EXIT_SCOPE, 'P1')]
    assert final_attempt.own_fill_evidence == final.own_fills[p1.order_key] == final_fill
    assert final.sells[EXIT_SCOPE] == before.sells[EXIT_SCOPE]
    assert final_attempt.submit_sold_quantity is p1.submit_sold_quantity
    assert final_attempt.submit_residual_quantity is p1.submit_residual_quantity
    assert current.accept_receipt_evidence(own_only) == 'stale_evidence'
    assert current.snapshot() == final
    invalid_attempts = [replace(final_attempt, native_state='running'),
        replace(final_attempt, submit_sold_quantity=known(D('1'))),
        replace(final_attempt, submit_residual_quantity=known(D('60'))),
        replace(final_attempt, submit_quantity=known(D('100'))),
        replace(final_attempt, own_fill_evidence=replace(final_fill, fill_ids=('S1-fill5',)))]
    for index, attempt in enumerate(invalid_attempts):
        invalid = replace(receipt, event_id=f'invalid-retained-{index}', terminal_proof=None,
            own_fill=None, exit_attempt=attempt)
        assert current.accept_receipt_evidence(invalid) in ('identity_error', 'stale_evidence')
        assert current.snapshot() == final
    for changes in ({'scope': ('A', 'M', 'TOKEN', 'wrong')}, {'scope': None},
            {'request_id': 'wrong-request'}, {'order_key': ('A', 'M', 'TOKEN', 'S2')},
            {'own_fill': replace(final_fill, fees_usd=known(D('9')))}):
        fields = dict(event_id='conflicting-retained', terminal_proof=None, own_fill=final_fill, exit_attempt=final_attempt)
        fields.update(changes)
        assert current.accept_receipt_evidence(replace(receipt, **fields)) in ('identity_error', 'stale_evidence')
        assert current.snapshot() == final
    wrong_scope_own = replace(own_only, event_id='own-only-wrong-scope', scope=('A', 'M', 'TOKEN', 'wrong'))
    assert current.accept_receipt_evidence(wrong_scope_own) == 'identity_error'
    assert current.snapshot() == final
    # A different event with unchanged exact evidence keeps existing receipt semantics.
    assert current.accept_receipt_evidence(replace(own_only, event_id='new-event-same-proof')) == 'applied'
    assert current.snapshot().sells[EXIT_SCOPE] == before.sells[EXIT_SCOPE]


def test_resolved_no_id_local_rejection_retains_truth_without_venue_fact():
    current = state()
    observe(current, frame([]), yields={})
    snapshot = current.snapshot()
    stamp = snapshot.observation.components['positions']
    known = lambda value: cs.Fact('known', value, (stamp,))
    local = exact_exit_attempt('not-sent-local', None, quantity='2', sold='0', residual='2', disposition='rejected')
    sell = cs.SellState(EXIT_SCOPE, known(D('2')), known(D('1')), True, (),
        snapshot.observation.components['open_orders'], stamp, None, local)
    assert current.apply_protection(cs.ExitEvidenceUpdate(exit_guard(current, event='local-rejected'), sell)) == 'applied'
    before = current.snapshot()
    next_attempt = exact_exit_attempt('fresh-after-local', None, quantity='2', sold='0', residual='2',
        disposition='unknown', native='running')
    assert replace_exit(current, next_attempt, event='replace-local-rejection') == 'applied'
    retained = current.snapshot()
    assert retained.resolved_exit_attempts[(EXIT_SCOPE, local.request_id)] == local
    assert retained.resolved_exit_attempts[(EXIT_SCOPE, local.request_id)].submit_quantity is local.submit_quantity
    assert local.order_key is None and local.own_fill_evidence is None
    assert retained.orders == before.orders == () and not retained.own_fills
    assert replace_exit(current, None, event='remove-actual-unknown') == 'stale'
    assert replace_exit(current, replace(local, request_id='another-local'), event='replace-actual-unknown') == 'stale'
    assert current.snapshot() == retained
    fake_key = ('A', 'M', 'TOKEN', 'invented-venue-ID')
    fake_order = cs.CurrentOrderFact(fake_key, 'SELL', 'LIVE', known(D('0.40')), known(D('2')),
        known(D('0')), known(D('2')), 'present', None, known('FOK'), known(None), 1, stamp)
    receipt = cs.ReceiptEvidence('fabricated-local-order', local.request_id, 'A', 'process', 'opaque-generation',
        fake_key, 'SELL', 1, order_fact=fake_order, exit_attempt=replace(local, order_key=fake_key), scope=EXIT_SCOPE)
    assert current.accept_receipt_evidence(receipt) == 'identity_error'
    assert current.snapshot() == retained
    # Omitting rich attempt context cannot smuggle an order onto the known NOT_SENT request.
    assert current.accept_receipt_evidence(replace(receipt, event_id='fabricated-order-only', exit_attempt=None)) == 'identity_error'
    assert current.snapshot() == retained


def completed_exit_fixture(*, phase='closed', p2_disposition='terminal', p2_native='ended',
                           entry_unknown=False, position='0', complete_entry_fill=True,
                           current_local_rejection=False, p2_order_id='S2', p2_price=None):
    current, p1 = exit_fixture()
    p2 = exact_exit_attempt('P2', None if current_local_rejection else p2_order_id, quantity='60', sold='40', residual='60',
        disposition='rejected' if current_local_rejection else p2_disposition, native=p2_native)
    if p2_price is not None:
        p2 = replace(p2, price=replace(p2.price, value=D(p2_price)))
    assert replace_exit(current, p2, event='archive-for-completion') == 'applied'
    entry_key = ('A', 'M', 'TOKEN', 'ENTRY')
    rows = [dict(order('S1', side='SELL', matched='40'), original_size='100', status='CANCELED', order_type='FOK'),
        dict(order('S2', side='SELL', matched='0'), original_size='60', status='CANCELED', order_type='FOK'),
        dict(order('S3', side='SELL', matched='0'), original_size='60', status='CANCELED', order_type='FOK')]
    entry_row = dict(order('ENTRY', matched='100'), original_size='100', status='LIVE' if entry_unknown else 'FILLED')
    def final_source(second, include_entry=True):
        return MarketReader.from_captured_sources(CapturedSources('A', {
            'open_orders': SourceResult('completion-orders', rows + ([entry_row] if include_entry else []),
                checked_at=T + timedelta(seconds=second)),
            'positions': SourceResult('completion-positions', [] if position == '0' else [dict(account_id='A',
                condition_id='M', token_id='TOKEN', quantity=position, cost_usd='0.40', average_price='0.40', redeemable=False)],
                checked_at=T + timedelta(seconds=second)),
            'balance': SourceResult('completion-balance', ('40', '40'), checked_at=T + timedelta(seconds=second))}))
    observe(current, final_source(10), yields={}, capacity=D('40'))
    stamp = current.snapshot().observation.components['open_orders']
    known = lambda value: cs.Fact('known', value, (stamp,))
    entry_fill = cs.OwnFillEvidence(entry_key, known(D('100')), known(D('40')), known(D('0.10')),
        ('entry-fill100',), stamp, complete_entry_fill)
    unknown_proof = None
    if entry_unknown:
        entry = next(o for o in current.snapshot().orders if o.key == entry_key)
        unknown_proof = cs.TerminalProof(entry_key, 'CANCELED', entry.original_size,
            cs.Fact('unknown', sources=(stamp,)), stamp, True)
    assert current.accept_receipt_evidence(cs.ReceiptEvidence('entry-complete-fill', 'entry-request', 'A', 'process',
        'opaque-generation', entry_key, 'BUY', 1, own_fill=entry_fill, terminal_proof=unknown_proof)) == 'applied'
    observe(current, final_source(11, include_entry=not entry_unknown), yields={}, capacity=D('40'))
    snapshot = current.snapshot()
    orders_source, positions_source = snapshot.observation.components['open_orders'], snapshot.observation.components['positions']
    sell = replace(snapshot.sells[EXIT_SCOPE], residual_quantity=known(D('0')), inventory_cost_usd=known(D('0')),
        position_reconciled=True, current_sells=tuple(o for o in snapshot.orders if o.side == 'SELL'),
        orders_source=orders_source, positions_source=positions_source)
    assert current.apply_protection(cs.ExitEvidenceUpdate(exit_guard(current, event='complete-sell-projection', observation=True), sell)) == 'applied'
    economics = cs.OpeningEconomics(EXIT_SCOPE, known(D('40')), known(D('16')), known(D('0.20')), known(D('0')), True)
    scoring = cs.ScoringState(None, None, cs.Fact('unknown'), None, None, None, orders_source)
    guard = exit_guard(current, event='complete-opening-projection', observation=True)
    opening = cs.OpeningProtection(EXIT_SCOPE, phase, None, ('ENTRY',), known(D('100')), economics,
        known(False), None, cs.Fact('unknown'), cs.Fact('unknown'), 'none', False, scoring, guard.expected_scope_version + 1)
    assert current.apply_protection(cs.OpeningProtectionUpdate(guard, opening)) == 'applied'
    return current, p1, p2


def test_exit_attempt_retirement_requires_closed_reconciled_scope():
    current, p1 = exit_fixture()
    p2 = exact_exit_attempt('P2', 'S2', quantity='60', sold='40', residual='60')
    assert replace_exit(current, p2, event='archive-before-retire') == 'applied'
    before = current.snapshot()
    assert current.retire_exit_attempts(exit_guard(current, event='no-closed-opening', observation=True)) == 'stale'
    assert current.snapshot() == before
    for options in ({'phase': 'protected_exit'}, {'p2_disposition': 'unknown'}, {'p2_disposition': 'accepted'},
            {'p2_native': 'running'}, {'entry_unknown': True}, {'position': '1'}, {'complete_entry_fill': False},
            {'p2_order_id': 'ENTRY'}, {'p2_price': '0.50'}):
        candidate, _, _ = completed_exit_fixture(**options)
        snapshot = candidate.snapshot()
        assert candidate.retire_exit_attempts(exit_guard(candidate, event='unproven-retire', observation=True)) == 'stale', options
        assert candidate.snapshot() == snapshot, options

    contradictory, _, p2 = completed_exit_fixture()
    snapshot = contradictory.snapshot()
    stamp = snapshot.observation.components['open_orders']
    known = lambda value: cs.Fact('known', value, (stamp,))
    conflicting_fill = cs.OwnFillEvidence(p2.order_key, known(D('1')), known(D('0.40')), known(D('0.01')),
        ('actual-S2-fill1',), stamp, True)
    assert contradictory.accept_receipt_evidence(cs.ReceiptEvidence('contradict-terminal-zero', p2.request_id,
        'A', 'process', 'opaque-generation', p2.order_key, 'SELL', 1, own_fill=conflicting_fill, scope=EXIT_SCOPE)) == 'applied'
    assert observe(contradictory, snapshot.observation.source_frame, yields={}, capacity=D('40')).status == 'accepted'
    before_conflict = contradictory.snapshot()
    assert contradictory.retire_exit_attempts(exit_guard(contradictory, event='refuse-conflicting-zero', observation=True)) == 'stale'
    assert contradictory.snapshot() == before_conflict

    current, p1, p2 = completed_exit_fixture()
    base = current.snapshot()
    sell = base.sells[EXIT_SCOPE]
    bad_source = replace(sell.orders_source, complete=False)
    sell_variants = [replace(sell, residual_quantity=cs.Fact('unknown')),
        replace(sell, residual_quantity=cs.Fact('known', D('1'))), replace(sell, position_reconciled=False),
        replace(sell, orders_source=bad_source), replace(sell, positions_source=replace(sell.positions_source, checked_at=None)),
        replace(sell, orders_source=replace(sell.orders_source, source_id='unaccepted-order-source'))]
    for index, variant in enumerate(sell_variants):
        assert current.apply_protection(cs.ExitEvidenceUpdate(exit_guard(current, event=f'bad-completion-{index}'), variant)) == 'applied'
        snapshot = current.snapshot()
        assert current.retire_exit_attempts(exit_guard(current, event=f'refuse-completion-{index}', observation=True)) == 'stale'
        assert current.snapshot() == snapshot
        assert current.apply_protection(cs.ExitEvidenceUpdate(exit_guard(current, event=f'restore-completion-{index}'), sell)) == 'applied'
    opening = current.snapshot().openings[EXIT_SCOPE]
    economics_variants = [replace(opening.economics, position_reconciled=False)] + [
        replace(opening.economics, **{name: cs.Fact('unknown')}) for name in
        ('buy_cost_usd', 'sold_revenue_usd', 'fees_usd', 'residual_exit_value_usd')]
    for index, economics in enumerate(economics_variants):
        guard = exit_guard(current, event=f'bad-economics-{index}')
        assert current.apply_protection(cs.OpeningProtectionUpdate(guard,
            replace(opening, economics=economics, scope_version=guard.expected_scope_version + 1))) == 'applied'
        snapshot = current.snapshot()
        assert current.retire_exit_attempts(exit_guard(current, event=f'refuse-economics-{index}', observation=True)) == 'stale'
        assert current.snapshot() == snapshot
    guard = exit_guard(current, event='missing-entry')
    assert current.apply_protection(cs.OpeningProtectionUpdate(guard,
        replace(opening, entry_order_ids=('missing-entry-ID',), scope_version=guard.expected_scope_version + 1))) == 'applied'
    snapshot = current.snapshot()
    assert current.retire_exit_attempts(exit_guard(current, event='refuse-missing-entry', observation=True)) == 'stale'
    assert current.snapshot() == snapshot
    guard = exit_guard(current, event='restore-closed-opening')
    assert current.apply_protection(cs.OpeningProtectionUpdate(guard,
        replace(opening, scope_version=guard.expected_scope_version + 1))) == 'applied'
    valid_guard = exit_guard(current, event='retire-fully-discharged', observation=True)
    for guard, status in [(replace(valid_guard, expected_scope_version=0), 'stale'),
            (replace(valid_guard, cycle_generation='old-generation'), 'stale'),
            (replace(valid_guard, observation_id='wrong-observation'), 'stale'),
            (replace(valid_guard, observation_id=None), 'stale'),
            (replace(valid_guard, scope=('wrong-account', 'M', 'TOKEN', 'episode')), 'invalid_identity'),
            (replace(valid_guard, scope=cs.ProtectionKey('queue_bucket', EXIT_SCOPE, D('0.40'), None)), 'invalid_identity')]:
        snapshot = current.snapshot()
        assert current.retire_exit_attempts(guard) == status
        assert current.snapshot() == snapshot
    before = current.snapshot()
    captured = before.resolved_exit_attempts
    assert current.retire_exit_attempts(valid_guard) == 'applied'
    after = current.snapshot()
    assert not after.resolved_exit_attempts
    assert after.sells[EXIT_SCOPE] == replace(before.sells[EXIT_SCOPE], passive=None, protected=None)
    assert after.orders == before.orders and after.own_fills == before.own_fills and after.openings == before.openings
    assert after.current == before.current and after.trade_version == before.trade_version
    assert after.buy_reserved == before.buy_reserved and after.inventory_cost == before.inventory_cost and after.new_capacity == before.new_capacity
    assert after.scope_versions[EXIT_SCOPE] == before.scope_versions[EXIT_SCOPE] + 1
    assert after.snapshot_version == before.snapshot_version + 1
    assert captured[(EXIT_SCOPE, 'P1')] == p1
    assert current.retire_exit_attempts(exit_guard(current, event='duplicate-retire', observation=True)) == 'stale'
    assert current.snapshot() == after
    # A completed local rejection needs no invented order/fill to be discharged.
    local, _, p2 = completed_exit_fixture(current_local_rejection=True)
    assert p2.order_key is None and p2.own_fill_evidence is None
    assert local.retire_exit_attempts(exit_guard(local, event='retire-local-rejection', observation=True)) == 'applied'


def test_retired_exit_scope_rejects_recreation_and_keeps_independent_facts():
    import pytest
    current, p1, p2 = completed_exit_fixture()
    opening = current.snapshot().openings[EXIT_SCOPE]
    guard = exit_guard(current, event='latch-old-episode')
    assert current.apply_protection(cs.OpeningProtectionUpdate(guard, replace(opening,
        stop_loss_latched=cs.Fact('known', True), stop_loss_triggered_at=T,
        stop_loss_triggered_loss_usd=cs.Fact('known', D('24')), scope_version=guard.expected_scope_version + 1))) == 'applied'
    captured = current.snapshot().resolved_exit_attempts
    assert current.retire_exit_attempts(exit_guard(current, event='retire-old-episode', observation=True)) == 'applied'
    retired = current.snapshot()
    stamp = cs.SourceStamp('late-retired-proof', checked_at=T + timedelta(seconds=12), complete=True, state='known')
    known = lambda value: cs.Fact('known', value, (stamp,))
    fill = replace(p1.own_fill_evidence, quantity=known(D('45')), notional_usd=known(D('18')),
        fees_usd=known(D('0.12')), fill_ids=('S1-fill40', 'S1-fill5'), source=stamp)
    proof = cs.TerminalProof(p1.order_key, 'CANCELED', retired.orders[0].original_size, known(D('45')), stamp, True)
    late = cs.ReceiptEvidence('late-retired-explicit', 'P1', 'A', 'process', 'old-generation', p1.order_key,
        'SELL', 1, terminal_proof=proof, own_fill=fill, exit_attempt=replace(p1, own_fill_evidence=fill), scope=EXIT_SCOPE)
    assert current.accept_receipt_evidence(late) == 'stale_evidence'
    assert current.snapshot() == retired
    unknown = replace(late, event_id='never-registered-attempt', request_id='never-registered',
        exit_attempt=replace(p1, request_id='never-registered', own_fill_evidence=fill))
    assert current.accept_receipt_evidence(unknown) == 'identity_error'
    assert current.snapshot() == retired
    assert replace_exit(current, p2, event='recreate-retired-slot') == 'stale'
    guard = exit_guard(current, event='reopen-retired')
    assert current.apply_protection(cs.OpeningProtectionUpdate(guard, replace(opening,
        phase='opening', scope_version=guard.expected_scope_version + 1))) == 'stale'
    assert current.snapshot() == retired
    new_scope = ('A', 'M', 'TOKEN', 'genuine-next-episode')
    reused_sell = replace(retired.sells[EXIT_SCOPE], scope=new_scope, protected=p1)
    assert current.apply_protection(cs.ExitEvidenceUpdate(exit_guard(current, new_scope, event='old-request-new-episode'),
        reused_sell)) == 'stale'
    assert current.snapshot() == retired

    # Dropping rich context does not block independent factual improvements.
    independent = replace(late, event_id='independent-late-facts', exit_attempt=None)
    assert current.accept_receipt_evidence(independent) == 'applied'
    factual = current.snapshot()
    assert not factual.resolved_exit_attempts
    assert factual.sells == retired.sells and factual.openings == retired.openings and factual.current == retired.current
    assert factual.orders[0].terminal_proof == proof and factual.own_fills[p1.order_key] == fill
    assert factual.new_capacity.state == 'unknown'
    assert factual.trade_version == retired.trade_version + 1
    fee_fill = replace(fill, fees_usd=known(D('0.13')))
    assert current.accept_receipt_evidence(replace(independent, event_id='independent-own-fill',
        terminal_proof=None, own_fill=fee_fill)) == 'applied'
    factual = current.snapshot()
    assert factual.own_fills[p1.order_key] == fee_fill and not factual.resolved_exit_attempts
    assert factual.sells == retired.sells
    # An independent order proof can also progress without any submit-context reconstruction.
    later_proof = replace(proof, source=replace(stamp, checked_at=T + timedelta(seconds=13)))
    assert current.accept_receipt_evidence(replace(independent, event_id='independent-order-proof',
        terminal_proof=later_proof, own_fill=None)) == 'applied'
    assert current.snapshot().sells == retired.sells and not current.snapshot().resolved_exit_attempts
    assert captured[(EXIT_SCOPE, 'P1')] == p1
    with pytest.raises(TypeError):
        captured[(EXIT_SCOPE, 'P2')] = p2

    # A distinct episode/request has independent current facts and retention.
    rows = [dict(row) for row in current.snapshot().observation.source_frame.orders]
    rows = [dict(row, size_matched=D('45')) if row['order_id'] == 'S1' else row for row in rows]
    rows.append(dict(order('NEXT-ENTRY', matched='2'), original_size='2', status='FILLED'))
    next_source = MarketReader.from_captured_sources(CapturedSources('A', {
        'open_orders': SourceResult('next-episode-orders', rows, checked_at=T + timedelta(seconds=14)),
        'positions': SourceResult('next-episode-positions', [dict(account_id='A', condition_id='M', token_id='TOKEN',
            quantity='2', cost_usd='0.80', average_price='0.40', redeemable=False)], checked_at=T + timedelta(seconds=14)),
        'balance': SourceResult('next-episode-balance', ('40', '40'), checked_at=T + timedelta(seconds=14))}))
    assert observe(current, next_source, yields={}, capacity=D('39.20')).status == 'accepted'
    latest = current.snapshot()
    next_known = lambda value: cs.Fact('known', value, (latest.observation.components['positions'],))
    next_sell = cs.SellState(new_scope, next_known(D('2')), next_known(D('0.80')), True, (),
        latest.observation.components['open_orders'], latest.observation.components['positions'], None,
        exact_exit_attempt('new-local', None, quantity='2', residual='2', disposition='rejected'))
    assert current.apply_protection(cs.ExitEvidenceUpdate(exit_guard(current, new_scope, event='new-episode-local'), next_sell)) == 'applied'
    new_active = exact_exit_attempt('new-actual-send', None, quantity='2', residual='2', disposition='unknown', native='running')
    assert replace_exit(current, new_active, scope=new_scope, event='new-episode-send') == 'applied'
    new_opening_guard = exit_guard(current, new_scope, event='new-opening')
    new_opening = replace(opening, scope=new_scope, phase='opening', entry_order_ids=('NEXT-ENTRY',),
        buy_filled_quantity=next_known(D('2')),
        economics=cs.OpeningEconomics(new_scope, next_known(D('0.80')), next_known(D('0')),
            next_known(D('0')), next_known(D('0.80')), True), stop_loss_latched=cs.Fact('known', False),
        stop_loss_triggered_at=None, stop_loss_triggered_loss_usd=cs.Fact('unknown'),
        scope_version=new_opening_guard.expected_scope_version + 1)
    assert current.apply_protection(cs.OpeningProtectionUpdate(new_opening_guard, new_opening)) == 'applied'
    before_control = current.snapshot()
    assert before_control.openings[new_scope].stop_loss_latched.value is False
    assert before_control.openings[EXIT_SCOPE].stop_loss_latched.value is True
    retained_new = before_control.resolved_exit_attempts[(new_scope, 'new-local')]
    current.apply_effective_control(cs.EffectiveControlTransition('A', 'process', ('process', 1),
        1, 0, True, False, 'pause'), 'opaque-generation')
    current.apply_effective_control(cs.EffectiveControlTransition('A', 'process', ('process', 2),
        2, 1, False, True, 'explicit_resume'), 'new-generation')
    resumed = current.snapshot()
    assert resumed.resolved_exit_attempts[(new_scope, 'new-local')] == retained_new
    assert (EXIT_SCOPE, 'P1') not in resumed.resolved_exit_attempts
    assert resumed.sells[new_scope] == before_control.sells[new_scope]
    assert replace_exit(current, p1, event='retired-after-resume') == 'stale'
    assert current.accept_receipt_evidence(replace(late, event_id='retired-receipt-after-resume')) == 'stale_evidence'
    # A still-current real no-ID send can later return its genuine exact order.
    returned_key = ('A', 'M', 'TOKEN', 'new-real-order')
    returned_order = cs.CurrentOrderFact(returned_key, 'SELL', 'LIVE', known(D('0.40')), known(D('2')),
        known(D('1')), known(D('1')), 'present', None, known('FOK'), known(None), 1, stamp)
    returned_attempt = replace(new_active, order_key=returned_key, state='accepted', native_state='ended')
    returned_receipt = cs.ReceiptEvidence('late-new-real-ID', new_active.request_id, 'A', 'process',
        'opaque-generation', returned_key, 'SELL', 1, order_fact=returned_order,
        exit_attempt=returned_attempt, scope=new_scope)
    assert current.accept_receipt_evidence(returned_receipt) == 'applied'
    received = current.snapshot()
    assert received.sells[new_scope].protected == returned_attempt
    assert received.current == resumed.current
    assert received.resolved_exit_attempts[(new_scope, 'new-local')] == retained_new
    assert (EXIT_SCOPE, 'P1') not in received.resolved_exit_attempts
    fresh_process = cs.CurrentLPState('A', 'another-process')
    fresh = fresh_process.snapshot()
    assert not fresh.resolved_exit_attempts and not fresh.sells and not fresh.openings and not fresh.own_fills


    # Retirement must preserve the known NOT_SENT and exact-order exclusions.
    excluded, real_p1, local_p2 = completed_exit_fixture(current_local_rejection=True)
    before_local = excluded.snapshot()
    assert local_p2.order_key is None and local_p2.state == 'rejected' and local_p2.native_state == 'ended'
    original_orders, original_fills = before_local.orders, before_local.own_fills
    local_stamp = before_local.observation.components['open_orders']
    local_known = lambda value: cs.Fact('known', value, (local_stamp,))
    false_key = ('A', 'M', 'TOKEN', 'false-NOT-SENT-venue-ID')
    false_order = cs.CurrentOrderFact(false_key, 'SELL', 'LIVE', local_known(D('0.40')), local_known(D('60')),
        local_known(D('0')), local_known(D('60')), 'present', None, local_known('FOK'), local_known(None), 1, local_stamp)
    false_receipt = cs.ReceiptEvidence('false-before-retire', local_p2.request_id, 'A', 'process',
        'opaque-generation', false_key, 'SELL', 1, order_fact=false_order, scope=EXIT_SCOPE)
    assert excluded.accept_receipt_evidence(false_receipt) == 'identity_error'
    assert excluded.snapshot() == before_local
    assert excluded.retire_exit_attempts(exit_guard(excluded, event='retire-local-exclusions', observation=True)) == 'applied'
    after_local = excluded.snapshot()
    assert excluded.accept_receipt_evidence(replace(false_receipt, event_id='false-after-retire')) == 'identity_error'
    assert excluded.snapshot() == after_local
    real_fill = after_local.own_fills[real_p1.order_key]
    false_fill_receipt = replace(false_receipt, event_id='false-fill-after-retire', order_key=real_p1.order_key,
        order_fact=None, own_fill=real_fill)
    for candidate in (false_fill_receipt,
            replace(false_receipt, event_id='false-order-wrong-scope', scope=('A', 'M', 'TOKEN', 'wrong-scope')),
            replace(false_fill_receipt, event_id='false-fill-wrong-scope', scope=('A', 'M', 'TOKEN', 'wrong-scope')),
            replace(false_fill_receipt, event_id='false-fill-no-scope', scope=None)):
        assert excluded.accept_receipt_evidence(candidate) == 'identity_error'
        assert excluded.snapshot() == after_local
    exact_real = replace(false_fill_receipt, event_id='real-retired-exact-fill', request_id=real_p1.request_id)
    other_key = ('A', 'M', 'TOKEN', 'S2')
    other_fill = cs.OwnFillEvidence(other_key, local_known(D('0')), local_known(D('0')), local_known(D('0')),
        ('other-order-zero',), local_stamp, True)
    for candidate in (replace(exact_real, event_id='real-retired-wrong-scope', scope=('A', 'M', 'TOKEN', 'wrong-scope')),
            replace(exact_real, event_id='real-retired-no-scope', scope=None),
            replace(exact_real, event_id='real-retired-wrong-order', order_key=other_key, own_fill=other_fill),
            replace(false_receipt, event_id='real-retired-false-order', request_id=real_p1.request_id),
            replace(exact_real, event_id='real-retired-wrong-side', side='BUY')):
        assert excluded.accept_receipt_evidence(candidate) == 'identity_error'
        assert excluded.snapshot() == after_local
    assert excluded.accept_receipt_evidence(exact_real) == 'applied'
    improved = excluded.snapshot()
    assert improved.orders == original_orders and improved.own_fills == original_fills
    assert improved.sells == after_local.sells and improved.openings == after_local.openings
    assert not improved.resolved_exit_attempts
    exact_proof = next(o.terminal_proof for o in improved.orders if o.key == real_p1.order_key)
    assert excluded.accept_receipt_evidence(replace(exact_real, event_id='real-retired-exact-order-proof',
        own_fill=None, terminal_proof=exact_proof)) == 'applied'
    assert not excluded.snapshot().resolved_exit_attempts
    assert excluded.snapshot().sells == after_local.sells
    excluded.apply_effective_control(cs.EffectiveControlTransition('A', 'process', ('process', 1),
        1, 0, True, False, 'pause'), 'opaque-generation')
    excluded.apply_effective_control(cs.EffectiveControlTransition('A', 'process', ('process', 2),
        2, 1, False, True, 'explicit_resume'), 'resumed-exclusion-generation')
    after_resume = excluded.snapshot()
    assert excluded.accept_receipt_evidence(replace(false_receipt, event_id='false-after-resume')) == 'identity_error'
    assert excluded.snapshot() == after_resume
