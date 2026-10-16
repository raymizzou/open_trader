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
)
from .polymarket_lp_market_calculation import calculate_current
from .polymarket_lp_market_reader import MarketReader, ReadScope
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


@dataclass(frozen=True)
class NativeAttempt:
    identity: ActionIdentity
    _future: Future
    _thread: threading.Thread

    def done(self):
        return self._future.done() and not self._thread.is_alive()

    def result(self, timeout=None):
        return self._future.result(timeout=timeout)


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
        self.reader = reader if reader is not None else MarketReader(trading.lp_reader_source_port(), clock=clock)
        self.writer = writer if writer is not None else LPBusinessWriter(store)
        self.state = CurrentLPState(account_id, process_epoch, clock=clock, utc_now=utc_now)
        self.rounds = LPRoundCoordinator()
        self.control = LPControlOwner(self.state, _ControlStore(store, account_id), self, self,
                                      clock=clock, utc_now=utc_now)
        self._run_lock = threading.Lock()
        self._closed = False
        self._admission = None
        self._record_requests = {}
        self._sealed_candidates = {}
        self._native_jobs = {}
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
            outcomes = tuple(self._attempt(start.token, action, observation) for action in start.token.actions)
            finish = self.complete_local_attempts(start.token, outcomes)
            self._sealed_candidates.clear()
            return RunResult(accepted, start, finish, outcomes)
        finally:
            self._run_lock.release()

    def _offer_progress(self, frame):
        # In-process logging identities only: never restore request history or
        # guess a venue ID for a BUY whose receipt was missing.
        retained = {}
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
            permitted = (control.effective_running and not control.fatal
                and control.cycle_generation == token.cycle_generation and action.account_id == self.account_id
                and target.venue == self.venue)
            owned = {h.identity.request_id for h in self._native_jobs.values() if h._thread.is_alive()}
            occupied = (owned | self._active_sends) - {action.request_id}
            if isinstance(target, CancelTarget):
                existing = self._native_jobs.get((self.account_id, target.venue, target.token_id, target.existing_order_id))
                permitted = permitted and not (existing and existing._thread.is_alive()
                    and existing.identity.request_id != action.request_id)
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
                outcome = self._cancel(action, observation, order) if isinstance(target, CancelTarget) else self._buy(token, action, record)
            finally:
                with self.state._lock:
                    self._active_sends.discard(action.request_id)
        self.writer.offer(ReceiptRecord(self.account_id, self.process_epoch, action.request_id,
            record.session_id, record.condition_id, record.token_id, outcome.venue_order_id or record.order_id,
            'accepted' if outcome.status == LocalStatus.ACCEPTED else
            'not_sent' if outcome.status == LocalStatus.NOT_SENT else
            'rejected' if outcome.status == LocalStatus.REJECTED else 'unknown',
            self._utc_now(), reason))
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
            elif applied != 'stale_evidence':
                return self._uncertain_buy(token, action, record, native_started_at, 'invalid_receipt', venue_id, status)
        return LocalOutcome(action, LocalStatus.ACCEPTED, True, venue_id, status)

    def _cancel(self, action, observation, order):
        target = action.target
        if not isinstance(target, CancelTarget) or target.venue != self.venue:
            return LocalOutcome(action, LocalStatus.NOT_SENT, True)
        control = self._control_view()
        if (not control.effective_running or control.fatal
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

    def cancel_independent(self, order_key):
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
            if (not control.effective_running or control.fatal or observation is None
                    or not observation.orders_complete
                    or observation.cycle_generation != control.cycle_generation
                    or current_order is None):
                raise ValueError('independent_cancel_not_current')
            if len({h.identity.request_id for h in self._native_jobs.values()} | self._active_sends) >= 2:
                raise RuntimeError('native_capacity')
            identity = ActionIdentity(str(uuid4()), self.account_id,
                CancelTarget(self.venue, order_key[2], order_key[3]))
            context = _IndependentContext(control.cycle_generation, 'independent:' + identity.request_id,
                self.rounds.snapshot().round_number, observation.observation_id)
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
