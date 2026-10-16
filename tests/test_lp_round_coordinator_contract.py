"""Source-independent examples for the five public round-coordinator methods."""
from dataclasses import dataclass, replace
import copy
import gc
import weakref
from datetime import UTC, datetime
from decimal import Decimal

import pytest


@dataclass(frozen=True)
class Startup:
    account_id: str = 'acct-1'
    transition_id: str = 'startup-1'
    control_version: int = 1
    effective_running: bool = True
    fatal: bool = False
    validated: bool = True


@dataclass(frozen=True)
class Control:
    cycle_generation: str
    account_id: str = 'acct-1'
    transition_id: str = 'startup-1'
    control_version: int = 1
    effective_running: bool = True
    fatal: bool = False


@dataclass(frozen=True)
class Order:
    order_id: str
    token_id: str
    side: str = 'BUY'
    account_id: str = 'acct-1'
    venue: str = 'polymarket'
    origin: str = 'auto'


@dataclass(frozen=True)
class Orders:
    cycle_generation: str
    observation_id: str = 'obs-1'
    account_id: str = 'acct-1'
    accepted: bool = True
    orders_current: bool = True
    orders_complete: bool = True
    orders_error: str | None = None
    open_orders: tuple[Order, ...] = ()
    checked_at: datetime = datetime(2026, 10, 16, 8, tzinfo=UTC)
    inventory: tuple = (('inventory-token', Decimal('7.25')),)
    balance_error: str | None = 'unavailable'
    positions_error: str | None = 'unavailable'


def initialized(*, running=True):
    from open_trader.polymarket_lp_round import LPRoundCoordinator
    coordinator = LPRoundCoordinator()
    update = coordinator.initialize(Startup(effective_running=running), process_epoch='runtime-1')
    assert update.status == 'initialized'
    return coordinator, Control(update.cycle_generation, effective_running=running)


def test_paused_start_observes_without_cleanup_or_round_progress():
    coordinator, control = initialized(running=False)
    observation = Orders(control.cycle_generation, open_orders=(Order('A', 'token-A'),))
    start = coordinator.begin_round(control, observation)
    assert start.status == 'observe_only'
    assert start.token is None
    snap = coordinator.snapshot()
    assert snap.round_number == 0
    assert snap.active_batch_id is None
    assert snap.cycle_generation == control.cycle_generation
    assert not {'effective_running', 'fatal', 'save_state'} & set(snap.__dataclass_fields__)
    assert observation.open_orders == (Order('A', 'token-A'),)


def test_round_zero_selects_exact_all_account_buy_ids():
    coordinator, control = initialized()
    observation = Orders(control.cycle_generation, open_orders=(
        Order('A', 'token-A'), Order('B', 'token-B', origin='manual'),
        Order('S', 'token-S', side='SELL'),
    ))
    start = coordinator.begin_round(control, observation)
    assert start.status == 'batch'
    token = start.token
    assert token.kind == 'STARTUP_CANCEL'
    assert token.round_number == 0
    assert token.account_id == 'acct-1'
    assert token.cycle_generation == control.cycle_generation
    assert token.observation_id == 'obs-1'
    assert {a.target.existing_order_id for a in token.actions} == {'A', 'B'}
    assert {a.target.token_id for a in token.actions} == {'token-A', 'token-B'}
    assert all(a.target.side == 'BUY' and a.target.kind == 'CANCEL_EXISTING_BUY'
               and a.account_id == 'acct-1' for a in token.actions)
    assert len({a.request_id for a in token.actions}) == 2
    assert all(a.request_id for a in token.actions)
    assert coordinator.snapshot().round_number == 0
    again = coordinator.begin_round(control, observation)
    assert again.status == 'busy'
    assert again.token == token
    assert observation.inventory == (('inventory-token', Decimal('7.25')),)


def test_failed_or_incomplete_orders_keep_round_zero():
    coordinator, control = initialized()
    current = Orders(control.cycle_generation)
    invalid = (
        replace(current, orders_error='read_failed'),
        replace(current, orders_complete=False),
        replace(current, orders_current=False),
        replace(current, accepted=False),
        replace(current, account_id='other-account'),
        replace(current, cycle_generation='old-generation'),
        replace(current, observation_id=''),
        replace(current, open_orders=(Order('A', 'token-A', account_id='other-account'),)),
        replace(current, open_orders=(Order('', 'token-A'),)),
        replace(current, open_orders=(Order('A', 'token-A'), Order('A', 'token-A'))),
    )
    for observation in invalid:
        start = coordinator.begin_round(control, observation)
        assert start.status == 'blocked'
        assert start.token is None
        assert coordinator.snapshot().round_number == 0
        assert coordinator.snapshot().active_batch_id is None
    for bad_control in (replace(control, account_id='wrong'),
                        replace(control, control_version=0),
                        replace(control, transition_id='not-applied'),
                        replace(control, cycle_generation='old')):
        assert coordinator.begin_round(bad_control, current).status == 'blocked'
    assert coordinator.snapshot().round_number == 0


def test_complete_empty_buy_list_finishes_round_zero():
    coordinator, control = initialized()
    observation = Orders(control.cycle_generation, open_orders=(Order('S', 'token-S', side='SELL'),))
    start = coordinator.begin_round(control, observation)
    assert start.status == 'batch'
    assert start.token.kind == 'STARTUP_CANCEL'
    assert start.token.actions == ()
    assert coordinator.snapshot().round_number == 0
    finished = coordinator.finish_local_attempts(control, start.token, ())
    assert finished.status == 'completed'
    assert finished.round_number == 1
    assert coordinator.snapshot().active_batch_id is None
    assert coordinator.finish_local_attempts(control, start.token, ()).status == 'duplicate'
    assert coordinator.snapshot().round_number == 1
    assert observation.inventory == (('inventory-token', Decimal('7.25')),)


def fixed_plan(control, observation_id, names=('C', 'D', 'E')):
    from open_trader.polymarket_lp_round import BuyTarget, BuyTerms, FixedBuyPlan
    return FixedBuyPlan('acct-1', control.cycle_generation, observation_id, 'plan-CDE', tuple(
        BuyTarget('polymarket', f'token-{name}', f'market-{name}',
                  BuyTerms(Decimal('0.37'), Decimal('12.50'), 'GTD', 1792141200,
                           True, (('defer_exec', False),))) for name in names))


def test_typed_local_outcomes_finish_startup_and_fixed_buy_rounds():
    from open_trader.polymarket_lp_round import BuyTarget, CancelTarget, LocalOutcome
    coordinator, control = initialized()
    startup = coordinator.begin_round(control, Orders(control.cycle_generation, open_orders=(
        Order('A', 'token-A'), Order('B', 'token-B', origin='manual')))).token
    a, b = startup.actions
    cancelled = (LocalOutcome(b, 'unknown_error', True), LocalOutcome(a, 'accepted', True, 'A'))
    bad_cancels = (
        replace(a, request_id='wrong-request'),
        replace(a, account_id='wrong-account'),
        replace(a, target=CancelTarget('polymarket', 'token-A', 'NEW-A')),
        replace(a, target=CancelTarget('polymarket', 'other-token', 'A')),
    )
    for identity in bad_cancels:
        assert coordinator.finish_local_attempts(control, startup, (
            cancelled[0], LocalOutcome(identity, 'rejected', True))).status == 'identity_error'
        assert coordinator.snapshot().round_number == 0
    assert coordinator.finish_local_attempts(control, startup, (
        cancelled[0], LocalOutcome(a, 'accepted', True, 'other-venue-id'))).status == 'identity_error'
    assert coordinator.finish_local_attempts(control, startup, cancelled[:1]).status == 'incomplete'
    assert coordinator.finish_local_attempts(control, startup, (cancelled[0], cancelled[0])).status == 'identity_error'
    assert coordinator.finish_local_attempts(control, startup, cancelled).status == 'completed'
    assert coordinator.finish_local_attempts(control, startup, cancelled).status == 'duplicate'
    assert coordinator.snapshot().round_number == 1
    observation = Orders(control.cycle_generation, observation_id='obs-2')
    assert coordinator.begin_round(control, observation).status == 'needs_fixed_plan'
    plan = fixed_plan(control, 'obs-2')
    for wrong in (replace(plan, account_id='wrong'), replace(plan, cycle_generation='old'),
                  replace(plan, observation_id='other-observation')):
        assert coordinator.begin_round(control, observation, fixed_buys=wrong).status == 'blocked'
    start = coordinator.begin_round(control, observation, fixed_buys=plan)
    assert start.status == 'batch'
    token = start.token
    assert token.kind == 'NORMAL_BUY' and token.round_number == 1
    assert tuple(a.target for a in token.actions) == plan.targets
    assert {a.target.token_id for a in token.actions} == {'token-C', 'token-D', 'token-E'}
    assert len({a.request_id for a in (*startup.actions, *token.actions)}) == 5
    assert all(not hasattr(a.target, 'venue_order_id') and not hasattr(a.target, 'existing_order_id')
               for a in token.actions)
    c, d, e = token.actions
    outcomes = (LocalOutcome(e, 'unknown_timeout', True), LocalOutcome(c, 'rejected', True),
                LocalOutcome(d, 'not_sent', True))
    assert all(o.venue_order_id is None for o in outcomes)
    bad_buys = (
        replace(c, account_id='wrong'), replace(c, request_id='wrong'),
        replace(c, target=replace(c.target, token_id='wrong')),
        replace(c, target=replace(c.target, venue='wrong')),
        replace(c, target=replace(c.target, market_id='wrong')),
        replace(c, target=replace(c.target, terms=replace(c.target.terms, price=Decimal('0.38')))),
        replace(c, target=replace(c.target, terms=replace(c.target.terms, quantity=Decimal('13')))),
        replace(c, target=replace(c.target, terms=replace(c.target.terms, post_only=False))),
        replace(c, target=CancelTarget('polymarket', 'token-C', 'existing-C')),
    )
    for identity in bad_buys:
        assert coordinator.finish_local_attempts(control, token, (
            outcomes[0], LocalOutcome(identity, 'rejected', True), outcomes[2])).status == 'identity_error'
        assert coordinator.snapshot().round_number == 1
    assert coordinator.finish_local_attempts(control, replace(token, kind='STARTUP_CANCEL'), outcomes).status == 'identity_error'
    assert coordinator.finish_local_attempts(control, token, (
        replace(outcomes[0], local_attempt_finished=False), *outcomes[1:])).status == 'incomplete'
    for wrong in (replace(control, effective_running=False), replace(control, fatal=True),
                  replace(control, control_version=9), replace(control, transition_id='future')):
        assert coordinator.finish_local_attempts(wrong, token, outcomes).status == 'control_not_current'
    assert coordinator.snapshot().round_number == 1
    assert coordinator.finish_local_attempts(control, token, outcomes).status == 'completed'
    assert coordinator.finish_local_attempts(control, token, outcomes).status == 'duplicate'
    assert coordinator.snapshot().round_number == 2
    assert token.actions[0].target == BuyTarget('polymarket', 'token-C', 'market-C', plan.targets[0].terms)
    assert token.actions[0].target.terms.price == Decimal('0.37')
    assert token.actions[0].target.terms.quantity == Decimal('12.50')
    assert not hasattr(coordinator.snapshot(), 'native_finished')
    with pytest.raises(AttributeError):
        token.actions[0].target.terms.price = Decimal('0.99')
    # An explicitly empty normal plan also goes through one begin/finish.
    empty_observation = replace(observation, observation_id='obs-3')
    empty = coordinator.begin_round(control, empty_observation,
                                     fixed_buys=fixed_plan(control, 'obs-3', ())).token
    assert empty.actions == () and empty.kind == 'NORMAL_BUY'
    assert coordinator.finish_local_attempts(control, empty, ()).round_number == 3
    assert coordinator.finish_local_attempts(control, empty, ()).status == 'duplicate'


@dataclass(frozen=True)
class Transition:
    transition_id: str
    control_version: int
    previous_running: bool
    effective_running: bool
    cause: str
    account_id: str = 'acct-1'
    admitted: bool = True


def pause_resume(coordinator, control):
    paused = Transition('pause-2', 2, True, False, 'pause')
    assert coordinator.apply_control(paused).status == 'applied'
    resume = Transition('resume-3', 3, False, True, 'explicit_resume')
    update = coordinator.apply_control(resume)
    return update, Control(update.cycle_generation, transition_id='resume-3', control_version=3)


def test_resume_uses_fresh_orders_and_new_generation():
    from open_trader.polymarket_lp_round import LocalOutcome
    coordinator, control = initialized()
    empty = coordinator.begin_round(control, Orders(control.cycle_generation)).token
    coordinator.finish_local_attempts(control, empty, ())
    old_observation = Orders(control.cycle_generation, observation_id='obs-old',
                             open_orders=(Order('A', 'token-A'),))
    old_plan = fixed_plan(control, 'obs-old')
    old_buy = coordinator.begin_round(control, old_observation, fixed_buys=old_plan).token
    paused = Transition('pause-2', 2, True, False, 'pause')
    update = coordinator.apply_control(paused)
    assert update.status == 'applied'
    assert not update.reset_provider_wait and not update.reset_unknown_since
    assert coordinator.snapshot().round_number == 1
    assert coordinator.snapshot().cycle_generation == control.cycle_generation
    paused_control = replace(control, transition_id='pause-2', control_version=2, effective_running=False)
    assert coordinator.begin_round(paused_control, old_observation).status == 'observe_only'
    assert coordinator.finish_local_attempts(paused_control, old_buy, tuple(
        LocalOutcome(a, 'not_sent', True) for a in old_buy.actions)).status == 'control_not_current'
    resume = Transition('resume-3', 3, False, True, 'explicit_resume')
    before = coordinator.snapshot()
    for bad in (replace(resume, account_id='other'), replace(resume, admitted=False)):
        assert coordinator.apply_control(bad).status == 'invalid_identity'
        assert coordinator.snapshot() == before
    update = coordinator.apply_control(resume)
    assert update.status == 'applied'
    assert update.reset_provider_wait and update.reset_unknown_since
    assert update.cycle_generation != control.cycle_generation
    assert update.round_number == 0
    assert coordinator.snapshot().active_batch_id is None
    replay = coordinator.apply_control(resume)
    assert replay.status == 'duplicate'
    assert replay.cycle_generation == update.cycle_generation
    assert not replay.reset_provider_wait and not replay.reset_unknown_since
    assert coordinator.apply_control(Transition('old-1', 1, True, False, 'pause')).status == 'stale'
    new_control = Control(update.cycle_generation, transition_id='resume-3', control_version=3)
    assert coordinator.begin_round(new_control, old_observation, fixed_buys=old_plan).status == 'blocked'
    fresh = Orders(update.cycle_generation, observation_id='fresh-after-resume', open_orders=(
        Order('A', 'token-A'), Order('D', 'token-D', origin='manual'), Order('S', 'token-S', side='SELL')))
    start = coordinator.begin_round(new_control, fresh, fixed_buys=old_plan)
    assert start.status == 'batch' and start.token.kind == 'STARTUP_CANCEL'
    assert {a.target.existing_order_id for a in start.token.actions} == {'A', 'D'}
    assert {a.request_id for a in start.token.actions}.isdisjoint(a.request_id for a in old_buy.actions)
    assert coordinator.snapshot().round_number == 0


def test_old_completion_cannot_advance_resumed_round_zero():
    from open_trader.polymarket_lp_round import LocalOutcome
    coordinator, control = initialized()
    startup = coordinator.begin_round(control, Orders(control.cycle_generation)).token
    coordinator.finish_local_attempts(control, startup, ())
    buy = coordinator.begin_round(control, Orders(control.cycle_generation, observation_id='obs-2'),
                                   fixed_buys=fixed_plan(control, 'obs-2', ('C',))).token
    original = buy.actions[0]
    timeout = (LocalOutcome(original, 'unknown_timeout', True),)
    assert coordinator.finish_local_attempts(control, buy, timeout).status == 'completed'
    assert coordinator.snapshot().round_number == 2
    late = (LocalOutcome(original, 'accepted', True, 'actual-C-venue-ID', 'LIVE'),)
    assert coordinator.finish_local_attempts(control, buy, late).status == 'duplicate'
    assert coordinator.snapshot().round_number == 2
    assert buy.actions == (original,)
    assert not hasattr(original.target, 'venue_order_id')

    # Caller-owned early token stays valid after arbitrary subsequent rounds.
    # The helper drops every strong reference for the other 64 round payloads.
    def complete_unretained_round(index):
        observation_id = f'observation-{index + 3}'
        plan = replace(fixed_plan(control, observation_id), plan_id=f'plan-{index + 3}')
        token = coordinator.begin_round(control, Orders(control.cycle_generation,
            observation_id=observation_id), fixed_buys=plan).token
        refs = tuple(weakref.ref(value) for value in (
            token, *token.actions, *(a.target for a in token.actions),
            *(a.target.terms for a in token.actions)))
        assert coordinator.finish_local_attempts(control, token, tuple(
            LocalOutcome(action, 'rejected', True) for action in token.actions)).status == 'completed'
        return refs

    historical_refs = tuple(complete_unretained_round(index) for index in range(64))
    gc.collect()
    # At most one round's bookkeeping is allowed; freeing only one item fails.
    assert sum(any(ref() is not None for ref in refs) for refs in historical_refs) <= 1
    assert sum(ref() is not None for refs in historical_refs for ref in refs) <= 10
    before = coordinator.snapshot()
    assert before.round_number == 66 and before.active_batch_id is None
    detached = copy.deepcopy(buy)
    assert detached == buy and detached is not buy
    assert coordinator.finish_local_attempts(control, detached, late).status == 'duplicate'
    equivalent = replace(detached, actions=(replace(detached.actions[0], target=replace(
        detached.actions[0].target, terms=replace(detached.actions[0].target.terms,
            price=Decimal('0.3700'), quantity=Decimal('12.500')))),))
    assert equivalent == buy
    assert coordinator.finish_local_attempts(control, equivalent, late).status == 'duplicate'
    assert coordinator.snapshot() == before

    from open_trader.polymarket_lp_round import CancelTarget
    bad_tokens = (
        (replace(buy, account_id='wrong-account'), 'identity_error'),
        (replace(buy, cycle_generation='wrong-generation'), 'stale'),
        (replace(buy, batch_id='forged-batch'), 'identity_error'),
        (replace(buy, round_number=0), 'identity_error'),
        (replace(buy, kind='STARTUP_CANCEL'), 'identity_error'),
        (replace(buy, observation_id='wrong-observation'), 'identity_error'),
        (replace(buy, actions=(replace(original, request_id='wrong-request'),)), 'identity_error'),
        (replace(buy, actions=(replace(original, account_id='wrong-account'),)), 'identity_error'),
        (replace(buy, actions=(replace(original,
            target=replace(original.target, token_id='wrong-token')),)), 'identity_error'),
        (replace(buy, actions=(replace(original,
            target=replace(original.target, venue='wrong-venue')),)), 'identity_error'),
        (replace(buy, actions=(replace(original,
            target=replace(original.target, market_id='wrong-market')),)), 'identity_error'),
        (replace(buy, actions=(replace(original,
            target=CancelTarget('polymarket', 'token-C', 'existing-C')),)), 'identity_error'),
    )
    for token, status in bad_tokens:
        assert coordinator.finish_local_attempts(control, token, late).status == status
        assert coordinator.snapshot() == before
    for changed_terms in (
        replace(original.target.terms, price=Decimal('0.38')),
        replace(original.target.terms, quantity=Decimal('13')),
        replace(original.target.terms, order_type='GTC'),
        replace(original.target.terms, expiration=1792141201),
        replace(original.target.terms, post_only=False),
        replace(original.target.terms, send_options=(('defer_exec', True),)),
    ):
        forged = replace(buy, actions=(replace(original,
            target=replace(original.target, terms=changed_terms)),))
        assert coordinator.finish_local_attempts(control, forged, late).status == 'identity_error'
        assert coordinator.snapshot() == before
    assert buy.actions == (original,)
    update, current = pause_resume(coordinator, control)
    assert update.round_number == 0
    assert coordinator.finish_local_attempts(current, buy, late).status == 'stale'
    assert coordinator.finish_local_attempts(control, buy, late).status == 'stale'
    assert coordinator.snapshot().round_number == 0
    assert coordinator.snapshot().active_batch_id is None
    fresh = coordinator.begin_round(current, Orders(current.cycle_generation,
        observation_id='fresh', open_orders=(Order('actual-C-venue-ID', 'token-C'),))).token
    assert fresh.kind == 'STARTUP_CANCEL'
    assert fresh.actions[0].target.existing_order_id == 'actual-C-venue-ID'
    before = coordinator.snapshot()
    assert coordinator.finish_local_attempts(current, buy, late).status == 'stale'
    assert coordinator.snapshot() == before
    assert coordinator.begin_round(control, Orders(control.cycle_generation),
                                    fixed_buys=fixed_plan(control, 'obs-2', ('C',))).status == 'blocked'
    assert buy.actions == (original,) and late[0].venue_order_id == 'actual-C-venue-ID'


def test_running_duplicate_control_is_noop():
    coordinator, control = initialized()
    startup = coordinator.begin_round(control, Orders(control.cycle_generation,
        open_orders=(Order('A', 'token-A'),))).token
    before = coordinator.snapshot()
    repeated_run = Transition('repeat-run-2', 2, True, True, 'run')
    update = coordinator.apply_control(repeated_run)
    assert update.status == 'duplicate'
    assert update.cycle_generation == control.cycle_generation
    assert update.round_number == 0
    assert not update.reset_provider_wait and not update.reset_unknown_since
    assert coordinator.snapshot() == before
    assert coordinator.apply_control(repeated_run).status == 'duplicate'
    assert coordinator.snapshot() == before
    # Ordinary wake is another call with the same current control, not resume.
    again = coordinator.begin_round(control, Orders(control.cycle_generation,
        observation_id='wake-observation', open_orders=(Order('D', 'token-D', origin='manual'),)))
    assert again.status == 'busy' and again.token == startup
    assert {a.target.existing_order_id for a in again.token.actions} == {'A'}
    assert coordinator.snapshot() == before
    assert coordinator.apply_control(Transition('bad-resume', 3, True, True, 'explicit_resume')).status == 'duplicate'
    assert coordinator.snapshot() == before
