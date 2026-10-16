"""Current LP values and their single in-process authority.

No acquisition, persistence or execution capability lives here. The integration
owner supplies the original frame and its already prepared calculation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, Literal, TypeVar
import threading
import time
import math

if TYPE_CHECKING:
    from .polymarket_lp_market_reader import MarketFrame
    from .polymarket_lp_market_calculation import CalculationResult

OrderKey = tuple[str, str, str, str]
ScopeKey = tuple[str, str, str, str]
TransitionId = tuple[str, int]
T = TypeVar('T')
FactState = Literal['known', 'unknown', 'not_read']
_CURRENT_ORDER_STATES = frozenset({'LIVE', 'OPEN', 'ACCEPTED', 'PARTIALLY_FILLED',
    'CANCELING', 'CANCELLING', 'PENDING_CANCEL'})
_TERMINAL_ORDER_STATES = frozenset({'CANCELED', 'CANCELLED', 'FILLED', 'MATCHED', 'EXPIRED', 'REJECTED'})


@dataclass(frozen=True)
class SourceStamp:
    source_id: str
    checked_at: datetime | None = None
    source_timestamp: datetime | None = None
    read_started_at: datetime | None = None
    read_ended_at: datetime | None = None
    complete: bool = False
    state: FactState = 'not_read'
    error_code: str | None = None
    retry_at: float | None = None

    def __post_init__(self):
        if self.state not in ('known', 'unknown', 'not_read') or type(self.complete) is not bool:
            raise ValueError('invalid_source_state')
        for value in (self.checked_at, self.source_timestamp, self.read_started_at, self.read_ended_at):
            if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None
                    or value.utcoffset().total_seconds() != 0):
                raise ValueError('source_time_requires_utc')
        if self.retry_at is not None and not math.isfinite(self.retry_at):
            raise ValueError('invalid_source_retry_deadline')


@dataclass(frozen=True)
class Fact(Generic[T]):
    state: FactState
    value: T | None = None
    sources: tuple[SourceStamp, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self):
        if self.state not in ('known', 'unknown', 'not_read'):
            raise ValueError('invalid_fact_state')
        if self.state != 'known' and self.value is not None:
            raise ValueError('unknown_fact_has_value')
        object.__setattr__(self, 'sources', tuple(self.sources))
        object.__setattr__(self, 'reason_codes', tuple(self.reason_codes))
        if self.value is not None and not isinstance(self.value, (Decimal, datetime, bool, int, str)):
            raise TypeError('fact_requires_scalar_value')
        if isinstance(self.value, Decimal) and not self.value.is_finite():
            raise ValueError('nonfinite_fact')


@dataclass(frozen=True)
class ReadTicket:
    account_id: str
    process_epoch: str
    cycle_generation: str
    observation_id: str
    read_sequence: int
    trade_version_at_start: int
    started_at: datetime
    started_monotonic: float
    order_terms_versions: tuple[tuple[OrderKey, int], ...]


@dataclass(frozen=True)
class EffectiveControlTransition:
    account_id: str
    process_epoch: str
    transition_id: TransitionId
    control_version: int
    previous_control_version: int | None
    previous_running: bool | None
    effective_running: bool
    cause: Literal['startup', 'pause', 'explicit_resume']


@dataclass(frozen=True)
class CurrentControl:
    account_id: str
    process_epoch: str
    cycle_generation: str | None = None
    transition_id: TransitionId | None = None
    control_version: int | None = None
    effective_running: bool | None = None
    phase: Literal['starting', 'paused', 'running', 'stopping', 'stopped'] = 'starting'
    shutdown_requested: bool = False
    fatal_id: str | None = None


@dataclass(frozen=True)
class ValidatedStartAdmission:
    process_epoch: str
    permit_claim_id: str


@dataclass(frozen=True)
class StoredControl:
    account_id: str
    desired_running: bool
    ever_enabled: bool
    enabled_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class ControlWrite:
    account_id: str
    process_epoch: str
    request_version: int
    request_id: str
    desired_running: bool
    requested_at: datetime
    actor: str | None


@dataclass(frozen=True)
class CommittedControl:
    request_id: str
    request_version: int
    desired_running: bool
    committed_at: datetime


@dataclass(frozen=True)
class AppliedControl:
    cycle_generation: str


@dataclass(frozen=True)
class FatalControlSignal:
    fatal_id: str
    process_epoch: str
    account_id: str | None
    origin: Literal['startup_read', 'save_retry_exhausted', 'save_timeout']
    request_id: str | None
    request_version: int | None
    attempt_number: int
    outcome: Literal['failed', 'unknown']
    error_code: str
    detected_at: datetime
    detected_monotonic: float
    human_recovery_required: Literal[True] = True


@dataclass(frozen=True)
class ControlStatus:
    current: CurrentControl
    requested_running: bool | None = None
    requested_version: int | None = None
    saved_version: int | None = None
    save_state: Literal['idle', 'pending', 'saving', 'retry_wait', 'retrying', 'saved', 'failed', 'unknown'] = 'idle'
    attempt_number: int = 0
    retry_at_monotonic: float | None = None
    attempt_started_monotonic: float | None = None
    attempt_deadline_monotonic: float | None = None
    native_save_in_flight: bool = False
    error_code: str | None = None
    fatal: FatalControlSignal | None = None


@dataclass(frozen=True)
class ControlRequestResult:
    status: Literal['accepted', 'noop', 'rejected']
    reason: Literal['control_save_pending', 'stopping', 'identity_error'] | None
    state: ControlStatus
    transition: EffectiveControlTransition | None = None


@dataclass(frozen=True)
class ControlCloseReport:
    writer_drained: bool
    native_request_id: str | None
    fatal: FatalControlSignal | None


@dataclass(frozen=True)
class TerminalProof:
    order_key: OrderKey
    status: str
    original_size: Fact[Decimal]
    matched: Fact[Decimal]
    source: SourceStamp
    identity_verified: bool


@dataclass(frozen=True)
class CurrentOrderFact:
    key: OrderKey
    side: Literal['BUY', 'SELL']
    venue_status: str
    price: Fact[Decimal]
    original_size: Fact[Decimal]
    size_matched: Fact[Decimal]
    remaining: Fact[Decimal]
    presence: Literal['present', 'absent', 'unknown']
    terminal_proof: TerminalProof | None
    order_type: Fact[Literal['GTC', 'GTD', 'FOK', 'FAK']]
    expiration: Fact[int | None]
    terms_version: int
    source: SourceStamp


@dataclass(frozen=True)
class CurrentPositionFact:
    account_id: str
    condition_id: str
    token_id: str
    quantity: Fact[Decimal]
    inventory_cost_usd: Fact[Decimal]
    average_price: Fact[Decimal]
    redeemable: Fact[bool]
    source: SourceStamp


@dataclass(frozen=True)
class BuyYieldState:
    order_key: OrderKey
    yield_pct_per_hour: Fact[Decimal]
    input_sources: tuple[SourceStamp, ...]
    evaluated_at: datetime
    cycle_generation: str
    unknown_since_monotonic: float | None = None
    unknown_since_at: datetime | None = None
    current_risk_request_id: str | None = None

    @property
    def unknown_deadline_monotonic(self) -> float | None:
        return None if self.unknown_since_monotonic is None else self.unknown_since_monotonic + 600


@dataclass(frozen=True)
class CurrentOrderView:
    account_id: str
    process_epoch: str
    cycle_generation: str
    observation_id: str
    read_ticket: ReadTicket
    snapshot_version: int
    source_frame: MarketFrame
    open_orders: tuple[CurrentOrderFact, ...]
    orders_complete: bool
    orders_error: str | None
    orders_source: SourceStamp


@dataclass(frozen=True)
class CurrentObservation:
    account_id: str
    process_epoch: str
    cycle_generation: str
    observation_id: str
    read_ticket: ReadTicket
    snapshot_version: int
    accepted_at: datetime
    source_frame: MarketFrame
    calculation: CalculationResult
    components: Mapping[str, SourceStamp]
    orders: tuple[CurrentOrderFact, ...]
    positions: tuple[CurrentPositionFact, ...]
    yields: Mapping[OrderKey, BuyYieldState]
    orders_complete: bool
    positions_complete: bool
    order_view: CurrentOrderView


@dataclass(frozen=True)
class Acceptance:
    status: Literal['accepted', 'partial', 'stale', 'invalid_identity']
    observation: CurrentObservation | None = None


@dataclass(frozen=True)
class ProtectionKey:
    kind: Literal['queue_bucket', 'first_seen_episode']
    scope: ScopeKey
    price: Decimal
    episode_id: str | None

    def __post_init__(self):
        if self.kind not in ('queue_bucket', 'first_seen_episode') or (self.kind == 'first_seen_episode' and not self.episode_id):
            raise ValueError('invalid_protection_key')


@dataclass(frozen=True)
class ProtectionGuard:
    scope: ScopeKey | OrderKey | ProtectionKey
    cycle_generation: str
    expected_scope_version: int
    expected_order_terms_versions: tuple[tuple[OrderKey, int], ...]
    observation_id: str | None
    event_id: str


@dataclass(frozen=True)
class ProtectionBaseline:
    key: ProtectionKey
    anchor_order_ids: tuple[str, ...]
    baseline_price: Decimal
    baseline_front: Fact[Decimal]
    baseline_source: Literal['submit', 'account_sync', 'first_observation']
    baseline_book_received_at: datetime | None
    baseline_source_timestamp: datetime | None
    baseline_book_hash: str | None
    baseline_version: int
    registered_at: datetime
    threshold: Decimal
    source: SourceStamp


@dataclass(frozen=True)
class CancelTargetState:
    order_key: OrderKey
    request_id: str
    requested_remaining: Fact[Decimal]
    requested_at: datetime
    reason: str
    local_outcome: Literal['pending', 'accepted', 'rejected', 'not_sent', 'unknown_timeout', 'unknown_error']
    native_state: Literal['pending', 'running', 'ended']
    venue_terminal: TerminalProof | None
    canceled_remaining: Fact[Decimal]
    fill_since_request: Fact[Decimal]
    retryable_before_send: bool


@dataclass(frozen=True)
class ProtectionFailureState:
    scope_kind: Literal['queue_group', 'first_seen_episode']
    scope_id: ScopeKey | ProtectionKey
    data_failures: int
    last_evaluation_id: str | None
    scope_version: int


@dataclass(frozen=True)
class ProtectionProgress:
    key: ProtectionKey
    state: Literal['unknown', 'registered', 'monitoring', 'triggered', 'canceling', 'canceled', 'partially_filled']
    front_estimate: Fact[Decimal]
    level_total: Fact[Decimal]
    ratio: Fact[Decimal]
    reason_codes: tuple[str, ...]
    data_time: datetime | None
    cancel_targets: tuple[CancelTargetState, ...]
    scope_version: int
    failure_state: ProtectionFailureState


@dataclass(frozen=True)
class OpeningEconomics:
    scope: ScopeKey
    buy_cost_usd: Fact[Decimal]
    sold_revenue_usd: Fact[Decimal]
    fees_usd: Fact[Decimal]
    residual_exit_value_usd: Fact[Decimal]
    position_reconciled: bool

    @property
    def opening_loss_usd(self) -> Fact[Decimal]:
        terms = (self.buy_cost_usd, self.sold_revenue_usd, self.fees_usd, self.residual_exit_value_usd)
        sources = tuple(s for t in terms for s in t.sources)
        if not self.position_reconciled or any(t.state != 'known' or t.value is None for t in terms):
            return Fact('unknown', sources=sources, reason_codes=('unproven_opening_economics',))
        return Fact('known', self.buy_cost_usd.value - self.sold_revenue_usd.value
            - self.residual_exit_value_usd.value + self.fees_usd.value, sources)


@dataclass(frozen=True)
class ScoringState:
    order_key: OrderKey | None
    role: Literal['entry', 'passive_exit', 'protected_exit'] | None
    scoring: Fact[bool]
    checked_at: datetime | None
    lost_since_at: datetime | None
    lost_since_monotonic: float | None
    source: SourceStamp


@dataclass(frozen=True)
class OpeningProtection:
    scope: ScopeKey
    phase: Literal['opening', 'passive_exit', 'protected_exit', 'review', 'closed']
    attention_reason: str | None
    entry_order_ids: tuple[str, ...]
    buy_filled_quantity: Fact[Decimal]
    economics: OpeningEconomics
    stop_loss_latched: Fact[bool]
    stop_loss_triggered_at: datetime | None
    stop_loss_triggered_loss_usd: Fact[Decimal]
    review_at: Fact[datetime]
    review_status: Literal['none', 'awaiting_reconciliation', 'scoring_continuously_lost']
    stop_requested: bool
    scoring: ScoringState
    scope_version: int


@dataclass(frozen=True)
class OwnFillEvidence:
    order_key: OrderKey
    quantity: Fact[Decimal]
    notional_usd: Fact[Decimal]
    fees_usd: Fact[Decimal]
    fill_ids: tuple[str, ...]
    source: SourceStamp
    complete_for_target: bool


@dataclass(frozen=True)
class ExitAttempt:
    request_id: str
    kind: Literal['passive_exit', 'protected_exit']
    order_key: OrderKey | None
    state: Literal['pending', 'unknown', 'accepted_without_order_id', 'accepted', 'rejected', 'terminal']
    retryable: bool
    submit_quantity: Fact[Decimal]
    submit_sold_quantity: Fact[Decimal]
    submit_residual_quantity: Fact[Decimal]
    price: Fact[Decimal]
    order_type: Fact[Literal['GTC', 'GTD', 'FOK', 'FAK']]
    expiration: Fact[int | None]
    cancel_request_id: str | None
    own_fill_evidence: OwnFillEvidence | None
    native_state: Literal['pending', 'running', 'ended']


@dataclass(frozen=True)
class SellState:
    scope: ScopeKey
    residual_quantity: Fact[Decimal]
    inventory_cost_usd: Fact[Decimal]
    position_reconciled: bool
    current_sells: tuple[CurrentOrderFact, ...]
    orders_source: SourceStamp
    positions_source: SourceStamp
    passive: ExitAttempt | None
    protected: ExitAttempt | None


@dataclass(frozen=True)
class SetBaseline:
    guard: ProtectionGuard
    baseline: ProtectionBaseline


@dataclass(frozen=True)
class QueueProgressUpdate:
    guard: ProtectionGuard
    progress: ProtectionProgress


@dataclass(frozen=True)
class CancelEvidenceUpdate:
    guard: ProtectionGuard
    target: CancelTargetState


@dataclass(frozen=True)
class OpeningProtectionUpdate:
    guard: ProtectionGuard
    opening: OpeningProtection


@dataclass(frozen=True)
class ScoringObservation:
    guard: ProtectionGuard
    scoring: ScoringState


@dataclass(frozen=True)
class ExitEvidenceUpdate:
    guard: ProtectionGuard
    sell: SellState


ProtectionUpdate = SetBaseline | QueueProgressUpdate | CancelEvidenceUpdate | OpeningProtectionUpdate | ScoringObservation | ExitEvidenceUpdate


@dataclass(frozen=True)
class BuyExecutionUncertainty:
    event_id: str
    request_id: str
    account_id: str
    process_epoch: str
    cycle_generation: str
    batch_id: str
    round_number: int
    observation_id: str
    venue: str
    condition_id: str
    token_id: str
    price: Decimal
    quantity: Decimal
    order_type: Literal['GTC', 'GTD', 'FOK', 'FAK']
    expiration: int | None
    post_only: bool
    send_options: tuple[tuple[str, str | int | bool | None], ...]
    native_started_at: datetime
    observed_at: datetime
    reason: Literal['unknown_timeout', 'unknown_error', 'invalid_receipt']
    venue_order_id: str | None = None


@dataclass(frozen=True)
class ReceiptEvidence:
    event_id: str
    request_id: str
    account_id: str
    process_epoch: str
    cycle_generation: str
    order_key: OrderKey
    side: Literal['BUY', 'SELL']
    expected_terms_version: int
    terminal_proof: TerminalProof | None = None
    own_fill: OwnFillEvidence | None = None
    order_fact: CurrentOrderFact | None = None
    cancel_target: CancelTargetState | None = None
    exit_attempt: ExitAttempt | None = None
    scope: ScopeKey | ProtectionKey | None = None


@dataclass(frozen=True)
class CurrentSnapshot:
    current: CurrentControl
    snapshot_version: int
    trade_version: int
    observation: CurrentObservation | None
    orders: tuple[CurrentOrderFact, ...]
    positions: tuple[CurrentPositionFact, ...]
    yields: Mapping[OrderKey, BuyYieldState]
    buy_reserved: Fact[Decimal]
    inventory_cost: Fact[Decimal]
    current_exposure: Fact[Decimal]
    new_capacity: Fact[Decimal]
    own_fills: Mapping[OrderKey, OwnFillEvidence]
    unknown_yield_due: tuple[OrderKey, ...]
    baselines: Mapping[ProtectionKey, ProtectionBaseline]
    progress: Mapping[ProtectionKey, ProtectionProgress]
    failure_states: Mapping[ScopeKey | ProtectionKey, ProtectionFailureState]
    openings: Mapping[ScopeKey, OpeningProtection]
    sells: Mapping[ScopeKey, SellState]
    scope_versions: Mapping[ScopeKey | OrderKey | ProtectionKey, int]

    @property
    def active_orders(self) -> tuple[CurrentOrderFact, ...]:
        return tuple(o for o in self.orders if o.presence == 'present' and o.terminal_proof is None
            and o.remaining.state == 'known' and o.remaining.value > 0)

    @property
    def ordinary_poll_order_keys(self) -> tuple[OrderKey, ...]:
        return tuple(o.key for o in self.active_orders)


def _decimal(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fact(value, *sources):
    return Fact('unknown' if value is None else 'known', value, tuple(sources))


def _stamp(source) -> SourceStamp:
    if source is None:
        return SourceStamp('')
    return SourceStamp(source.source_id, checked_at=source.checked_at,
        source_timestamp=getattr(source, 'source_timestamp', None),
        read_started_at=getattr(source, 'read_started_at', None),
        read_ended_at=getattr(source, 'read_ended_at', None), complete=source.complete,
        state='unknown' if source.error or source.value is None else 'known',
        error_code=source.error, retry_at=source.retry_at)


class CurrentLPState:
    def __init__(self, account_id: str, process_epoch: str, *,
                 clock: Callable[[], float] = time.monotonic,
                 utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        if not account_id or not process_epoch:
            raise ValueError('missing_state_identity')
        self.account_id, self.process_epoch = account_id, process_epoch
        self._clock, self._utc_now = clock, utc_now
        self._lock = threading.RLock()
        self._current = CurrentControl(account_id, process_epoch)
        self._control_status = ControlStatus(self._current)
        self._read_sequence = self._snapshot_version = self._trade_version = 0
        self._applied_transitions = {}
        self._observation = None
        self._accepted_sequence = 0
        self._issued_tickets = {}
        self._source_watermarks = {}
        self._orders: dict[OrderKey, CurrentOrderFact] = {}
        self._positions = ()
        self._yields = {}
        self._receipt_events = set()
        self._uncertain_buy_events = {}
        self._uncertain_buy_requests = {}
        self._financial_uncertainty_at = None
        self._own_fills = {}
        self._baselines, self._progress, self._failures = {}, {}, {}
        self._openings, self._sells, self._scope_versions = {}, {}, {}
        self._protection_events = set()
        self._reserved = self._inventory = self._capacity = Fact('not_read')

    def apply_effective_control(self, transition: EffectiveControlTransition,
                                cycle_generation: str) -> CurrentControl:
        with self._lock:
            if (transition.account_id != self.account_id or transition.process_epoch != self.process_epoch
                    or not isinstance(cycle_generation, str) or not cycle_generation):
                raise ValueError('invalid_control_identity')
            previous = self._current
            known_transition = self._applied_transitions.get(transition.transition_id)
            if known_transition is not None:
                if known_transition != (transition, cycle_generation):
                    raise ValueError('conflicting_control_transition')
                return previous
            if previous.shutdown_requested:
                raise ValueError('control_stopping')
            if (transition.transition_id != (self.process_epoch, transition.control_version)
                    or type(transition.effective_running) is not bool
                    or transition.control_version < 0
                    or (previous.control_version is not None and transition.control_version <= previous.control_version)
                    or transition.previous_control_version != previous.control_version
                    or transition.previous_running is not previous.effective_running):
                raise ValueError('stale_control_transition')
            if transition.cause == 'startup':
                valid = previous.control_version is None and transition.previous_running is None
            elif transition.cause == 'pause':
                valid = previous.control_version is not None and transition.effective_running is False and cycle_generation == previous.cycle_generation
            elif transition.cause == 'explicit_resume':
                valid = previous.effective_running is False and transition.effective_running is True and cycle_generation != previous.cycle_generation
            else:
                valid = False
            if not valid:
                raise ValueError('invalid_control_transition')
            if transition.cause in ('startup', 'explicit_resume'):
                self._yields = {k: replace(y, unknown_since_monotonic=None, unknown_since_at=None,
                    cycle_generation=cycle_generation) for k, y in self._yields.items()}
            self._applied_transitions[transition.transition_id] = (transition, cycle_generation)
            self._current = CurrentControl(self.account_id, self.process_epoch, cycle_generation,
                transition.transition_id, transition.control_version, transition.effective_running,
                'running' if transition.effective_running else 'paused')
            self._snapshot_version += 1
            return self._current

    def begin_observation(self, cycle_generation: str, components) -> ReadTicket:
        with self._lock:
            if cycle_generation != self._current.cycle_generation:
                raise ValueError('invalid_observation_generation')
            self._read_sequence += 1
            components = tuple(components)
            if not components or any(not isinstance(c, str) or not c for c in components):
                raise ValueError('invalid_observation_components')
            ticket = ReadTicket(self.account_id, self.process_epoch, cycle_generation,
                f'{self.process_epoch}:read:{self._read_sequence}', self._read_sequence,
                self._trade_version, self._utc_now(), self._clock(),
                tuple((k, o.terms_version) for k, o in self._orders.items()))
            self._issued_tickets[ticket.observation_id] = (ticket, components)
            return ticket

    def accept(self, ticket: ReadTicket, source_frame: MarketFrame,
               calculation: CalculationResult, *, evaluated_at: datetime) -> Acceptance:
        with self._lock:
            if (not isinstance(ticket, ReadTicket) or ticket.account_id != self.account_id or ticket.process_epoch != self.process_epoch
                    or source_frame.account != self.account_id):
                return Acceptance('invalid_identity')
            if (ticket.cycle_generation != self._current.cycle_generation
                    or ticket.read_sequence <= self._accepted_sequence
                    or ticket.trade_version_at_start != self._trade_version):
                return Acceptance('stale')
            issued = self._issued_tickets.get(ticket.observation_id)
            if issued is None or issued[0] != ticket:
                return Acceptance('invalid_identity')
            if any(name not in source_frame.sources for name in issued[1]):
                return Acceptance('invalid_identity')
            stamps = {k: _stamp(v) for k, v in source_frame.sources.items()}
            os = stamps.get('open_orders', SourceStamp(''))
            ps = stamps.get('positions', SourceStamp(''))
            orders_at = self._source_watermarks.get('open_orders')
            orders_source_current = (os.state == 'known' and os.checked_at is not None
                and (orders_at is None or os.checked_at >= orders_at))
            orders_complete = source_frame.orders_complete and os.state == 'known' and os.complete and orders_source_current
            positions_at = self._source_watermarks.get('positions')
            positions_source_current = (ps.state == 'known' and ps.checked_at is not None
                and (positions_at is None or ps.checked_at >= positions_at))
            positions_complete = source_frame.positions_complete and ps.state == 'known' and ps.complete and positions_source_current
            orders = {}
            rejected_order_inputs = set() if orders_source_current else set(self._orders)
            for row in source_frame.orders:
                key = (self.account_id, row['condition_id'], row['token_id'], row['order_id'])
                original = _decimal(row.get('original_size', row.get('quantity')))
                matched, price = _decimal(row.get('size_matched')), _decimal(row.get('price'))
                if (row.get('account_id', self.account_id) != self.account_id or not all(key)
                        or key in orders or row.get('side') not in ('BUY', 'SELL')
                        or original is None or matched is None or price is None
                        or not 0 <= matched <= original or not 0 < price < 1):
                    return Acceptance('invalid_identity')
                remaining = original - matched if original is not None and matched is not None and 0 <= matched <= original else None
                kind = row.get('order_type')
                expiration = row.get('expiration')
                order = CurrentOrderFact(key, row['side'], row['status'], _fact(price, os),
                    _fact(original, os), _fact(matched, os), _fact(remaining, os), 'present', None,
                    _fact(kind if kind in ('GTC', 'GTD', 'FOK', 'FAK') else None, os),
                    Fact('known', None, (os,)) if kind in ('GTC', 'FOK', 'FAK') and expiration is None else _fact(expiration, os),
                    1, os)
                if row['status'] in _TERMINAL_ORDER_STATES:
                    proof = TerminalProof(key, row['status'], order.original_size, order.size_matched, os, True)
                    order = replace(order, terminal_proof=proof)
                elif row['status'] not in _CURRENT_ORDER_STATES:
                    order = replace(order, venue_status='UNKNOWN', presence='unknown')
                    orders_complete = False
                previous = self._orders.get(key)
                if previous is None and not orders_source_current:
                    rejected_order_inputs.add(key)
                    continue
                if previous:
                    same = (previous.side, previous.price.value, previous.original_size.value,
                        previous.size_matched.value, previous.order_type.value, previous.expiration.value) == (
                        order.side, order.price.value, order.original_size.value, order.size_matched.value,
                        order.order_type.value, order.expiration.value)
                    if not orders_source_current:
                        order = previous
                    elif previous.terminal_proof is not None:
                        # A fresh confirming terminal row supports its prepared aggregates;
                        # conflicting LIVE/terms still cannot undo the original exact proof.
                        same_terms = (previous.side, previous.price.value, previous.original_size.value,
                            previous.order_type.value, previous.expiration.value) == (
                            order.side, order.price.value, order.original_size.value,
                            order.order_type.value, order.expiration.value)
                        resolves_terminal = (same_terms and previous.terminal_proof.matched.state != 'known'
                            and order.terminal_proof is not None
                            and self._valid_terminal(order.terminal_proof, previous))
                        confirms_terminal = ((same or resolves_terminal) and order.terminal_proof is not None
                            and order.venue_status == previous.venue_status
                            and (previous.source.checked_at is None or os.checked_at >= previous.source.checked_at))
                        if not confirms_terminal:
                            rejected_order_inputs.add(key)
                        order = (replace(order, terms_version=previous.terms_version + 1)
                            if confirms_terminal and resolves_terminal else previous)
                    elif previous.source.checked_at and os.checked_at and os.checked_at < previous.source.checked_at:
                        rejected_order_inputs.add(key)
                        order = previous
                        orders_complete = False
                    else:
                        order = replace(order, terms_version=previous.terms_version + (not same))
                orders[key] = order
            for key, previous in self._orders.items():
                if key not in orders:
                    if (orders_source_current and previous.source.checked_at is not None
                            and os.checked_at < previous.source.checked_at):
                        # A later receipt can be newer than the last whole-component watermark.
                        rejected_order_inputs.add(key)
                        orders[key] = previous
                        orders_complete = False
                    else:
                        orders[key] = (replace(previous, presence='absent' if orders_complete else 'unknown')
                            if orders_source_current else previous)
            if any(r.get('account_id', self.account_id) != self.account_id
                    or not r.get('condition_id') or not r.get('token_id') for r in source_frame.positions):
                return Acceptance('invalid_identity')
            positions = tuple(CurrentPositionFact(self.account_id, r['condition_id'], r['token_id'],
                _fact(_decimal(r.get('quantity')), ps), _fact(_decimal(r.get('cost_usd')), ps),
                _fact(_decimal(r.get('average_price')), ps), _fact(r.get('redeemable'), ps), ps)
                for r in source_frame.positions)
            if any(p.account_id != self.account_id or p.quantity.value is None or p.quantity.value < 0 for p in positions):
                return Acceptance('invalid_identity')
            if not positions_source_current:
                positions = self._positions
            elif not positions_complete:
                incoming = {(p.condition_id, p.token_id): p for p in positions}
                positions = tuple(incoming.values()) + tuple(p for p in self._positions
                    if (p.condition_id, p.token_id) not in incoming)
            if positions_source_current:
                self._source_watermarks['positions'] = ps.checked_at
            if orders_source_current:
                self._source_watermarks['open_orders'] = os.checked_at
            self._orders, self._positions = orders, positions
            yield_sources = tuple(stamp for name, stamp in stamps.items()
                if name not in ('balance', 'positions', 'candidates'))
            yields = {}
            for k, o in orders.items():
                if o.side != 'BUY' or o.terminal_proof is not None or o.presence == 'absent' or o.remaining.value == 0:
                    continue
                previous = self._yields.get(k)
                if k in rejected_order_inputs:
                    # The prepared result belongs to the rejected row, not the retained facts.
                    if previous is not None:
                        yields[k] = previous
                    continue
                value = _fact(_decimal(calculation.yields.get(k[3])) if o.presence == 'present' else None, *yield_sources)
                since = since_at = None
                if value.state != 'known':
                    since = previous.unknown_since_monotonic if previous else None
                    since_at = previous.unknown_since_at if previous else None
                    if since is None and o.presence == 'present':
                        since, since_at = self._clock(), evaluated_at
                yields[k] = BuyYieldState(k, value, yield_sources, evaluated_at, ticket.cycle_generation, since, since_at,
                    previous.current_risk_request_id if previous else None)
            self._yields = yields
            calculation_orders_valid = (orders_complete and not rejected_order_inputs
                and self._financial_source_current(os, ticket))
            calculation_positions_valid = positions_complete and self._financial_source_current(ps, ticket)
            self._reserved = _fact(calculation.buy_reserved if calculation_orders_valid else None, os)
            self._inventory = _fact(calculation.inventory_cost if calculation_positions_valid else None, ps)
            balance = stamps.get('balance', SourceStamp(''))
            calculation_balance_valid = (balance.state == 'known' and balance.complete
                and self._financial_source_current(balance, ticket))
            self._capacity = _fact(calculation.new_capacity if calculation_orders_valid and calculation_positions_valid
                and calculation_balance_valid else None, *stamps.values())
            self._snapshot_version += 1
            view = CurrentOrderView(self.account_id, self.process_epoch, ticket.cycle_generation,
                ticket.observation_id, ticket, self._snapshot_version, source_frame, tuple(o for o in orders.values() if o.presence == 'present' and o.terminal_proof is None),
                orders_complete, os.error_code, os)
            observation = CurrentObservation(self.account_id, self.process_epoch, ticket.cycle_generation,
                ticket.observation_id, ticket, self._snapshot_version, self._utc_now(), source_frame, calculation,
                MappingProxyType(stamps), tuple(orders.values()), self._positions, MappingProxyType(self._yields.copy()),
                orders_complete, positions_complete, view)
            self._issued_tickets = {k: pair for k, pair in self._issued_tickets.items()
                if pair[0].read_sequence > ticket.read_sequence}
            self._accepted_sequence = ticket.read_sequence
            self._observation = observation
            partial = (not calculation_orders_valid or not calculation_positions_valid or not calculation_balance_valid
                or any(s.state != 'known' or not s.complete for s in stamps.values()))
            return Acceptance('partial' if partial else 'accepted', observation)

    def _financial_source_current(self, source: SourceStamp, ticket: ReadTicket) -> bool:
        floor = self._financial_uncertainty_at
        return (floor is None or (ticket.started_at > floor
            and source.checked_at is not None and source.checked_at > floor))

    def apply_protection(self, update: ProtectionUpdate) -> str:
        if type(update) not in (SetBaseline, QueueProgressUpdate, CancelEvidenceUpdate,
                OpeningProtectionUpdate, ScoringObservation, ExitEvidenceUpdate):
            return 'invalid_identity'
        with self._lock:
            g = update.guard
            if not isinstance(g, ProtectionGuard):
                return 'invalid_identity'
            scope = g.scope.scope if isinstance(g.scope, ProtectionKey) else g.scope
            if (not isinstance(scope, tuple) or len(scope) != 4 or scope[0] != self.account_id
                    or not g.event_id or g.expected_scope_version < 0):
                return 'invalid_identity'
            if (g.cycle_generation != self._current.cycle_generation
                    or (g.scope, g.event_id) in self._protection_events
                    or g.expected_scope_version != self._scope_versions.get(g.scope, 0)
                    or (g.observation_id is not None and (self._observation is None
                        or g.observation_id != self._observation.observation_id
                        or self._observation.read_ticket.trade_version_at_start != self._trade_version))):
                return 'stale'
            for key, version in g.expected_order_terms_versions:
                order = self._orders.get(key)
                if key[:3] != scope[:3] or order is None:
                    return 'invalid_identity'
                if order.terms_version != version:
                    return 'stale'
            version = g.expected_scope_version + 1
            if isinstance(update, SetBaseline):
                b = update.baseline
                if (b.key != g.scope or b.baseline_price != b.key.price or b.threshold != Decimal('0.5')
                        or (b.baseline_front.state == 'known' and (b.baseline_front.value is None
                            or b.baseline_front.value < 0 or b.source.state != 'known'))
                        or not b.anchor_order_ids or b.baseline_source not in ('submit', 'account_sync', 'first_observation')
                        or any(not any(k[3] == oid and self._orders[k].side == 'BUY' for k, _ in g.expected_order_terms_versions)
                            for oid in b.anchor_order_ids)):
                    return 'invalid_identity'
                if b.key in self._baselines and self._baselines[b.key].baseline_front.state == 'known':
                    return 'stale'
                self._baselines[b.key] = b
            elif isinstance(update, QueueProgressUpdate):
                p, f = update.progress, update.progress.failure_state
                failure_scope = p.key.scope if p.key.kind == 'queue_bucket' else p.key
                if (p.key != g.scope or f.scope_id != failure_scope or f.data_failures < 0
                        or f.scope_kind != ('queue_group' if p.key.kind == 'queue_bucket' else 'first_seen_episode')
                        or not f.last_evaluation_id):
                    return 'invalid_identity'
                old_failure = self._failures.get(failure_scope)
                if old_failure and f.last_evaluation_id == old_failure.last_evaluation_id:
                    if f != old_failure:
                        return 'stale'
                    f = old_failure
                elif f.scope_version != (old_failure.scope_version if old_failure else 0) + 1:
                    return 'stale'
                if any(not self._valid_cancel(t, scope, g) for t in p.cancel_targets):
                    return 'invalid_identity'
                self._failures[failure_scope] = f
                for key, existing in tuple(self._progress.items()):
                    if existing.failure_state.scope_id == failure_scope:
                        self._progress[key] = replace(existing, failure_state=f)
                self._progress[p.key] = replace(p, scope_version=version, failure_state=f)
            elif isinstance(update, CancelEvidenceUpdate):
                p = self._progress.get(g.scope)
                if p is None or not self._valid_cancel(update.target, scope, g):
                    return 'invalid_identity'
                targets = {t.request_id: t for t in p.cancel_targets}
                previous = targets.get(update.target.request_id)
                if previous and (previous.order_key != update.target.order_key
                        or previous.requested_remaining != update.target.requested_remaining
                        or previous.requested_at != update.target.requested_at or previous.reason != update.target.reason
                        or (previous.native_state == 'ended' and update.target.native_state != 'ended')
                        or (previous.venue_terminal is not None and update.target.venue_terminal != previous.venue_terminal)):
                    return 'invalid_identity'
                targets[update.target.request_id] = update.target
                self._progress[g.scope] = replace(p, cancel_targets=tuple(targets.values()), scope_version=version)
            elif isinstance(update, OpeningProtectionUpdate):
                opening = update.opening
                if (opening.scope != g.scope or opening.economics.scope != opening.scope
                        or opening.scope_version != version
                        or not self._valid_scoring(opening.scoring, scope, g)):
                    return 'invalid_identity'
                previous = self._openings.get(g.scope)
                if previous and previous.stop_loss_latched.value is True:
                    opening = replace(opening, stop_loss_latched=previous.stop_loss_latched,
                        stop_loss_triggered_at=previous.stop_loss_triggered_at,
                        stop_loss_triggered_loss_usd=previous.stop_loss_triggered_loss_usd)
                self._openings[g.scope] = replace(opening, scope_version=version)
            elif isinstance(update, ScoringObservation):
                opening, scoring = self._openings.get(g.scope), update.scoring
                if opening is None or not self._valid_scoring(scoring, scope, g):
                    return 'invalid_identity'
                previous = opening.scoring
                if previous.checked_at and scoring.checked_at and scoring.checked_at < previous.checked_at:
                    return 'stale'
                if (previous.order_key, previous.role) == (scoring.order_key, scoring.role) and scoring.scoring.value is False:
                    scoring = replace(scoring, lost_since_at=previous.lost_since_at or scoring.lost_since_at,
                        lost_since_monotonic=previous.lost_since_monotonic if previous.lost_since_monotonic is not None else scoring.lost_since_monotonic)
                elif scoring.scoring.value is not False:
                    scoring = replace(scoring, lost_since_at=None, lost_since_monotonic=None)
                self._openings[g.scope] = replace(opening, scoring=scoring, scope_version=version)
            elif isinstance(update, ExitEvidenceUpdate):
                sell = update.sell
                if sell.scope != g.scope or any(o.key[:3] != scope[:3] or o.side != 'SELL'
                        or o != self._orders.get(o.key) for o in sell.current_sells):
                    return 'invalid_identity'
                previous = self._sells.get(g.scope)
                own_updates = {}
                for name in ('passive', 'protected'):
                    attempt = getattr(sell, name)
                    old = getattr(previous, name) if previous else None
                    if old and (old.native_state != 'ended' or old.state not in ('terminal', 'rejected')) and (attempt is None or attempt.request_id != old.request_id):
                        return 'stale'
                    if attempt:
                        if (not attempt.request_id or (attempt.order_key is not None and attempt.order_key[:3] != scope[:3])
                                or attempt.native_state not in ('pending', 'running', 'ended')):
                            return 'invalid_identity'
                        if (attempt.submit_quantity.state == 'known' and attempt.submit_residual_quantity.state == 'known'
                                and not 0 <= attempt.submit_quantity.value <= attempt.submit_residual_quantity.value):
                            return 'invalid_identity'
                        same_request = old if old and old.request_id == attempt.request_id else None
                        result, attempt = self._merge_exit_attempt(same_request, attempt)
                        if result != 'applied':
                            return 'invalid_identity'
                        if attempt.own_fill_evidence is not None:
                            result, own = self._merge_own_fill(self._own_fills.get(attempt.order_key),
                                attempt.own_fill_evidence, attempt.order_key)
                            if result != 'applied':
                                return 'invalid_identity'
                            attempt = replace(attempt, own_fill_evidence=own)
                            own_updates[attempt.order_key] = own
                        sell = replace(sell, **{name: attempt})
                self._sells[g.scope] = sell
                self._own_fills.update(own_updates)
            self._scope_versions[g.scope] = version
            self._protection_events.add((g.scope, g.event_id))
            self._snapshot_version += 1
            return 'applied'

    def _merge_exit_attempt(self, previous: ExitAttempt | None,
                            attempt: ExitAttempt) -> tuple[str, ExitAttempt]:
        """Keep exact request terms and actual completion monotonic on both paths."""
        native_rank = {'pending': 0, 'running': 1, 'ended': 2}
        if (attempt.kind not in ('passive_exit', 'protected_exit')
                or attempt.native_state not in native_rank
                or attempt.state not in ('pending', 'unknown', 'accepted_without_order_id', 'accepted', 'rejected', 'terminal')):
            return 'identity_error', attempt
        if previous is not None:
            immutable = ('request_id', 'kind', 'submit_quantity', 'submit_sold_quantity',
                'submit_residual_quantity', 'price', 'order_type', 'expiration')
            if any(getattr(previous, name) != getattr(attempt, name) for name in immutable):
                return 'identity_error', attempt
            if previous.order_key is not None and attempt.order_key != previous.order_key:
                return 'identity_error', attempt
            if (native_rank[attempt.native_state] < native_rank[previous.native_state]
                    or (previous.state in ('terminal', 'rejected') and attempt.state != previous.state)
                    or (previous.state in ('accepted', 'accepted_without_order_id')
                        and attempt.state in ('pending', 'unknown', 'rejected'))
                    or (previous.state == 'accepted' and attempt.state == 'accepted_without_order_id')):
                return 'stale_evidence', attempt
        own = attempt.own_fill_evidence
        old_own = previous.own_fill_evidence if previous else None
        if own is not None:
            result, own = self._merge_own_fill(old_own, own, attempt.order_key)
            if result != 'applied':
                return result, attempt
        elif old_own is not None:
            own = old_own
        if (own is not None and own.quantity.state == attempt.submit_quantity.state == 'known'
                and own.quantity.value > attempt.submit_quantity.value):
            return 'identity_error', attempt
        attempt = replace(attempt, own_fill_evidence=own)
        if previous and previous.cancel_request_id and attempt.cancel_request_id is None:
            attempt = replace(attempt, cancel_request_id=previous.cancel_request_id)
        return 'applied', attempt

    def _merge_own_fill(self, previous: OwnFillEvidence | None,
                        own: OwnFillEvidence, order_key: OrderKey) -> tuple[str, OwnFillEvidence]:
        if (order_key is None or own.order_key != order_key or not own.fill_ids or len(set(own.fill_ids)) != len(own.fill_ids)
                or (own.quantity.state == 'known' and (own.quantity.value is None or own.quantity.value < 0))):
            return 'identity_error', own
        if previous is not None:
            if (not set(previous.fill_ids) <= set(own.fill_ids)
                    or (previous.quantity.state == own.quantity.state == 'known'
                        and own.quantity.value < previous.quantity.value)):
                return 'stale_evidence', own
            same_coverage = (set(previous.fill_ids) == set(own.fill_ids)
                and (previous.quantity.state != 'known' or own.quantity.state != 'known'
                    or previous.quantity.value == own.quantity.value))
            if same_coverage:
                # These amounts prove exactly this covered set, never an expanded total.
                retained = {name: getattr(previous, name) for name in ('quantity', 'notional_usd', 'fees_usd')
                    if getattr(previous, name).state == 'known' and getattr(own, name).state != 'known'}
                own = replace(own, **retained,
                    complete_for_target=own.complete_for_target or previous.complete_for_target)
        return 'applied', own

    def _valid_scoring(self, scoring, scope, guard):
        if scoring.order_key is None:
            return scoring.role is None and scoring.scoring.state != 'known'
        return (scoring.order_key[:3] == scope[:3]
            and scoring.order_key in dict(guard.expected_order_terms_versions)
            and scoring.role in ('entry', 'passive_exit', 'protected_exit'))

    def _valid_cancel(self, target, scope, guard):
        order = self._orders.get(target.order_key)
        if (not target.request_id or order is None or order.side != 'BUY'
                or target.order_key[:3] != scope[:3] or target.order_key not in dict(guard.expected_order_terms_versions)):
            return False
        if target.venue_terminal and not self._valid_terminal(target.venue_terminal, order):
            return False
        # A local ACK is not evidence of canceled quantity or a terminal venue state.
        if target.canceled_remaining.state == 'known' and target.canceled_remaining.value > 0 and target.venue_terminal is None:
            return False
        if target.native_state not in ('pending', 'running', 'ended'):
            return False
        return True

    def _valid_terminal(self, proof: TerminalProof, order: CurrentOrderFact) -> bool:
        if (not isinstance(proof, TerminalProof) or not proof.identity_verified
                or proof.order_key != order.key or proof.status not in _TERMINAL_ORDER_STATES
                or proof.original_size.state != 'known' or proof.original_size.value != order.original_size.value
                or proof.source.state != 'known' or proof.source.checked_at is None
                or (order.source.checked_at is not None and proof.source.checked_at < order.source.checked_at)):
            return False
        previous = order.terminal_proof
        if previous is not None and (proof.status != previous.status
                or (previous.matched.state == 'known' and proof.matched.state != 'known')):
            return False
        if proof.matched.state == 'known':
            if proof.matched.value is None or not 0 <= proof.matched.value <= proof.original_size.value:
                return False
            if order.size_matched.state == 'known' and proof.matched.value < order.size_matched.value:
                return False
        if proof.status in ('FILLED', 'MATCHED') and proof.matched.value != proof.original_size.value:
            return False
        return True

    def accept_buy_execution_uncertainty(self, evidence: BuyExecutionUncertainty) -> str:
        with self._lock:
            if (not isinstance(evidence, BuyExecutionUncertainty)
                    or evidence.account_id != self.account_id or evidence.process_epoch != self.process_epoch
                    or not any(generation == evidence.cycle_generation
                        for _, generation in self._applied_transitions.values())):
                return 'identity_error'
            identities = (evidence.event_id, evidence.request_id, evidence.account_id, evidence.process_epoch,
                evidence.cycle_generation, evidence.batch_id, evidence.observation_id, evidence.venue,
                evidence.condition_id, evidence.token_id)
            if (any(not isinstance(value, str) or not value.strip() for value in identities)
                    or type(evidence.round_number) is not int or evidence.round_number < 0
                    or not isinstance(evidence.price, Decimal) or not evidence.price.is_finite()
                    or not 0 < evidence.price < 1
                    or not isinstance(evidence.quantity, Decimal) or not evidence.quantity.is_finite()
                    or evidence.quantity <= 0 or evidence.order_type not in ('GTC', 'GTD', 'FOK', 'FAK')
                    or (evidence.order_type == 'GTD' and (type(evidence.expiration) is not int or evidence.expiration < 0))
                    or (evidence.order_type != 'GTD' and evidence.expiration is not None)
                    or type(evidence.post_only) is not bool
                    or evidence.reason not in ('unknown_timeout', 'unknown_error', 'invalid_receipt')
                    or (evidence.venue_order_id is not None and (not isinstance(evidence.venue_order_id, str)
                        or not evidence.venue_order_id.strip()))):
                return 'identity_error'
            times = (evidence.native_started_at, evidence.observed_at)
            if (any(not isinstance(value, datetime) or value.utcoffset() is None
                    or value.utcoffset().total_seconds() != 0 for value in times)
                    or evidence.observed_at < evidence.native_started_at):
                return 'identity_error'
            options = evidence.send_options
            if (not isinstance(options, tuple) or any(not isinstance(option, tuple) or len(option) != 2
                    or not isinstance(option[0], str) or not option[0].strip()
                    or type(option[1]) not in (str, int, bool, type(None)) for option in options)
                    or len({option[0] for option in options}) != len(options)):
                return 'identity_error'
            # Keep only immutable request identity for process-local replay protection,
            # never a second workflow or an invented order/venue observation.
            identity = (evidence.account_id, evidence.process_epoch, evidence.cycle_generation,
                evidence.batch_id, evidence.round_number, evidence.observation_id, evidence.venue,
                evidence.condition_id, evidence.token_id, evidence.price, evidence.quantity,
                evidence.order_type, evidence.expiration, evidence.post_only, tuple(sorted(options)),
                evidence.native_started_at)
            event_identity = (evidence.request_id, identity)
            previous_event = self._uncertain_buy_events.get(evidence.event_id)
            previous_request = self._uncertain_buy_requests.get(evidence.request_id)
            if ((previous_event is not None and previous_event != event_identity)
                    or (previous_request is not None and previous_request != identity)):
                return 'identity_error'
            if previous_event is not None or previous_request is not None:
                return 'stale_evidence'
            self._uncertain_buy_events[evidence.event_id] = event_identity
            self._uncertain_buy_requests[evidence.request_id] = identity
            self._financial_uncertainty_at = (evidence.observed_at if self._financial_uncertainty_at is None
                else max(self._financial_uncertainty_at, evidence.observed_at))
            self._reserved = Fact('unknown', sources=self._reserved.sources, reason_codes=('buy_execution_uncertain',))
            self._inventory = Fact('unknown', sources=self._inventory.sources, reason_codes=('buy_execution_uncertain',))
            self._capacity = Fact('unknown', sources=self._capacity.sources, reason_codes=('buy_execution_uncertain',))
            self._trade_version += 1
            self._snapshot_version += 1
            return 'applied'

    def accept_receipt_evidence(self, evidence: ReceiptEvidence) -> str:
        if not isinstance(evidence, ReceiptEvidence):
            return 'identity_error'
        with self._lock:
            if (not evidence.event_id or not evidence.request_id or evidence.account_id != self.account_id
                    or evidence.process_epoch != self.process_epoch or evidence.order_key[0] != self.account_id):
                return 'identity_error'
            if evidence.event_id in self._receipt_events:
                return 'stale_evidence'
            order = self._orders.get(evidence.order_key)
            incoming = evidence.order_fact
            if incoming is not None:
                if (incoming.key != evidence.order_key or incoming.side != evidence.side
                        or incoming.price.state != 'known' or incoming.original_size.state != 'known'
                        or incoming.size_matched.state != 'known' or incoming.remaining.state != 'known'
                        or not 0 < incoming.price.value < 1
                        or not 0 <= incoming.size_matched.value <= incoming.original_size.value
                        or incoming.remaining.value != incoming.original_size.value - incoming.size_matched.value):
                    return 'identity_error'
                if order is not None and (incoming.price.value != order.price.value
                        or incoming.original_size.value != order.original_size.value or incoming.side != order.side):
                    return 'identity_error'
                if order is not None and (order.terminal_proof is not None
                        or incoming.size_matched.value < order.size_matched.value):
                    return 'stale_evidence'
                order = replace(incoming, terms_version=order.terms_version + 1 if order else 1)
            if order is None or order.side != evidence.side:
                return 'identity_error'
            if evidence.expected_terms_version > order.terms_version:
                return 'stale_evidence'
            proof, own = evidence.terminal_proof, evidence.own_fill
            if proof is not None and not self._valid_terminal(proof, order):
                return 'identity_error'
            if own is not None:
                if own.quantity.state != 'known' or own.quantity.value is None:
                    return 'identity_error'
                result, own = self._merge_own_fill(self._own_fills.get(order.key), own, order.key)
                if result != 'applied':
                    return result
                if own.quantity.state == 'known' and own.quantity.value > order.original_size.value:
                    return 'identity_error'
            progress = self._progress.get(evidence.scope)
            target = evidence.cancel_target
            if target is not None:
                previous = next((t for t in progress.cancel_targets if t.request_id == evidence.request_id), None) if progress else None
                if (previous is None or target.request_id != previous.request_id or target.order_key != previous.order_key
                        or target.order_key != evidence.order_key or target.requested_remaining != previous.requested_remaining
                        or (target.venue_terminal and not self._valid_terminal(target.venue_terminal, order))
                        or (target.canceled_remaining.state == 'known' and target.canceled_remaining.value > 0
                            and target.venue_terminal is None)):
                    return 'identity_error'
            sell = self._sells.get(evidence.scope)
            attempt = evidence.exit_attempt
            attempt_name = None
            if attempt is None and own is not None and sell is not None:
                for name in ('passive', 'protected'):
                    current_attempt = getattr(sell, name)
                    if (current_attempt is not None and current_attempt.request_id == evidence.request_id
                            and current_attempt.order_key == order.key):
                        attempt = replace(current_attempt, own_fill_evidence=own)
                        break
            if attempt is not None:
                attempt_name = 'passive' if attempt.kind == 'passive_exit' else 'protected'
                previous = getattr(sell, attempt_name) if sell else None
                if (previous is None or attempt.request_id != evidence.request_id or previous.request_id != attempt.request_id
                        or (previous.order_key is not None and attempt.order_key != previous.order_key)
                        or attempt.order_key != evidence.order_key or attempt.submit_quantity != previous.submit_quantity
                        or attempt.order_type != previous.order_type or attempt.expiration != previous.expiration):
                    return 'identity_error'
                if own is not None:
                    if (evidence.exit_attempt is not None and attempt.own_fill_evidence is not None
                            and attempt.own_fill_evidence != evidence.own_fill):
                        return 'identity_error'
                    attempt = replace(attempt, own_fill_evidence=own)
                result, attempt = self._merge_exit_attempt(previous, attempt)
                if result != 'applied':
                    return result
                if attempt.own_fill_evidence is not None:
                    result, own = self._merge_own_fill(self._own_fills.get(order.key), attempt.own_fill_evidence, order.key)
                    if result != 'applied':
                        return result
                    if own.quantity.state == 'known' and own.quantity.value > order.original_size.value:
                        return 'identity_error'
                    attempt = replace(attempt, own_fill_evidence=own)
            if proof is None and own is None and incoming is None and target is None and attempt is None:
                return 'identity_error'
            if proof is not None:
                matched = proof.matched if proof.matched.state == 'known' else order.size_matched
                remaining = (_fact(order.original_size.value - matched.value, proof.source)
                    if proof.matched.state == 'known' else _fact(None, proof.source))
                order = replace(order, terminal_proof=proof, venue_status=proof.status,
                    size_matched=matched, remaining=remaining, source=proof.source, terms_version=order.terms_version + 1)
                self._yields.pop(order.key, None)
            self._orders[order.key] = order
            if own is not None:
                self._own_fills[order.key] = own
            if target is not None:
                scope_version = self._scope_versions.get(evidence.scope, 0) + 1
                self._progress[evidence.scope] = replace(progress, scope_version=scope_version, cancel_targets=tuple(
                    target if t.request_id == target.request_id else t for t in progress.cancel_targets))
                self._scope_versions[evidence.scope] = scope_version
            if attempt is not None:
                self._sells[evidence.scope] = replace(sell, **{attempt_name: attempt})
                self._scope_versions[evidence.scope] = self._scope_versions.get(evidence.scope, 0) + 1
            self._receipt_events.add(evidence.event_id)
            self._trade_version += 1
            self._snapshot_version += 1
            # A receipt invalidates admission from the old financial observation.
            # It does not re-stamp that observation or invent new balance/position proof.
            active = tuple(o for o in self._orders.values() if o.side == 'BUY'
                and o.terminal_proof is None and o.presence == 'present')
            if self._reserved.state == 'known' and all(o.remaining.state == o.price.state == 'known' for o in active):
                self._reserved = _fact(sum((o.remaining.value * o.price.value for o in active), Decimal(0)),
                    *(o.source for o in active))
            else:
                self._reserved = Fact('unknown', sources=self._reserved.sources)
            self._capacity = Fact('unknown', sources=self._capacity.sources, reason_codes=('receipt_invalidated',))
            return 'applied'

    def snapshot(self) -> CurrentSnapshot:
        with self._lock:
            exposure = (_fact(self._reserved.value + self._inventory.value,
                *self._reserved.sources, *self._inventory.sources)
                if self._reserved.state == self._inventory.state == 'known' else Fact('unknown'))
            return CurrentSnapshot(self._current, self._snapshot_version, self._trade_version, self._observation,
                tuple(self._orders.values()), self._positions, MappingProxyType(self._yields.copy()),
                self._reserved, self._inventory, exposure, self._capacity, MappingProxyType(self._own_fills.copy()),
                tuple(k for k, y in self._yields.items() if y.unknown_deadline_monotonic is not None
                    and self._clock() >= y.unknown_deadline_monotonic),
                MappingProxyType(self._baselines.copy()), MappingProxyType(self._progress.copy()),
                MappingProxyType(self._failures.copy()), MappingProxyType(self._openings.copy()),
                MappingProxyType(self._sells.copy()), MappingProxyType(self._scope_versions.copy()))
