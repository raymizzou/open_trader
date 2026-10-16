"""Explicitly constructed D2 composition; no default runtime activation."""
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from collections.abc import Mapping
import json
import hashlib
import threading
import time
from concurrent.futures import Future
from uuid import uuid4

from .lp_business_writer import LPBusinessWriter, RequestRecord, ReceiptRecord, ProgressRecord
from .polymarket_lp_control import LPControlOwner
from .polymarket_lp_current_inputs import prepare_current_inputs
from .polymarket_lp_current_state import (
    AppliedControl, CommittedControl, CurrentLPState, StoredControl, ValidatedStartAdmission,
    ReceiptEvidence, SourceStamp, TerminalProof, CurrentOrderFact, Fact,
    BuyExecutionUncertainty,
    ProtectionKey, ProtectionGuard, ProtectionBaseline, SetBaseline,
    ProtectionProgress, ProtectionFailureState, QueueProgressUpdate,
    CancelTargetState, CancelEvidenceUpdate,
    OpeningProtection, OpeningEconomics, OpeningProtectionUpdate,
    ScoringState, ScoringObservation,
    OwnFillEvidence, SellState, ExitAttempt, ExitEvidenceUpdate,
)
from .polymarket_lp_market_calculation import calculate_current
from .polymarket_lp_market_reader import MarketReader, ReadScope
from .polymarket_lp_risk import (estimate_lp_queue_position, first_observation_baseline,
    _executable_bid_value, _projected_taker_fee, _freshness, _levels, TERMINAL_ORDER_STATES)
from .polymarket_lp import SCORING_POLL_SECONDS, SCORING_STALE_SECONDS, SCORING_FAILURE_WINDOW_SECONDS, STOP_LOSS
from .polymarket_lp_round import (
    LPRoundCoordinator, CycleStatus, FixedBuyPlan, StartStatus, CancelTarget,
    LocalOutcome, LocalStatus, BuyTarget, BuyTerms, ActionIdentity,
)


@dataclass(frozen=True)
class _Startup:
    account_id: str
    transition_id: str
    control_version: int
    validated: bool
    fatal: bool


@dataclass(frozen=True)
class _Transition:
    account_id: str
    transition_id: str
    control_version: int
    previous_running: bool
    effective_running: bool
    cause: str
    admitted: bool


@dataclass(frozen=True)
class RunResult:
    acceptance: object
    start: object = None
    finish: object = None
    outcomes: tuple = ()


@dataclass(frozen=True)
class _Control:
    account_id: str
    transition_id: str
    control_version: int
    cycle_generation: str
    effective_running: bool
    fatal: bool


@dataclass(frozen=True)
class _Order:
    account_id: str
    venue: str
    token_id: str
    order_id: str
    side: str


@dataclass(frozen=True)
class _Orders:
    account_id: str
    cycle_generation: str
    observation_id: str
    accepted: bool
    orders_current: bool
    orders_complete: bool
    orders_error: str | None
    open_orders: tuple


@dataclass(frozen=True)
class _IndependentContext:
    cycle_generation: str
    batch_id: str
    round_number: int
    observation_id: str
    protection_key: object = None


@dataclass(frozen=True)
class NativeAttempt:
    identity: ActionIdentity
    _future: Future
    _thread: threading.Thread

    def done(self):
        return self._future.done() and not self._thread.is_alive()

    def result(self, timeout=None):
        return self._future.result(timeout=timeout)


@dataclass(frozen=True)
class _ProtectionAction:
    request_id: str
    account_id: str
    scope: tuple
    kind: str


@dataclass(frozen=True)
class _ProtectionOutcome:
    request_id: str
    status: str
    order_id: str | None


@dataclass(frozen=True)
class _OpeningOrderAssociation:
    scope: tuple
    order_key: tuple
    request_id: str | None
    observation_id: str
    source: SourceStamp
    requested_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class _SellCalculationOrigin:
    guard: ProtectionGuard
    trade_version: int
    residual: Decimal
    sold: Decimal


@dataclass(frozen=True)
class _DeferredProtectionCancel:
    order: CurrentOrderFact
    trigger_observation_id: str
    reason: str


class _ControlStore:
    def __init__(self, store, account_id):
        self.store = store
        self.account_id = account_id
        # The legacy pool persists a normalized wallet hash. Current API facts
        # and the state owner retain the wallet itself as their account identity.
        self.pool_account_id = hashlib.sha256(account_id.strip().casefold().encode('utf-8')).hexdigest()

    def load(self, account_id):
        if account_id != self.account_id:
            raise ValueError('d2_control_account_identity')
        saved = dict(self.store.lp_load_current_control(self.pool_account_id))
        if saved['account_id'] != self.pool_account_id:
            raise ValueError('d2_control_pool_identity')
        return StoredControl(**{**saved, 'account_id': self.account_id})

    def save(self, write):
        if write.account_id != self.account_id:
            raise ValueError('d2_control_account_identity')
        return CommittedControl(**self.store.lp_save_current_control({
            **asdict(write), 'account_id': self.pool_account_id}))


def _transition_id(value):
    # JSON tuple encoding preserves epoch delimiters and integer identity.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class LPD2LiveExecution:
    def __init__(self, store, trading, *, account_id, venue, process_epoch, policy,
                 reader=None, writer=None, clock=time.monotonic,
                 utc_now=lambda: datetime.now(UTC)):
        if (venue != 'polymarket' or type(account_id) is not str or not account_id
                or account_id != trading.config.wallet_address
                or type(process_epoch) is not str or not process_epoch):
            raise ValueError('d2_transport_identity')
        self.store, self.trading, self.account_id, self.venue = store, trading, account_id, venue
        self.process_epoch, self.policy = process_epoch, policy
        self._clock, self._utc_now = clock, utc_now
        self._positions_full_coverage = reader is None  # owned adapter requests threshold0
        self.reader = reader if reader is not None else MarketReader(trading.lp_reader_source_port(), clock=clock)
        self.writer = writer if writer is not None else LPBusinessWriter(store)
        self.state = CurrentLPState(account_id, process_epoch, clock=clock, utc_now=utc_now)
        self.rounds = LPRoundCoordinator()
        self.control = LPControlOwner(self.state, _ControlStore(store, account_id), self, self,
                                      clock=clock, utc_now=utc_now)
        self._run_lock = threading.Lock()
        self._protection_lock = threading.Lock()
        self._closed = False
        self._admission = None
        self._record_requests = {}
        # Immutable current-process ownership/provenance only. All business
        # quantities, statuses and latches remain exclusively in CurrentLPState.
        self._opening_order_associations = {}
        self._sealed_candidates = {}
        self._sealed_candidate_source = None
        self._native_jobs = {}
        # Finite dispatch tickets for an already accepted protection trigger.
        # No request/native attempt exists until actual capacity is admitted.
        self._deferred_protection_cancels = {}
        self._active_sends = set()
        self._last_preparation_rejection = None
        self._last_current_evidence = None
        self._last_buy_uncertainty = None

    def initialize(self, admission):
        self._admission = admission
        return self.control.initialize(admission)

    def apply(self, transition):
        """LPControlOwner activation callback; publish both peer owners once."""
        current = self.state.snapshot().current
        identity = _transition_id(transition.transition_id)
        if transition.cause == 'startup':
            validated = (isinstance(self._admission, ValidatedStartAdmission)
                and self._admission.process_epoch == transition.process_epoch == self.process_epoch
                and bool(self._admission.permit_claim_id) and transition.account_id == self.account_id
                and transition.control_version == 0 and current.control_version is None)
            update = self.rounds.initialize(_Startup(transition.account_id, identity,
                transition.control_version, validated, current.fatal_id is not None), process_epoch=self.process_epoch)
        else:
            status = self.control.status()
            admitted = (transition.account_id == current.account_id
                and transition.process_epoch == current.process_epoch
                and transition.previous_control_version == current.control_version
                and transition.previous_running is current.effective_running
                and transition.control_version == status.requested_version
                and status.fatal is None and not current.shutdown_requested)
            update = self.rounds.apply_control(_Transition(transition.account_id, identity,
                transition.control_version, transition.previous_running, transition.effective_running,
                transition.cause, admitted))
        if update.status not in (CycleStatus.INITIALIZED, CycleStatus.APPLIED):
            raise ValueError('d2_control_not_applied')
        self.state.apply_effective_control(transition, update.cycle_generation)
        if update.reset_provider_wait:
            self.reader.reset_provider_wait()
        return AppliedControl(update.cycle_generation)

    def trip(self, signal):
        self._closed = True

    def request_running(self, running, *, actor=None):
        return self.control.request_running(running, actor=actor)

    def poll(self):
        return self.control.poll(self._clock())

    def run_once(self, captured_candidates=None, *, timeout=2):
        if self._closed:
            raise RuntimeError('d2_closed')
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError('d2_run_in_progress')
        try:
            self.poll()
            current = self.state.snapshot().current
            ticket = self.state.begin_observation(current.cycle_generation, ('balance', 'open_orders', 'positions'))
            frame = self.reader.read_current(ReadScope(self.account_id, related_markets=True), time.monotonic()+timeout)
            as_of = self._utc_now()
            prepared = prepare_current_inputs(frame, captured_candidates, self.policy, as_of)
            calculation = calculate_current(prepared, self.policy, as_of)
            accepted = self.state.accept(ticket, frame, calculation, evaluated_at=as_of)
            if accepted.observation is None:
                return RunResult(accepted)
            observation = accepted.observation
            source = observation.order_view.orders_source
            orders_current = (accepted.status in ('accepted', 'partial')
                and observation.read_ticket == ticket and source.state == 'known'
                and source.source_id == frame.sources['open_orders'].source_id
                and source.checked_at is not None and source.checked_at >= ticket.started_at
                and all(r.get('venue', self.venue) == self.venue for r in frame.orders))
            orders = _Orders(self.account_id, ticket.cycle_generation, ticket.observation_id,
                accepted.status in ('accepted', 'partial'), orders_current,
                observation.orders_complete, source.error_code, tuple(
                    _Order(o.key[0], self.venue, o.key[2], o.key[3], o.side)
                    for o in observation.order_view.open_orders))
            if orders_current and observation.orders_complete:
                self._associate_current_manual_sells()
                self._offer_progress(frame)
            fixed = self._fixed_plan(observation, prepared, calculation)
            # LPControlOwner uses this same peer coordination lock while its
            # activation callback publishes round then state. No native I/O is
            # performed under it; seal/finish cannot race control application.
            with self.state._lock:
                start = self.rounds.begin_round(self._control_view(), orders, fixed_buys=fixed)
            if start.status != StartStatus.BATCH:
                return RunResult(accepted, start)
            self._sealed_candidates = {(start.token.batch_id, a.request_id): row
                for a in start.token.actions if isinstance(a.target, BuyTarget)
                for row in prepared.sources['candidates'].value
                if row['token_id'] == a.target.token_id and row.get('market_id') == a.target.market_id}
            self._sealed_candidate_source = prepared.sources['candidates']
            outcomes = tuple(self._attempt(start.token, action, observation) for action in start.token.actions)
            finish = self.complete_local_attempts(start.token, outcomes)
            self._sealed_candidates.clear()
            self._sealed_candidate_source = None
            return RunResult(accepted, start, finish, outcomes)
        finally:
            self._run_lock.release()

    def _offer_progress(self, frame):
        # In-process logging identities only: never restore request history or
        # guess a venue ID for a BUY whose receipt was missing.
        openings = self.state.snapshot().openings.values()
        retained = {key: record for key, record in self._record_requests.items()
            if record.kind == 'BUY' and any(o.phase != 'closed' and key[:3] == o.scope[:3]
                and key[3] in o.entry_order_ids for o in openings)}
        for row in frame.orders:
            key = (frame.account, row['condition_id'], row['token_id'], row['order_id'])
            record = self._record_requests.get(key)
            if record is None or row['side'] != 'BUY':
                continue
            if row['price'] != record.price or Decimal(str(row['original_size'])) != record.quantity:
                continue
            self.writer.offer(ProgressRecord(record.account_id, record.process_epoch, record.request_id,
                record.session_id, record.condition_id, record.token_id, record.order_id,
                Decimal(str(row['original_size'])), Decimal(str(row['size_matched'])), row['price'], row['status'],
                frame.sources['open_orders'].checked_at))
            if row['status'] in ('LIVE', 'OPEN', 'ACCEPTED', 'PARTIALLY_FILLED', 'CANCELING', 'CANCELLING', 'PENDING_CANCEL'):
                retained[key] = record
        self._record_requests = retained

    def _control_view(self):
        current = self.state.snapshot().current
        return _Control(current.account_id, _transition_id(current.transition_id), current.control_version,
            current.cycle_generation, current.effective_running,
            current.fatal_id is not None or current.shutdown_requested or self._closed)

    def _fixed_plan(self, observation, prepared, calculation):
        source = prepared.sources['candidates']
        if (not source.complete or source.error or source.value is None
                or self.state.snapshot().new_capacity.state != 'known'):
            return None
        if not isinstance(source.value, tuple) or any(not isinstance(row, Mapping) for row in source.value):
            return None
        targets = []
        for token in calculation.target_tokens:
            rows = [r for r in source.value if not r.get('existing') and r.get('token_id') == token]
            if not rows:
                continue  # retained existing BUY is not a new send
            if len(rows) != 1:
                return None  # ambiguous identity is not an empty fixed plan
            row = rows[0]
            if any(o.phase != 'closed' and o.stop_requested and o.scope[1:3] == (row.get('condition_id'), row.get('token_id'))
                    for o in self.state.snapshot().openings.values()):
                continue
            try:
                if (any(type(row.get(key)) is not str or not row[key]
                        for key in ('condition_id', 'token_id', 'market_id'))
                        or type(row.get('venue', self.venue)) is not str
                        or row.get('account_id', self.account_id) != self.account_id):
                    return None
                terms = BuyTerms(Decimal(str(row['price'])), Decimal(str(row['quantity'])),
                    row['order_type'], row.get('expiration'), row['post_only'], tuple(row.get('send_options', ())))
                targets.append(BuyTarget(row.get('venue', self.venue), row['token_id'], row['market_id'], terms))
            except (KeyError, TypeError, ValueError):
                return None
        return FixedBuyPlan(self.account_id, observation.cycle_generation,
            observation.observation_id, observation.observation_id + ':fixed', tuple(targets))

    def complete_local_attempts(self, token, outcomes):
        with self.state._lock:
            return self.rounds.finish_local_attempts(self._control_view(), token, outcomes)

    def _attempt(self, token, action, observation, *, cancel_order=None):
        target = action.target
        if isinstance(target, CancelTarget):
            order = cancel_order if cancel_order is not None else next(
                o for o in observation.orders if o.key[3] == target.existing_order_id
                and o.key[2] == target.token_id)
            condition, price, quantity = order.key[1], order.price.value, order.original_size.value
            order_id, kind = order.key[3], 'CANCEL'
            order_type, expiration, post_only, options = order.order_type.value, None, None, ()
        else:
            candidate = self._sealed_candidates[(token.batch_id, action.request_id)]
            condition, price, quantity = candidate['condition_id'], target.terms.price, target.terms.quantity
            order_id, kind = None, 'BUY'
            order_type, expiration = target.terms.order_type, target.terms.expiration
            post_only, options = target.terms.post_only, target.terms.send_options
        record = RequestRecord(self.account_id, self.process_epoch, action.request_id,
            'd2:' + action.request_id, condition, target.token_id, kind, price, quantity,
            self._utc_now(), order_id, target.venue, token.cycle_generation, token.batch_id,
            token.round_number, token.observation_id, order_type, expiration, post_only, options)
        if order_id:
            self._record_requests[(self.account_id, condition, target.token_id, order_id)] = record
        self.writer.offer(record)
        with self.state._lock:
            control = self._control_view()
            protection = isinstance(token, _IndependentContext) and token.protection_key is not None
            permitted = ((control.effective_running or protection) and not control.fatal
                and control.cycle_generation == token.cycle_generation and action.account_id == self.account_id
                and target.venue == self.venue)
            owned = {h.identity.request_id for h in self._native_jobs.values() if h._thread.is_alive()}
            occupied = (owned | self._active_sends) - {action.request_id}
            if isinstance(target, CancelTarget):
                existing = self._native_jobs.get((self.account_id, target.venue, target.token_id, target.existing_order_id))
                permitted = permitted and not (existing and existing._thread.is_alive()
                    and existing.identity.request_id != action.request_id)
                if not isinstance(token, _IndependentContext):
                    # Resume cleanup cannot steal a deferred protection target
                    # or replay an already attempted (including UNKNOWN) one.
                    permitted = permitted and not self._protection_cancel_owned(order.key)
            permitted = permitted and len(occupied) < 2
            if permitted:
                self._active_sends.add(action.request_id)
        reason = self._preparation_error(target) if isinstance(target, BuyTarget) else None
        if not permitted or reason:
            reason = reason or 'control_or_native_not_current'
            self._last_preparation_rejection = (action.request_id, reason)
            with self.state._lock:
                self._active_sends.discard(action.request_id)
            outcome = LocalOutcome(action, LocalStatus.NOT_SENT, True)
        else:
            try:
                outcome = self._cancel(action, observation, order, protection=protection) if isinstance(target, CancelTarget) else self._buy(token, action, record)
            finally:
                with self.state._lock:
                    self._active_sends.discard(action.request_id)
        self.writer.offer(ReceiptRecord(self.account_id, self.process_epoch, action.request_id,
            record.session_id, record.condition_id, record.token_id, outcome.venue_order_id or record.order_id,
            'accepted' if outcome.status == LocalStatus.ACCEPTED else
            'not_sent' if outcome.status == LocalStatus.NOT_SENT else
            'rejected' if outcome.status == LocalStatus.REJECTED else 'unknown',
            self._utc_now(), reason))
        if isinstance(token, _IndependentContext) and token.protection_key is not None:
            self._finish_protection_cancel(token.protection_key, outcome)
        return outcome

    @staticmethod
    def _preparation_error(target):
        terms = target.terms
        if (terms.order_type not in ('GTC', 'GTD') or terms.post_only is not True
                or (terms.order_type == 'GTC') != (terms.expiration is None)
                or terms.expiration is not None and (type(terms.expiration) is not int or terms.expiration < 0)):
            return 'unsupported_order_shape'
        names = [key for key, _ in terms.send_options]
        if len(set(names)) != len(names):
            return 'ambiguous_send_options'
        if any(key != 'builder_code' for key in names):
            return 'unsupported_send_option'
        if any(value is not None and type(value) is not str for _, value in terms.send_options):
            return 'invalid_builder_code'
        return None

    def _uncertain_buy(self, token, action, record, native_started_at, reason,
                       venue_order_id=None, receipt_status=None):
        terms = action.target.terms
        evidence = BuyExecutionUncertainty(
            event_id=action.request_id + ':buy_uncertain', request_id=action.request_id,
            account_id=record.account_id, process_epoch=record.process_epoch,
            cycle_generation=token.cycle_generation, batch_id=token.batch_id,
            round_number=token.round_number, observation_id=token.observation_id,
            venue=action.target.venue, condition_id=record.condition_id, token_id=record.token_id,
            price=terms.price, quantity=terms.quantity, order_type=terms.order_type,
            expiration=terms.expiration, post_only=terms.post_only, send_options=terms.send_options,
            native_started_at=native_started_at, observed_at=self._utc_now(), reason=reason,
            venue_order_id=venue_order_id)
        disposition = self.state.accept_buy_execution_uncertainty(evidence)
        self._last_buy_uncertainty = evidence
        self._last_current_evidence = (action.request_id, 'buy_uncertainty', disposition)
        if disposition not in ('applied', 'stale_evidence'):
            # Continuing with known financial authority would be unsafe if the
            # sole current owner rejected this actual execution uncertainty.
            self._closed = True
            raise RuntimeError('d2_buy_uncertainty_not_applied:' + str(disposition))
        return LocalOutcome(action, LocalStatus.UNKNOWN_TIMEOUT if reason == 'unknown_timeout'
            else LocalStatus.UNKNOWN_ERROR, True, venue_order_id, receipt_status)

    def _buy(self, token, action, record):
        terms = action.target.terms
        try:
            signed = self.trading.lp_create_limit_order(token_id=record.token_id, price=terms.price,
                quantity=terms.quantity, side='BUY', post_only=terms.post_only, expiration=terms.expiration,
                **dict(terms.send_options))
        except Exception:
            return LocalOutcome(action, LocalStatus.NOT_SENT, True)
        with self.state._lock:
            current = self._control_view()
            if not current.effective_running or current.fatal or current.cycle_generation != token.cycle_generation:
                return LocalOutcome(action, LocalStatus.NOT_SENT, True)
        native_started_at = self._utc_now()
        try:
            response = self.trading.lp_post_order(signed)
        except TimeoutError:
            return self._uncertain_buy(token, action, record, native_started_at, 'unknown_timeout')
        except Exception:
            return self._uncertain_buy(token, action, record, native_started_at, 'unknown_error')
        def value(name, default=None):
            return response.get(name, default) if isinstance(response, Mapping) else getattr(response, name, default)
        for name, expected in (('account_id', self.account_id), ('venue', self.venue),
                ('token_id', record.token_id), ('request_id', action.request_id)):
            if value(name, expected) != expected:
                return self._uncertain_buy(token, action, record, native_started_at, 'invalid_receipt')
        status = str(value('status', '')).upper()
        venue_id = value('order_id', value('id')) or None
        if venue_id is not None and (type(venue_id) is not str or not venue_id):
            return self._uncertain_buy(token, action, record, native_started_at, 'invalid_receipt')
        if value('accepted') is False or value('ok') is False or status in ('REJECTED', 'FAILED'):
            return LocalOutcome(action, LocalStatus.REJECTED, True, venue_id, status)
        if not (value('accepted') is True or venue_id or status in ('LIVE', 'OPEN', 'ACCEPTED', 'MATCHED', 'FILLED')):
            return self._uncertain_buy(token, action, record, native_started_at, 'invalid_receipt', venue_id, status)
        # SDK BUY taking_amount is returned filled shares, not making_amount
        # (cash) or the signed order's intended maker/taker quantities. Older
        # transport shapes may provide size_matched; require agreement if both
        # are present, and never turn missing/invalid quantity into zero.
        missing = object()
        amounts = [value(name, missing) for name in ('taking_amount', 'size_matched')]
        try:
            quantities = tuple(Decimal(str(amount)) for amount in amounts if amount is not missing)
            if (not quantities or any(not q.is_finite() or not 0 <= q <= terms.quantity for q in quantities)
                    or len(set(quantities)) != 1):
                raise ValueError('invalid_receipt_quantity')
            matched = quantities[0]
        except (ValueError, ArithmeticError):
            return self._uncertain_buy(token, action, record, native_started_at, 'invalid_receipt', venue_id, status)
        if venue_id is None:
            return self._uncertain_buy(token, action, record, native_started_at, 'invalid_receipt', None, status)
        if venue_id:
            stamp = SourceStamp(action.request_id, checked_at=self._utc_now(), complete=True, state='known')
            def fact(v): return Fact('known', v, (stamp,))
            key = (self.account_id, record.condition_id, record.token_id, venue_id)
            incoming = CurrentOrderFact(key, 'BUY', status or 'ACCEPTED', fact(terms.price),
                fact(terms.quantity), fact(matched), fact(terms.quantity-matched), 'present', None,
                fact(terms.order_type), fact(terms.expiration), 1, stamp)
            applied = self.state.accept_receipt_evidence(ReceiptEvidence(action.request_id, action.request_id,
                self.account_id, self.process_epoch, token.cycle_generation, key, 'BUY', 0, order_fact=incoming))
            self._last_current_evidence = (action.request_id, 'buy_receipt', applied)
            if applied == 'applied':
                self._record_requests[key] = replace(record, order_id=venue_id)
                self._register_opening(key)
                self._register_submit_baseline(token, action, record, key)
            elif applied != 'stale_evidence':
                return self._uncertain_buy(token, action, record, native_started_at, 'invalid_receipt', venue_id, status)
        return LocalOutcome(action, LocalStatus.ACCEPTED, True, venue_id, status)

    def _cancel(self, action, observation, order, *, protection=False):
        target = action.target
        if not isinstance(target, CancelTarget) or target.venue != self.venue:
            return LocalOutcome(action, LocalStatus.NOT_SENT, True)
        control = self._control_view()
        if (not (control.effective_running or protection) or control.fatal
                or control.cycle_generation != observation.cycle_generation):
            return LocalOutcome(action, LocalStatus.NOT_SENT, True)
        if (order.key[0] != self.account_id or order.key[2:] != (target.token_id, target.existing_order_id)
                or order.side != 'BUY'):
            return LocalOutcome(action, LocalStatus.NOT_SENT, True)
        try:
            result = self.trading.cancel_orders_detailed((target.existing_order_id,))
        except Exception:
            return LocalOutcome(action, LocalStatus.UNKNOWN_ERROR, True)
        if target.existing_order_id not in result['canceled']:
            return LocalOutcome(action, LocalStatus.REJECTED, True, target.existing_order_id)
        stamp = SourceStamp(action.request_id, checked_at=self._utc_now(), complete=True, state='known')
        # The cancel endpoint supplies exact IDs only. It proves cancellation,
        # not cumulative fills since the captured observation.
        proof = TerminalProof(order.key, 'CANCELED', order.original_size,
            Fact('unknown', sources=(stamp,), reason_codes=('cancel_final_fill_unknown',)), stamp, True)
        disposition = self.state.accept_receipt_evidence(ReceiptEvidence(action.request_id, action.request_id,
            self.account_id, self.process_epoch, observation.cycle_generation, order.key,
            'BUY', order.terms_version, terminal_proof=proof))
        # The venue ACK and application to current facts are separate results.
        # In particular, rejection by a newer final proof must never overwrite
        # that proof or be reported as a successful current-state application.
        self._last_current_evidence = (action.request_id, 'cancel', disposition)
        return LocalOutcome(action, LocalStatus.ACCEPTED, True, target.existing_order_id, 'CANCELED')

    def status(self):
        with self.state._lock:
            owned = {h.identity.request_id for h in self._native_jobs.values() if h._thread.is_alive()}
            return dict(control=self.control.status(),
                round=self.rounds.snapshot() if self.state.snapshot().current.cycle_generation else None,
                native_inflight=len(owned | self._active_sends), writer=self.writer.status(), closed=self._closed,
                last_preparation_rejection=self._last_preparation_rejection,
                last_current_evidence=self._last_current_evidence,
                last_buy_uncertainty=self._last_buy_uncertainty)

    def cancel_independent(self, order_key, *, protection_key=None, reason=None):
        """Normal exact-ID BUY cancellation outside the sealed BUY barrier.

        This native handle remains owned across logical control transitions.
        It is not a round token and never goes through round finish.
        """
        with self.state._lock:
            if not isinstance(order_key, tuple) or len(order_key) != 4 or order_key[0] != self.account_id:
                raise ValueError('independent_cancel_identity')
            key = (self.account_id, self.venue, order_key[2], order_key[3])
            existing = self._native_jobs.get(key)
            if existing and existing._thread.is_alive():
                return existing
            self._native_jobs = {k: h for k, h in self._native_jobs.items() if h._thread.is_alive()}
            snapshot = self.state.snapshot()
            control = self._control_view()
            observation = snapshot.observation
            # A receipt may have added this immutable fact after the last API
            # frame. Carry the exact fact validated here through the sender;
            # selecting again from observation.orders would lose that receipt.
            current_order = next((o for o in snapshot.active_orders if o.key == order_key and o.side == 'BUY'), None)
            protection = (protection_key in snapshot.progress
                and protection_key.scope[:3] == order_key[:3]) if protection_key is not None else False
            if (not (control.effective_running or protection) or control.fatal or observation is None
                    or not observation.orders_complete
                    or observation.cycle_generation != control.cycle_generation
                    or current_order is None):
                raise ValueError('independent_cancel_not_current')
            if len({h.identity.request_id for h in self._native_jobs.values()} | self._active_sends) >= 2:
                if protection:
                    return None  # no fabricated request or attempt on deferral
                raise RuntimeError('native_capacity')
            identity = ActionIdentity(str(uuid4()), self.account_id,
                CancelTarget(self.venue, order_key[2], order_key[3]))
            context = _IndependentContext(control.cycle_generation, 'independent:' + identity.request_id,
                self.rounds.snapshot().round_number, observation.observation_id, protection_key)
            if protection_key is not None:
                target = CancelTargetState(current_order.key, identity.request_id, current_order.remaining,
                    self._utc_now(), reason, 'pending', 'running', None,
                    Fact('unknown'), Fact('unknown'), False)
                guard = self._protection_guard(snapshot, protection_key, (current_order,))
                if self.state.apply_protection(CancelEvidenceUpdate(guard, target)) != 'applied':
                    raise ValueError('protection_cancel_not_registered')
            future = Future()
            def run():
                try:
                    outcome = self._attempt(context, identity, observation, cancel_order=current_order)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(outcome)
            thread = threading.Thread(target=run, name='lp-d2-independent-cancel', daemon=False)
            handle = NativeAttempt(identity, future, thread)
            self._native_jobs[key] = handle
            thread.start()
            return handle

    def _protection_guard(self, snapshot, scope, orders, *, observation=False):
        return ProtectionGuard(scope, snapshot.current.cycle_generation,
            snapshot.scope_versions.get(scope, 0), tuple((o.key, o.terms_version) for o in orders),
            snapshot.observation.observation_id if observation else None, uuid4().hex)

    def _protection_cancel_owned(self, order_key):
        snapshot = self.state.snapshot()
        return (any(t.order_key == order_key for p in snapshot.progress.values() for t in p.cancel_targets)
            or any(t.order.key == order_key for tickets in self._deferred_protection_cancels.values() for t in tickets))

    def _dispatch_protection_cancels(self):
        attempts = []
        with self.state._lock:
            snapshot = self.state.snapshot()
            observation = snapshot.observation
            if (self._control_view().fatal or observation is None or not observation.orders_complete
                    or observation.cycle_generation != snapshot.current.cycle_generation
                    or observation.read_ticket.trade_version_at_start != snapshot.trade_version):
                return attempts
            for key, tickets in tuple(self._deferred_protection_cancels.items()):
                baseline = snapshot.baselines.get(key)
                # Accepted risk cancellation outlives its own anchor's removal.
                # Resolve the complete exact anchor set from canonical facts;
                # filtering active_orders could silently drop a terminal member.
                anchors = tuple(next((o for o in snapshot.orders if o.key == (*key.scope[:3], order_id)), None)
                    for order_id in baseline.anchor_order_ids) if baseline is not None else ()
                if not anchors or any(o is None for o in anchors):
                    continue  # missing identity is unresolved, never a smaller anchor set
                if any(o.side != 'BUY' or o.price.state == 'known' and o.price.value != key.price
                        or o.size_matched.state == 'known' and o.size_matched.value > 0
                        or o.terminal_proof is not None and o.terminal_proof.matched.state == 'known'
                            and o.terminal_proof.matched.value > 0 for o in anchors):
                    self._deferred_protection_cancels.pop(key, None)
                    continue  # a real positive fill remains inapplicable
                if any(o.price.state != 'known' or o.original_size.state != 'known'
                        or o.size_matched.state != 'known'
                        or (o.terminal_proof is None and o.presence != 'present')
                        or (o.terminal_proof is not None and (not o.terminal_proof.identity_verified
                            or o.terminal_proof.order_key != o.key or o.terminal_proof.matched.state != 'known'
                            or o.terminal_proof.matched.value != 0 or o.terminal_proof.original_size.state != 'known'
                            or o.terminal_proof.original_size.value != o.original_size.value
                            or o.terminal_proof.source.state != 'known' or not o.terminal_proof.source.complete))
                        for o in anchors):
                    continue  # terminal UNKNOWN waits; observed lower-bound0 is not final0
                retained = []
                for ticket in tickets:
                    current = self.state.snapshot()
                    progress = current.progress.get(key)
                    if progress is None or any(t.order_key == ticket.order.key for t in progress.cancel_targets):
                        continue
                    order = next((o for o in current.active_orders if o.key == ticket.order.key), None)
                    if (order is None or order.side != 'BUY' or order.remaining.state != 'known' or order.remaining.value <= 0
                            or any(getattr(order, name).value != getattr(ticket.order, name).value
                                for name in ('price', 'original_size', 'order_type', 'expiration'))
                            or order.price.value != key.price):
                        continue  # never replace the original target with a new/changed order
                    existing = self._native_jobs.get((self.account_id, self.venue, order.key[2], order.key[3]))
                    if existing and existing._thread.is_alive():
                        continue  # a real independent request already owns this exact target
                    handle = self.cancel_independent(order.key, protection_key=key, reason=ticket.reason)
                    if handle is None:
                        retained.append(ticket)
                    else:
                        attempts.append(handle)
                if retained:
                    self._deferred_protection_cancels[key] = tuple(retained)
                else:
                    self._deferred_protection_cancels.pop(key, None)
        return attempts

    def _register_submit_baseline(self, token, action, record, order_key):
        row = self._sealed_candidates[(token.batch_id, action.request_id)]
        source = self._sealed_candidate_source
        book = row.get('direction', {}).get('book')
        if (source is None or not source.complete or source.error or not isinstance(book, Mapping)
                or book.get('condition_id') != record.condition_id or book.get('token_id') != record.token_id):
            return
        baseline = first_observation_baseline(book, price=record.price, own_remaining=Decimal(0))
        if baseline['state'] != 'known':
            return
        with self.state._lock:
            snapshot = self.state.snapshot()
            order = next(o for o in snapshot.orders if o.key == order_key)
            scope = (self.account_id, record.condition_id, record.token_id, self.process_epoch)
            key = ProtectionKey('queue_bucket', scope, record.price, None)
            stamp = SourceStamp(source.source_id, checked_at=source.checked_at,
                source_timestamp=book.get('timestamp'), complete=True, state='known')
            fact = ProtectionBaseline(key, (order_key[3],), record.price,
                Fact('known', baseline['baseline_front'], (stamp,)), 'submit',
                book.get('received_at'), book.get('timestamp'), book.get('hash'), 1,
                self._utc_now(), Decimal('.5'), stamp)
            self.state.apply_protection(SetBaseline(self._protection_guard(snapshot, key, (order,)), fact))

    def _register_opening(self, order_key):
        """Only a real current-process BUY receipt starts an opening."""
        with self.state._lock:
            snapshot = self.state.snapshot()
            ownership = self._opening_order_associations.get(order_key)
            if ownership is not None:
                return  # exact IDs never rebind, including closed exclusion tombstones
            related = tuple(o for o in snapshot.openings.values() if o.scope[:3] == order_key[:3])
            active = tuple(o for o in related if o.phase != 'closed')
            if len(active) > 1:
                raise ValueError('ambiguous_current_opening')
            previous = active[0] if active else None
            scope = previous.scope if previous else (*order_key[:3],
                self.process_epoch if not related else self.process_epoch+':'+uuid4().hex)
            order = next(o for o in snapshot.orders if o.key == order_key)
            unknown = Fact('unknown')
            scoring = ScoringState(order_key, 'entry', unknown, None, None, None, order.source)
            version = snapshot.scope_versions.get(scope, 0)+1
            if previous is None:
                opening = OpeningProtection(scope, 'opening', None, (order_key[3],),
                    order.size_matched, OpeningEconomics(scope, unknown, unknown, unknown, unknown, False),
                    Fact('known', False, (order.source,)), None, unknown, unknown, 'none', False, scoring, version)
            else:
                ids = tuple(dict.fromkeys((*previous.entry_order_ids, order_key[3])))
                # A target change resets its observation, never the risk/review latches.
                opening = replace(previous, entry_order_ids=ids, scope_version=version,
                    scoring=scoring if previous.phase == 'opening' else previous.scoring)
            orders = tuple(o for o in snapshot.orders if o.key[:3] == scope[:3])
            disposition = self.state.apply_protection(OpeningProtectionUpdate(
                self._protection_guard(snapshot, scope, orders), opening))
            if disposition != 'applied':
                raise ValueError('opening_receipt_not_registered:' + disposition)
            record = self._record_requests[order_key]
            self._opening_order_associations[order_key] = _OpeningOrderAssociation(scope, order_key,
                record.request_id, record.observation_id, order.source, requested_at=record.requested_at)

    @staticmethod
    def _scoring_target(snapshot, opening):
        if opening.phase == 'opening':
            return next(((o, 'entry') for oid in reversed(opening.entry_order_ids)
                for o in snapshot.active_orders if o.key[:3] == opening.scope[:3]
                and o.key[3] == oid and o.side == 'BUY'), (None, None))
        sell = snapshot.sells.get(opening.scope)
        attempt = (sell.passive if opening.phase == 'passive_exit' else
            sell.protected if opening.phase == 'protected_exit' else None) if sell else None
        return next(((o, opening.phase) for o in snapshot.active_orders
            if attempt and o.key == attempt.order_key and o.side == 'SELL'), (None, None))

    def _evaluate_scoring(self, deadline):
        for scope in tuple(self.state.snapshot().openings):
            snapshot = self.state.snapshot()
            opening = snapshot.openings[scope]
            if opening.phase == 'closed':
                continue
            order, role = self._scoring_target(snapshot, opening)
            if order is None:
                continue
            previous, now = opening.scoring, self._utc_now()
            same = (previous.order_key, previous.role) == (order.key, role)
            age = (now-previous.checked_at).total_seconds() if same and previous.checked_at else None
            due = age is None or age < 0 or age >= float(SCORING_POLL_SECONDS)
            scoring = previous
            if due:
                guard = self._protection_guard(snapshot, scope, (order,))
                source = self._read_protection('scoring', (order.key[3],), deadline)
                value = source.value if source.complete and not source.error else None
                # Receipt, role, control and terms may have changed during native I/O.
                with self.state._lock:
                    current = self.state.snapshot()
                    current_opening = current.openings.get(scope)
                    target, current_role = self._scoring_target(current, current_opening) if current_opening else (None, None)
                    if (self._control_view().fatal or target is None or target.key != order.key or current_role != role):
                        continue
                    checked = source.checked_at
                    if type(value) is bool and checked is not None:
                        stamp = self._source_stamp(source)
                        scoring = ScoringState(order.key, role, Fact('known', value, (stamp,)), checked,
                            checked if value is False and role == 'entry' else None,
                            self._clock() if value is False and role == 'entry' else None, stamp)
                    elif not (same and age is not None and 0 <= age <= float(SCORING_STALE_SECONDS)):
                        stamp = self._source_stamp(source)
                        scoring = ScoringState(order.key, role, Fact('unknown', sources=(stamp,)),
                            None, None, None, stamp)
                    if self.state.apply_protection(ScoringObservation(guard, scoring)) != 'applied':
                        continue
            snapshot = self.state.snapshot()
            opening = snapshot.openings[scope]
            scoring = opening.scoring
            if (role == 'entry' and scoring.scoring.value is False
                    and scoring.checked_at is not None
                    and 0 <= (self._utc_now()-scoring.checked_at).total_seconds() <= float(SCORING_STALE_SECONDS)
                    and scoring.lost_since_monotonic is not None
                    and self._clock()-scoring.lost_since_monotonic >= float(SCORING_FAILURE_WINDOW_SECONDS)
                    and not opening.stop_requested):
                orders = tuple(o for o in snapshot.active_orders if o.key[:3] == scope[:3])
                review = replace(opening, phase='review', review_status='scoring_continuously_lost',
                    review_at=Fact('known', self._utc_now(), (scoring.source,)), stop_requested=True,
                    scope_version=snapshot.scope_versions.get(scope, 0)+1)
                self.state.apply_protection(OpeningProtectionUpdate(
                    self._protection_guard(snapshot, scope, orders, observation=True), review))

    def _cancel_review_entries(self):
        attempts = []
        snapshot = self.state.snapshot()
        for opening in snapshot.openings.values():
            if not opening.stop_requested:
                continue
            for order in snapshot.active_orders:
                if (order.key[:3] != opening.scope[:3] or order.side != 'BUY'
                        or order.key[3] not in opening.entry_order_ids
                        or any(t.order_key == order.key for p in snapshot.progress.values() for t in p.cancel_targets)):
                    continue
                key = next((k for k in snapshot.progress if k.scope[:3] == order.key[:3]
                    and k.price == order.price.value), None)
                if key is not None:
                    attempts.append(self.cancel_independent(order.key, protection_key=key, reason='scoring_continuously_lost'))
        return attempts

    @staticmethod
    def _protection_component(kind, *identity):
        return 'protection:' + json.dumps((kind, *identity), separators=(',', ':'))

    def _read_protection(self, kind, identity, deadline):
        component = self._protection_component(kind, *identity)
        return self.reader.read_current(ReadScope(self.account_id, (component,)), deadline).sources[component]

    @staticmethod
    def _source_stamp(source):
        return SourceStamp(source.source_id, checked_at=source.checked_at,
            complete=source.complete, state='known' if source.complete and not source.error and source.value is not None else 'unknown',
            error_code=source.error, retry_at=source.retry_at)

    def _associate_current_manual_sells(self):
        with self.state._lock:
            snapshot = self.state.snapshot()
            observation = snapshot.observation
            if observation is None or not observation.orders_complete:
                return
            stamp = observation.order_view.orders_source
            for raw in observation.source_frame.orders:
                key = (self.account_id, raw['condition_id'], raw['token_id'], raw['order_id'])
                if raw['side'] != 'SELL' or key in self._opening_order_associations:
                    continue
                active = tuple(o for o in snapshot.openings.values() if o.scope[:3] == key[:3] and o.phase != 'closed')
                if len(active) != 1:
                    continue
                opening = active[0]
                entry_proofs = tuple(a for a in self._opening_order_associations.values()
                    if a.scope == opening.scope and a.order_key[3] in opening.entry_order_ids)
                floor = min((a.requested_at for a in entry_proofs if a.requested_at is not None), default=None)
                created = raw.get('created_at')
                order = next((o for o in snapshot.orders if o.key == key), None)
                if (floor is None or not isinstance(created, datetime) or stamp.checked_at is None
                        or not floor <= created <= stamp.checked_at or order is None
                        or order.side != 'SELL' or order.presence != 'present'):
                    continue
                self._opening_order_associations[key] = _OpeningOrderAssociation(opening.scope, key, None,
                    observation.observation_id, stamp, created_at=created)

    def _opening_orders(self, snapshot, opening):
        # Raw open_orders is a current view, never a history/ownership store.
        # Missing and terminal exact facts remain with their original episode.
        keys = {a.order_key for a in self._opening_order_associations.values() if a.scope == opening.scope}
        return tuple(o for o in snapshot.orders if o.key in keys)

    def _own_fills_from_source(self, source, orders):
        if not source.complete or source.error or not isinstance(source.value, tuple):
            return ()
        stamp, result = self._source_stamp(source), []
        for order in orders:
            matched, notional, fees = Decimal(0), Decimal(0), Decimal(0)
            ids, seen, valid, fees_known = [], {}, True, True
            for trade in source.value:
                candidates = [(m, 'maker') for m in trade['maker_orders'] if m['order_id'] == order.key[3]]
                if trade['taker_order_id'] == order.key[3]:
                    candidates.append((trade, 'taker'))
                for row, role in candidates:
                    if trade['status'] == 'FAILED':
                        continue
                    qty = row.get('matched_amount') if role == 'maker' else row.get('size')
                    price = row.get('price')
                    token = row.get('token_id')
                    if (trade['status'] != 'CONFIRMED' or not trade['trade_id']
                            or str(row.get('maker_address') or '').casefold() != self.account_id.casefold()
                            or trade['condition_id'] != order.key[1] or token != order.key[2]
                            or row['side'] != order.side or qty is None or qty <= 0
                            or price is None or not 0 < price < 1
                            or order.side == 'BUY' and price > order.price.value
                            or order.side == 'SELL' and price < order.price.value):
                        valid = False
                        continue
                    identity = trade['trade_id']
                    amount = (qty, price, row.get('fee_rate_bps'), role)
                    if identity in seen:
                        if seen[identity] != amount: valid = False
                        continue
                    seen[identity] = amount
                    ids.append(identity)
                    matched += qty
                    notional += qty * price
                    # Explicit zero rate together with real quantity/price is
                    # monetary proof. Missing rate is never a zero amount.
                    if row.get('fee_rate_bps') != 0:
                        fees_known = False
            if (valid and ids and matched <= order.original_size.value
                    and order.size_matched.state == 'known' and matched == order.size_matched.value):
                result.append(OwnFillEvidence(order.key, Fact('known', matched, (stamp,)),
                    Fact('known', notional, (stamp,)), Fact('known', fees, (stamp,)) if fees_known else Fact('unknown', sources=(stamp,)),
                    tuple(ids), stamp, True))
        return tuple(result)

    def _refresh_protection_order(self, order, deadline):
        if (order.terminal_proof is not None and order.terminal_proof.matched.state == 'known'):
            return False  # final authoritative quantities are not ordinary polling targets
        if order.presence == 'present' and order.terminal_proof is None:
            return False
        source = self._read_protection('order', order.key[1:], deadline)
        row = source.value
        if (not source.complete or source.error or not isinstance(row, Mapping)
                or (row['side'], row['price'], row['original_size']) != (order.side, order.price.value, order.original_size.value)):
            return False
        stamp = self._source_stamp(source)
        fact = lambda v: Fact('known', v, (stamp,))
        proof = TerminalProof(order.key, row['status'], fact(row['original_size']), fact(row['size_matched']), stamp, True) if row['status'] in TERMINAL_ORDER_STATES else None
        incoming = None if proof else CurrentOrderFact(order.key, order.side, row['status'],
            fact(row['price']), fact(row['original_size']), fact(row['size_matched']), fact(row['remaining_size']),
            'present', None, fact(row['order_type']), fact(row['expiration']), order.terms_version, stamp)
        with self.state._lock:
            current = self.state.snapshot()
            disposition = self.state.accept_receipt_evidence(ReceiptEvidence(uuid4().hex, source.source_id,
                self.account_id, self.process_epoch, current.current.cycle_generation, order.key, order.side,
                order.terms_version, terminal_proof=proof, order_fact=incoming))
            self._last_current_evidence = (source.source_id, 'protection_exact_order', disposition)
            return disposition == 'applied'

    def _refresh_protection_evidence(self, deadline):
        changed, checked_orders = False, set()
        def refresh_order(order):
            if order.key in checked_orders:
                return False
            checked_orders.add(order.key)
            return self._refresh_protection_order(order, deadline)

        snapshot = self.state.snapshot()
        deferred_anchors = set()
        for key, tickets in tuple(self._deferred_protection_cancels.items()):
            baseline, progress = snapshot.baselines.get(key), snapshot.progress.get(key)
            if tickets and baseline is not None and progress is not None:
                deferred_anchors.update((*key.scope[:3], oid) for oid in baseline.anchor_order_ids)
        # These exact registered IDs have accepted pending cancel obligations,
        # independently of any current-process BUY/opening association. The same
        # Reader/deadline and canonical receipt guards own their final proof.
        for order in snapshot.orders:
            if order.key in deferred_anchors:
                changed |= refresh_order(order)
        for scope in tuple(self.state.snapshot().openings):
            snapshot = self.state.snapshot()
            opening = snapshot.openings[scope]
            if opening.phase == 'closed':
                continue
            orders = self._opening_orders(snapshot, opening)
            for order in orders:
                changed |= refresh_order(order)
            snapshot = self.state.snapshot()
            orders = self._opening_orders(snapshot, snapshot.openings[scope])
            needed = tuple(o for o in orders if o.size_matched.state == 'known' and o.size_matched.value > 0
                and (o.key not in snapshot.own_fills or not snapshot.own_fills[o.key].complete_for_target
                    or any(f.state != 'known' for f in (snapshot.own_fills[o.key].quantity,
                        snapshot.own_fills[o.key].notional_usd, snapshot.own_fills[o.key].fees_usd))
                    or snapshot.own_fills[o.key].quantity.value != o.size_matched.value))
            if not needed:
                continue
            source = self._read_protection('fills', scope[1:3], deadline)
            for own in self._own_fills_from_source(source, needed):
                current = self.state.snapshot()
                old = current.own_fills.get(own.order_key)
                if old and (old.fill_ids, old.quantity.value, old.notional_usd.value, old.fees_usd.value) == (
                        own.fill_ids, own.quantity.value, own.notional_usd.value, own.fees_usd.value):
                    continue  # identical coverage cannot renew source freshness
                order = next(o for o in current.orders if o.key == own.order_key)
                sell = current.sells.get(scope)
                attempt = next((a for a in (sell.passive, sell.protected) if a and a.order_key == own.order_key), None) if sell else None
                association = self._opening_order_associations.get(own.order_key)
                if attempt is None:
                    retained = tuple(a for (episode, _), a in current.resolved_exit_attempts.items()
                        if episode == scope and a.order_key == own.order_key)
                    if len(retained) > 1:
                        continue  # ambiguous request ownership never becomes fill authority
                    attempt = retained[0] if retained else None
                record = self._record_requests.get(own.order_key)
                request = attempt.request_id if attempt else record.request_id if record else source.source_id
                disposition = self.state.accept_receipt_evidence(ReceiptEvidence(uuid4().hex, request,
                    self.account_id, self.process_epoch, current.current.cycle_generation, own.order_key, order.side,
                    order.terms_version, own_fill=own, scope=scope))
                self._last_current_evidence = (request, 'protection_own_fill', disposition)
                changed |= disposition == 'applied'
        return changed

    def _evaluate_opening(self, scope):
        snapshot = self.state.snapshot()
        opening = snapshot.openings[scope]
        if opening.phase == 'closed':
            return ()
        observation = snapshot.observation
        orders = self._opening_orders(snapshot, opening)
        entries = tuple(o for o in orders if o.side == 'BUY' and o.key[3] in opening.entry_order_ids)
        buys = tuple(snapshot.own_fills.get(o.key) for o in entries)
        sales_orders = tuple(o for o in orders if o.side == 'SELL' and o.size_matched.value != 0)
        sales = tuple(snapshot.own_fills.get(o.key) for o in sales_orders)
        complete = all(o.size_matched.state == 'known' and (o.size_matched.value == 0 or own is not None
            and own.complete_for_target and own.quantity.value == o.size_matched.value)
            for o,own in (*zip(entries, buys), *zip(sales_orders, sales)))
        bought = sum((o.quantity.value for o in buys if o and o.quantity.state == 'known'), Decimal(0)) if complete else None
        sold = sum((o.quantity.value for o in sales if o and o.quantity.state == 'known'), Decimal(0)) if complete else None
        positions = tuple(p for p in snapshot.positions if (p.account_id, p.condition_id, p.token_id) == scope[:3])
        position = positions[0] if len(positions) == 1 else None
        residual = position.quantity if position else Fact('unknown')
        position_source = observation.components['positions']
        monetary_fills = tuple(o for o in (*buys, *sales) if o is not None)
        final_coverage = bool(orders) and all(o.terminal_proof is not None
            and o.terminal_proof.identity_verified and o.terminal_proof.matched.state == 'known'
            and o.terminal_proof.source.checked_at is not None
            and position_source.checked_at is not None
            and position_source.checked_at >= o.terminal_proof.source.checked_at
            and observation.order_view.orders_source.checked_at >= o.terminal_proof.source.checked_at for o in orders)
        previous_sell = snapshot.sells.get(scope)
        attempts = tuple(a for a in (previous_sell.passive, previous_sell.protected) if a) if previous_sell else ()
        no_native = all(a.native_state == 'ended' for a in attempts) and not any(h._thread.is_alive()
            and (getattr(h.identity, 'scope', None) == scope
                or any(h is self._native_jobs.get((self.account_id, self.venue, o.key[2], o.key[3])) for o in orders))
            for h in self._native_jobs.values())
        absent_zero = bool(not positions and self._positions_full_coverage and observation.positions_complete
            and observation.orders_complete and complete and bought is not None and bought == sold
            and final_coverage and no_native and all(o.complete_for_target and all(f.state == 'known'
                for f in (o.quantity, o.notional_usd, o.fees_usd)) for o in monetary_fills))
        if absent_zero:
            # Same-frame complete threshold0 coverage plus fully resolved exact
            # obligations projects zero locally; never invent a canonical row.
            residual = Fact('known', Decimal(0), (position_source,))
        reconciled = bool(complete and bought is not None and sold is not None and bought >= sold
            and (position is not None or absent_zero) and observation.positions_complete and observation.orders_complete
            and residual.state == 'known' and residual.value == bought-sold)
        source = observation.source_frame.sources.get(f'market:{scope[1]}:{scope[2]}')
        data = source.value if source and source.complete and not source.error and isinstance(source.value, Mapping) else {}
        book, market = data.get('book'), data.get('metadata')
        if not isinstance(book, Mapping) or (book.get('condition_id'), book.get('token_id')) != scope[1:3]:
            book = None
        if book is not None:
            try:
                _freshness(book.get('received_at'), self._utc_now(), 'book_freshness')
                _freshness(book.get('timestamp'), self._utc_now(), 'book_timestamp')
                _levels(book.get('bids'), 'bids')
                _levels(book.get('asks'), 'asks')
            except ValueError:
                book = None
        stamp = self._source_stamp(source) if source else SourceStamp('')
        risk_input = dict(book=book, market=market)
        fact = lambda value, sources: Fact('known', value, sources) if value is not None else Fact('unknown', sources=sources)
        own = tuple(o for o in (*buys, *sales) if o is not None)
        amounts_known = complete and all(o.notional_usd.state == 'known' for o in own)
        sources = tuple(o.source for o in own)
        cost = sum((o.notional_usd.value for o in buys if o), Decimal(0)) if amounts_known and bought else None
        revenue = sum((o.notional_usd.value for o in sales if o), Decimal(0)) if amounts_known and bought else None
        exit_value = _executable_bid_value(risk_input, residual.value) if book and residual.state == 'known' else None
        projected = _projected_taker_fee(risk_input, residual.value) if book and residual.state == 'known' else None
        if residual.state == 'known' and residual.value == 0:
            exit_value, projected = Decimal(0), Decimal(0)
        fees = sum((o.fees_usd.value for o in own), Decimal(0))+projected if (
            complete and bought and all(o.fees_usd.state == 'known' for o in own) and projected is not None) else None
        economics = OpeningEconomics(scope, fact(cost, sources), fact(revenue, sources), fact(fees, (*sources, stamp)),
            fact(exit_value, (stamp,)), reconciled)
        loss = economics.opening_loss_usd
        latch = not opening.stop_loss_latched.value and loss.state == 'known' and loss.value >= STOP_LOSS
        updated = replace(opening, buy_filled_quantity=fact(bought, sources), economics=economics,
            stop_loss_latched=Fact('known', True, loss.sources) if latch else opening.stop_loss_latched,
            stop_loss_triggered_at=self._utc_now() if latch else opening.stop_loss_triggered_at,
            stop_loss_triggered_loss_usd=loss if latch else opening.stop_loss_triggered_loss_usd,
            scope_version=snapshot.scope_versions.get(scope, 0)+1)
        with self.state._lock:
            if self.state.apply_protection(OpeningProtectionUpdate(
                    self._protection_guard(snapshot, scope, orders, observation=True), updated)) != 'applied':
                return ()
            snapshot = self.state.snapshot()
            old = snapshot.sells.get(scope)
            def current_attempt(attempt):
                if attempt is None or attempt.native_state != 'ended': return attempt
                cancel = self._native_jobs.get((self.account_id, self.venue, scope[2], attempt.cancel_request_id))
                if cancel is not None and cancel._thread.is_alive(): return attempt
                order = next((o for o in snapshot.orders if o.key == attempt.order_key), None)
                own = snapshot.own_fills.get(attempt.order_key)
                proof = order.terminal_proof if order else None
                # None is not zero. All final0, exact request, native end, original
                # terms, fresh complete positions/orders and reconciliation agree.
                zero = bool(proof and proof.identity_verified and proof.matched.state == 'known' and proof.matched.value == 0
                    and proof.original_size.value == attempt.submit_quantity.value and reconciled
                    and observation.order_view.orders_source.checked_at >= proof.source.checked_at
                    and position_source.checked_at >= proof.source.checked_at)
                filled = bool(proof and own and own.complete_for_target and own.quantity.state == 'known'
                    and proof.matched.state == 'known' and own.quantity.value == proof.matched.value and reconciled)
                return replace(attempt, state='terminal', own_fill_evidence=own) if zero or filled else attempt
            sell = SellState(scope, residual, position.inventory_cost_usd if position else Fact('unknown'), reconciled,
                tuple(o for o in snapshot.active_orders if o.key[:3] == scope[:3] and o.side == 'SELL'),
                observation.order_view.orders_source, observation.components['positions'],
                current_attempt(old.passive) if old else None, current_attempt(old.protected) if old else None)
            if self.state.apply_protection(ExitEvidenceUpdate(
                    self._protection_guard(snapshot, scope, orders, observation=True), sell)) != 'applied':
                return ()
            snapshot = self.state.snapshot()
            opening, sell = snapshot.openings[scope], snapshot.sells[scope]
            origin = _SellCalculationOrigin(self._protection_guard(snapshot, scope, orders, observation=True),
                snapshot.trade_version, residual.value, sold)
            attempts = tuple(a for a in (sell.passive, sell.protected) if a)
            final_orders = bool(orders) and all(o.terminal_proof is not None
                and o.terminal_proof.identity_verified and o.terminal_proof.matched.state == 'known'
                and o.terminal_proof.source.checked_at is not None
                and observation.order_view.orders_source.checked_at >= o.terminal_proof.source.checked_at
                and position_source.checked_at is not None and position_source.checked_at >= o.terminal_proof.source.checked_at
                for o in orders)
            monetary = complete and all(o is not None and o.complete_for_target
                and all(f.state == 'known' for f in (o.quantity, o.notional_usd, o.fees_usd))
                for order,o in (*zip(entries, buys), *zip(sales_orders, sales))
                if order.size_matched.value != 0)
            native_ended = all(a.native_state == 'ended' and a.state in ('terminal', 'rejected') for a in attempts)
            native_pending = any(h._thread.is_alive() and (getattr(h.identity, 'scope', None) == scope
                or any(h is self._native_jobs.get((self.account_id, self.venue, o.key[2], o.key[3])) for o in orders))
                for h in self._native_jobs.values())
            if (reconciled and bought and residual.state == 'known' and residual.value == 0
                    and final_orders and monetary and native_ended and not native_pending):
                closed = replace(opening, phase='closed', scope_version=snapshot.scope_versions.get(scope, 0)+1)
                if self.state.apply_protection(OpeningProtectionUpdate(origin.guard, closed)) != 'applied':
                    return ()
                closing = self.state.snapshot()
                disposition = self.state.retire_exit_attempts(
                    self._protection_guard(closing, scope, orders, observation=True))
                self._last_current_evidence = (scope, 'exit_retirement', disposition)
                if disposition != 'applied':
                    # No observer can see an unconfirmed completion: this whole
                    # projection/retirement sequence holds the state owner lock.
                    # Restore the prior role through the public guard, preserving
                    # every quantity, latch and unresolved obligation for retry.
                    current = self.state.snapshot()
                    pending = replace(current.openings[scope], phase=opening.phase,
                        scope_version=current.scope_versions.get(scope, 0)+1)
                    restored = self.state.apply_protection(OpeningProtectionUpdate(
                        self._protection_guard(current, scope, orders, observation=True), pending))
                    if restored != 'applied':
                        raise RuntimeError('exit_retirement_refusal_not_restored:' + restored)
                return ()
        if not reconciled or residual.state != 'known' or residual.value <= 0 or not book:
            return ()
        if len({h.identity.request_id for h in self._native_jobs.values() if h._thread.is_alive()} | self._active_sends) >= 2:
            return ()
        passive = sell.passive
        if opening.stop_loss_latched.value:
            if passive and (passive.native_state != 'ended' or passive.state not in ('terminal', 'rejected')):
                if passive.order_key and not passive.cancel_request_id and passive.native_state == 'ended':
                    return (self._cancel_exit(scope, passive, origin),)
                return ()
            if (sell.protected is not None and (sell.protected.native_state != 'ended'
                    or sell.protected.state not in ('terminal', 'rejected'))) or sell.current_sells:
                return ()
            bids = sorted(((Decimal(str(r['price'])), Decimal(str(r['size']))) for r in book['bids']), reverse=True)
            remaining, quantity, price = residual.value, Decimal(0), None
            for level, size in bids:
                used = min(remaining, size)
                if used > 0:
                    quantity += used; remaining -= used; price = level
                if remaining <= 0: break
            minimum = book.get('min_order_size')
            if price and quantity > 0 and (minimum is None or quantity >= minimum):
                return (self._start_exit(scope, 'protected_exit', quantity, sold, price, origin),)
        elif economics.opening_loss_usd.state == 'known' and not opening.stop_requested:
            asks = sorted((Decimal(str(r['price'])), Decimal(str(r['size']))) for r in book['asks'])
            own_at_price = {}
            for order in sell.current_sells:
                own_at_price[order.price.value] = own_at_price.get(order.price.value, Decimal(0))+order.remaining.value
            price = next((p for p,size in asks if size > own_at_price.get(p, 0)), None)
            if passive and (passive.state not in ('terminal', 'rejected') or passive.native_state != 'ended'):
                if (price is not None and price != passive.price.value and passive.native_state == 'ended'
                        and passive.order_key and not passive.cancel_request_id):
                    return (self._cancel_exit(scope, passive, origin),)
                return ()
            if price and not sell.current_sells:
                return (self._start_exit(scope, 'passive_exit', residual.value, sold, price, origin),)
        return ()

    def _publish_sell_attempt(self, scope, attempt, *, observation=False):
        snapshot = self.state.snapshot()
        sell = snapshot.sells[scope]
        name = 'passive' if attempt.kind == 'passive_exit' else 'protected'
        sell = replace(sell, **{name: attempt})
        orders = tuple(o for o in snapshot.orders if o.key[:3] == scope[:3])
        result = self.state.apply_protection(ExitEvidenceUpdate(
            self._protection_guard(snapshot, scope, orders, observation=observation), sell))
        if result != 'applied':
            raise ValueError('exit_attempt_not_applied:' + result)

    def _sell_origin_current(self, snapshot, scope, origin):
        guard, observation = origin.guard, snapshot.observation
        terms = {o.key: o.terms_version for o in snapshot.orders}
        return bool(guard.scope == scope and observation is not None
            and guard.observation_id == observation.observation_id
            and guard.cycle_generation == snapshot.current.cycle_generation
            and guard.expected_scope_version == snapshot.scope_versions.get(scope, 0)
            and origin.trade_version == snapshot.trade_version == observation.read_ticket.trade_version_at_start
            and observation.orders_complete and observation.positions_complete
            and all(terms.get(key) == version for key, version in guard.expected_order_terms_versions))

    def _start_exit(self, scope, kind, quantity, sold, price, origin):
        with self.state._lock:
            snapshot = self.state.snapshot()
            if not self._sell_origin_current(snapshot, scope, origin):
                return None
            sell = snapshot.sells.get(scope)
            positions = tuple(p for p in snapshot.positions if (p.account_id, p.condition_id, p.token_id) == scope[:3])
            if (sell is None or not sell.position_reconciled or len(positions) != 1
                    or positions[0].quantity.state != 'known' or positions[0].quantity.value != origin.residual
                    or sell.residual_quantity.state != 'known' or sell.residual_quantity.value != origin.residual
                    or sold != origin.sold or not 0 < quantity <= origin.residual
                    or any(o.side == 'SELL' and o.key[:3] == scope[:3] for o in snapshot.active_orders)):
                return None
            if self._control_view().fatal or len({h.identity.request_id for h in self._native_jobs.values()
                    if h._thread.is_alive()} | self._active_sends) >= 2:
                return None
            identity = _ProtectionAction(uuid4().hex, self.account_id, scope, kind)
            stamp = snapshot.sells[scope].positions_source
            fact = lambda v: Fact('known', v, (stamp,))
            attempt = ExitAttempt(identity.request_id, kind, None, 'pending', False, fact(quantity),
                fact(sold), snapshot.sells[scope].residual_quantity, fact(price),
                fact('GTC' if kind == 'passive_exit' else 'FOK'), fact(None), None, None, 'running')
            self._publish_sell_attempt(scope, attempt, observation=True)
            current = self.state.snapshot()
            opening = current.openings[scope]
            unknown = Fact('unknown')
            role_reset = ScoringState(None, None, unknown, None, None, None, stamp)
            orders = tuple(o for o in current.orders if o.key[:3] == scope[:3])
            changed = replace(opening, phase=kind, scoring=role_reset,
                scope_version=current.scope_versions.get(scope, 0)+1)
            if self.state.apply_protection(OpeningProtectionUpdate(
                    self._protection_guard(current, scope, orders, observation=True), changed)) != 'applied':
                raise ValueError('exit_role_not_current')
            generation = snapshot.current.cycle_generation
            record = RequestRecord(self.account_id, self.process_epoch, identity.request_id, 'd2:'+identity.request_id,
                scope[1], scope[2], 'SELL', price, quantity, self._utc_now(), None, self.venue, generation,
                'protection:'+identity.request_id, self.rounds.snapshot().round_number,
                snapshot.observation.observation_id, attempt.order_type.value, None, kind == 'passive_exit', ())
            self.writer.offer(record)
            return self._start_protection_native(identity, lambda: self._execute_exit(scope, attempt, record, generation))

    def _start_protection_native(self, identity, operation):
        owned = {h.identity.request_id for h in self._native_jobs.values() if h._thread.is_alive()} | self._active_sends
        if len(owned) >= 2:
            raise RuntimeError('native_capacity')
        self._native_jobs = {k: h for k, h in self._native_jobs.items() if h._thread.is_alive()}
        future = Future()
        def run():
            try: result = operation()
            except BaseException as exc: future.set_exception(exc)
            else: future.set_result(result)
        thread = threading.Thread(target=run, name='lp-d2-protection-send', daemon=False)
        handle = NativeAttempt(identity, future, thread)
        self._native_jobs[(self.account_id, self.venue, identity.scope[2], identity.request_id)] = handle
        thread.start()
        return handle

    def _execute_exit(self, scope, attempt, record, generation):
        outcome, venue_id, signed = 'not_sent', None, None
        try:
            if self._control_view().fatal or self._control_view().cycle_generation != generation:
                with self.state._lock:
                    self._publish_sell_attempt(scope, replace(attempt, state='rejected', native_state='ended'))
                return _ProtectionOutcome(attempt.request_id, outcome, venue_id)
            signed = (self.trading.lp_create_limit_order(token_id=scope[2], side='SELL', price=attempt.price.value,
                quantity=attempt.submit_quantity.value, expiration=None, post_only=True)
                if attempt.kind == 'passive_exit' else None)
            if self._control_view().fatal or self._control_view().cycle_generation != generation:
                with self.state._lock:
                    self._publish_sell_attempt(scope, replace(attempt, state='rejected', native_state='ended'))
                return _ProtectionOutcome(attempt.request_id, outcome, venue_id)
            outcome = 'unknown'
            response = (self.trading.lp_post_order(signed) if signed is not None else
                self.trading.submit_protected_sell(token_id=scope[2], quantity=attempt.submit_quantity.value, min_price=attempt.price.value))
            value = lambda name, default=None: response.get(name, default) if isinstance(response, Mapping) else getattr(response, name, default)
            venue_id = value('order_id', value('id'))
            if value('accepted') is False or value('ok') is False:
                outcome = 'rejected'
            elif type(venue_id) is str and venue_id:
                outcome = 'accepted'
                key = (*scope[:3], venue_id)
                with self.state._lock:
                    association = self._opening_order_associations.get(key)
                    current = self.state.snapshot()
                    sell = current.sells[scope]
                    previous_requests = tuple(a for a in (sell.passive, sell.protected) if a)
                    previous_requests += tuple(a for (episode, _), a in current.resolved_exit_attempts.items() if episode == scope)
                    if (association is not None and (association.scope != scope
                            or association.request_id not in (None, attempt.request_id))
                            or any(a.order_key == key and a.request_id != attempt.request_id for a in previous_requests)):
                        raise ValueError('exit_order_identity_already_owned')
                    # First observation may precede this native receipt. Its
                    # immutable same-episode provenance is retained; the real
                    # request/order binding lives in the state-owned attempt.
                stamp = SourceStamp(attempt.request_id, checked_at=self._utc_now(), complete=True, state='known')
                amount = value('making_amount')
                try: matched = Decimal(str(amount))
                except ArithmeticError: matched = None
                if matched is not None and matched.is_finite() and 0 <= matched <= attempt.submit_quantity.value:
                    fact = lambda v: Fact('known', v, (stamp,))
                    key = (*scope[:3], venue_id)
                    incoming = CurrentOrderFact(key, 'SELL', str(value('status', 'ACCEPTED')).upper(),
                        attempt.price, attempt.submit_quantity, fact(matched), fact(attempt.submit_quantity.value-matched),
                        'present', None, attempt.order_type, attempt.expiration, 1, stamp)
                    result = self.state.accept_receipt_evidence(ReceiptEvidence(uuid4().hex, attempt.request_id,
                        self.account_id, self.process_epoch, generation, key, 'SELL', 0, order_fact=incoming, scope=scope))
                    self._last_current_evidence = (attempt.request_id, 'sell_receipt', result)
                with self.state._lock:
                    key = (*scope[:3], venue_id)
                    self._publish_sell_attempt(scope, replace(attempt, state='accepted', order_key=key, native_state='ended'))
                    self._opening_order_associations.setdefault(key, _OpeningOrderAssociation(scope, key,
                        attempt.request_id, record.observation_id, stamp, requested_at=record.requested_at))
                return _ProtectionOutcome(attempt.request_id, outcome, venue_id)
        except Exception:
            pass  # actual post uncertainty retains this exact attempt; never replay it
        finally:
            self.writer.offer(ReceiptRecord(self.account_id, self.process_epoch, attempt.request_id,
                record.session_id, scope[1], scope[2], venue_id, outcome, self._utc_now()))
        with self.state._lock:
            self._publish_sell_attempt(scope, replace(attempt,
                state='rejected' if outcome in ('rejected', 'not_sent') else 'unknown', native_state='ended'))
        return _ProtectionOutcome(attempt.request_id, outcome, venue_id)

    def _cancel_exit(self, scope, attempt, origin):
        with self.state._lock:
            if self._control_view().fatal or len({h.identity.request_id for h in self._native_jobs.values()
                    if h._thread.is_alive()} | self._active_sends) >= 2:
                return None
            snapshot = self.state.snapshot()
            if not self._sell_origin_current(snapshot, scope, origin):
                return None
            identity = _ProtectionAction(uuid4().hex, self.account_id, scope, 'cancel_exit')
            updated = replace(attempt, cancel_request_id=identity.request_id)
            self._publish_sell_attempt(scope, updated, observation=True)
            record = RequestRecord(self.account_id, self.process_epoch, identity.request_id, 'd2:'+identity.request_id,
                scope[1], scope[2], 'CANCEL', attempt.price.value, attempt.submit_quantity.value, self._utc_now(),
                attempt.order_key[3], self.venue, snapshot.current.cycle_generation, 'protection:'+identity.request_id,
                self.rounds.snapshot().round_number, snapshot.observation.observation_id,
                attempt.order_type.value, attempt.expiration.value, None, ())
            self.writer.offer(record)
            def cancel():
                outcome = 'unknown'
                try:
                    if self._control_view().fatal:
                        outcome = 'not_sent'
                    else:
                        result = self.trading.cancel_orders_detailed((attempt.order_key[3],))
                        if attempt.order_key[3] in result['canceled']:
                            outcome = 'accepted'
                            snapshot = self.state.snapshot()
                            order = next(o for o in snapshot.orders if o.key == attempt.order_key)
                            stamp = SourceStamp(identity.request_id, checked_at=self._utc_now(), complete=True, state='known')
                            proof = TerminalProof(order.key, 'CANCELED', order.original_size, Fact('unknown', sources=(stamp,)), stamp, True)
                            disposition = self.state.accept_receipt_evidence(ReceiptEvidence(uuid4().hex, attempt.request_id,
                                self.account_id, self.process_epoch, snapshot.current.cycle_generation,
                                order.key, 'SELL', order.terms_version, terminal_proof=proof, scope=scope))
                            self._last_current_evidence = (identity.request_id, 'sell_cancel', disposition)
                except Exception:
                    pass
                self.writer.offer(ReceiptRecord(self.account_id, self.process_epoch, identity.request_id,
                    record.session_id, scope[1], scope[2], attempt.order_key[3], outcome, self._utc_now()))
                return _ProtectionOutcome(identity.request_id, outcome, attempt.order_key[3])
            return self._start_protection_native(identity, cancel)

    def _finish_protection_cancel(self, key, outcome):
        with self.state._lock:
            snapshot = self.state.snapshot()
            progress = snapshot.progress.get(key)
            previous = next((t for t in progress.cancel_targets if t.request_id == outcome.identity.request_id), None) if progress else None
            if previous is None:
                raise ValueError('protection_cancel_identity_lost')
            order = next(o for o in snapshot.orders if o.key == previous.order_key)
            target = replace(previous, local_outcome=outcome.status.value, native_state='ended',
                venue_terminal=order.terminal_proof if outcome.status == LocalStatus.ACCEPTED else None)
            disposition = self.state.apply_protection(CancelEvidenceUpdate(
                self._protection_guard(snapshot, key, (order,)), target))
            if disposition != 'applied':
                raise ValueError('protection_cancel_completion_not_applied:' + disposition)

    def _register_first_seen(self, snapshot):
        observation = snapshot.observation
        buckets = {}
        for order in snapshot.active_orders:
            if order.side == 'BUY':
                buckets.setdefault((order.key[:3], order.price.value), []).append(order)
        for (identity, price), orders in buckets.items():
            current = self.state.snapshot()
            ids = {o.key[3] for o in orders}
            existing = tuple(b for b in current.baselines.values() if b.key.scope[:3] == identity
                and b.key.price == price and ids.intersection(b.anchor_order_ids))
            if any(b.key.kind == 'queue_bucket' or b.baseline_front.state == 'known' for b in existing):
                continue
            old = next(iter(existing), None)
            key = old.key if old else ProtectionKey('first_seen_episode', (*identity, self.process_epoch),
                price, orders[0].key[3])
            source = observation.source_frame.sources.get(f'market:{identity[1]}:{identity[2]}')
            book = (source.value.get('book') if source and source.complete and not source.error
                and isinstance(source.value, Mapping) else None)
            if isinstance(book, Mapping) and (book.get('condition_id') != identity[1] or book.get('token_id') != identity[2]):
                book = None
            result = first_observation_baseline(book, price=price,
                own_remaining=sum((o.remaining.value for o in orders), Decimal(0)))
            stamp = SourceStamp(source.source_id if source else '', checked_at=source.checked_at if source else None,
                complete=bool(source and source.complete), state='known' if result['state'] == 'known' else 'unknown')
            front = Fact('known', result['baseline_front'], (stamp,)) if result['state'] == 'known' else Fact('unknown', sources=(stamp,))
            baseline = ProtectionBaseline(key, old.anchor_order_ids if old else (orders[0].key[3],), price,
                front, 'first_observation', result['baseline_book_received_at'],
                book.get('timestamp') if book else None, result['baseline_book_hash'],
                current.scope_versions.get(key, 0)+1, old.registered_at if old else self._utc_now(), Decimal('.5'), stamp)
            self.state.apply_protection(SetBaseline(
                self._protection_guard(current, key, orders, observation=True), baseline))

    def run_protection_once(self, *, timeout=2):
        """Evaluate the accepted raw frame; protection never acquires a new book."""
        with self._protection_lock:
            snapshot = self.state.snapshot()
            observation = snapshot.observation
            if (self._closed or snapshot.current.shutdown_requested or snapshot.current.fatal_id
                    or observation is None or not observation.orders_complete
                    or observation.cycle_generation != snapshot.current.cycle_generation
                    or observation.read_ticket.trade_version_at_start != snapshot.trade_version):
                return ()
            deadline = time.monotonic()+timeout
            if self._refresh_protection_evidence(deadline):
                return ()  # receipts invalidate the old observation; ordinary reader supplies the next one
            self._register_first_seen(snapshot)
            snapshot = self.state.snapshot()
            attempts = self._dispatch_protection_cancels()
            for key, baseline in snapshot.baselines.items():
                current = self.state.snapshot()
                progress = current.progress.get(key)
                if progress and progress.cancel_targets:
                    continue
                orders = tuple(o for o in snapshot.active_orders if o.key[:3] == key.scope[:3]
                    and o.side == 'BUY' and o.price.value == key.price)
                anchors = tuple(o for o in orders if o.key[3] in baseline.anchor_order_ids)
                pending_anchor = any((handle := self._native_jobs.get(
                    (self.account_id, self.venue, o.key[2], o.key[3]))) is not None
                    and handle._thread.is_alive() for o in anchors)
                if not anchors or any(o.size_matched.value != 0 for o in anchors) or pending_anchor:
                    continue
                source = observation.source_frame.sources.get(f'market:{key.scope[1]}:{key.scope[2]}')
                book = (source.value.get('book') if source and source.complete and not source.error
                    and isinstance(source.value, Mapping) else None)
                estimate = estimate_lp_queue_position(book, price=key.price,
                    own_remaining=sum((o.remaining.value for o in orders), Decimal(0)),
                    baseline_front=baseline.baseline_front.value, condition_id=key.scope[1], token_id=key.scope[2]) if baseline.baseline_front.state == 'known' else dict(
                        state='unknown', front_estimate=None, level_total=None, ratio=None,
                        reason_codes=('baseline_unknown',), data_time=book.get('received_at') if book else None)
                stamp = SourceStamp(source.source_id if source else '',
                    checked_at=source.checked_at if source else None,
                    complete=bool(source and source.complete), state='known' if book else 'unknown')
                def fact(name):
                    value = estimate.get(name)
                    return Fact('known', value, (stamp,)) if value is not None else Fact('unknown', sources=(stamp,))
                failure_scope = key.scope if key.kind == 'queue_bucket' else key
                old_failure = current.failure_states.get(failure_scope)
                evaluation_id = source.source_id if source and source.source_id else observation.observation_id
                fresh_failure = (source is not None and bool(source.source_id) and source.failed_at is not None
                    and source.failed_at >= observation.read_ticket.started_at)
                count = old_failure.data_failures if old_failure else 0
                if old_failure and old_failure.last_evaluation_id == evaluation_id:
                    failure = old_failure
                else:
                    count = count + 1 if estimate['state'] == 'unknown' and fresh_failure else (
                        0 if estimate['state'] != 'unknown' else count)
                    failure = ProtectionFailureState('queue_group' if key.kind == 'queue_bucket' else 'first_seen_episode', failure_scope, count, evaluation_id,
                        (old_failure.scope_version if old_failure else 0) + 1)
                triggered = estimate['state'] == 'triggered' or failure.data_failures >= 10
                update = ProtectionProgress(key, 'triggered' if triggered else estimate['state'], fact('front_estimate'), fact('level_total'),
                    fact('ratio'), tuple(estimate['reason_codes']), estimate['data_time'], (),
                    current.scope_versions.get(key, 0)+1, failure)
                disposition = self.state.apply_protection(QueueProgressUpdate(
                    self._protection_guard(current, key, orders, observation=True), update))
                if disposition != 'applied':
                    continue
                if triggered:
                    reason = 'queue_data_failure_limit' if failure.data_failures >= 10 else 'queue_threshold'
                    self._deferred_protection_cancels[key] = tuple(_DeferredProtectionCancel(
                        order, observation.observation_id, reason) for order in orders)
                    attempts.extend(self._dispatch_protection_cancels())
            current = self.state.snapshot()
            for order_key in current.unknown_yield_due:
                if any(t.order_key == order_key for progress in current.progress.values() for t in progress.cancel_targets):
                    continue  # current exact request obligation survives an uncertain result
                order = next((o for o in current.active_orders if o.key == order_key and o.side == 'BUY'), None)
                if order is None:
                    continue
                key = next((k for k in current.baselines if k.scope[:3] == order.key[:3] and k.price == order.price.value), None)
                if key is None:
                    continue
                if key not in current.progress:
                    failure_scope = key.scope if key.kind == 'queue_bucket' else key
                    failure = current.failure_states.get(failure_scope) or ProtectionFailureState(
                        'queue_group' if key.kind == 'queue_bucket' else 'first_seen_episode', failure_scope,
                        0, 'unknown-yield:' + observation.observation_id, 1)
                    progress = ProtectionProgress(key, 'triggered', Fact('unknown'), Fact('unknown'),
                        Fact('unknown'), ('buy_yield_unknown_timeout',), None, (),
                        current.scope_versions.get(key, 0)+1, failure)
                    if self.state.apply_protection(QueueProgressUpdate(
                            self._protection_guard(current, key, (order,), observation=True), progress)) != 'applied':
                        continue
                attempts.append(self.cancel_independent(order_key,
                    protection_key=key, reason='buy_yield_unknown_timeout'))
            for scope in tuple(self.state.snapshot().openings):
                attempts.extend(self._evaluate_opening(scope))
            self._evaluate_scoring(deadline)
            attempts.extend(self._cancel_review_entries())
            return tuple(a for a in attempts if a is not None)

    def close(self, timeout=5):
        self._closed = True
        deadline = time.monotonic()+max(0, timeout)
        # Stop disposable admission immediately; late native receipts still go
        # to CurrentLPState, while their optional records are counted as lost.
        try:
            self.writer.close(timeout=0)
        except TimeoutError:
            pass
        reader_done = self.reader.close(timeout=max(0, deadline-time.monotonic()))
        control_done = self.control.close(max(0, deadline-time.monotonic())).writer_drained
        with self.state._lock:
            handles = tuple(self._native_jobs.values())
        for handle in handles:
            handle._thread.join(timeout=max(0, deadline-time.monotonic()))
        # A synchronous batch may also be inside a real native call. Neither
        # local control reset nor this bounded close releases that owner.
        run_done = self._run_lock.acquire(timeout=max(0, deadline-time.monotonic()))
        if run_done:
            self._run_lock.release()
        try:
            self.writer.close(timeout=max(0, deadline-time.monotonic()))
        except TimeoutError:
            return False
        return (reader_done and control_done and run_done
            and all(not h._thread.is_alive() for h in handles))
