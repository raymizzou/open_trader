"""Single LP business owner with independently owned cancellable reads."""
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from collections.abc import Mapping
import json
import hashlib
import logging
import threading
import time
from concurrent.futures import Future
from uuid import uuid4
from types import MappingProxyType

from .lp_business_writer import LPBusinessWriter, RequestRecord, ReceiptRecord, ProgressRecord
from .polymarket_lp_control import LPControlOwner
from .polymarket_lp_current_inputs import prepare_current_inputs
from .polymarket_lp_diagnostics import (summarize_iteration, project_diagnostics, append_outcome,
    diagnostic_json_copy, summarize_yield_observation, yield_diagnostic_value,
    YIELD_ROUND_LIMIT, YIELD_ROW_LIMIT)
from .polymarket_lp_current_state import (
    AppliedControl, CommittedControl, CurrentLPState, StoredControl, ValidatedStartAdmission,
    ControlRequestResult, Acceptance,
    ReceiptEvidence, SourceStamp, TerminalProof, CurrentOrderFact, Fact,
    BuyExecutionUncertainty,
    ProtectionKey, ProtectionGuard, ProtectionBaseline, SetBaseline,
    ProtectionProgress, ProtectionFailureState, QueueProgressUpdate,
    CancelTargetState, CancelEvidenceUpdate,
    OpeningProtection, OpeningEconomics, OpeningProtectionUpdate,
    ScoringState, ScoringObservation,
    OwnFillEvidence, SellState, ExitAttempt, ExitEvidenceUpdate,
    external_account_matches,
)
from .polymarket_lp_market_calculation import CalculationPolicy, calculate_current, select_current_composition
from .polymarket_lp_market_reader import MarketReader, ReadScope, freeze
from .polymarket_lp_risk import (estimate_lp_queue_position, first_observation_baseline,
    _executable_bid_value, _projected_taker_fee, _freshness, _levels, _maybe_decimal, TERMINAL_ORDER_STATES)
from .polymarket_lp import SCORING_POLL_SECONDS, SCORING_STALE_SECONDS, SCORING_FAILURE_WINDOW_SECONDS, STOP_LOSS
from .polymarket_lp_round import (
    LPRoundCoordinator, CycleStatus, FixedBuyPlan, StartStatus, CancelTarget,
    LocalOutcome, LocalStatus, BuyTarget, BuyTerms, ActionIdentity, BatchKind,
)


logger = logging.getLogger(__name__)

_CANCEL_BLOCK_REASONS = frozenset({
    'mutation_not_admitted', 'mutation_lease_not_current', 'independent_cancel_identity',
    'independent_cancel_not_current', 'independent_cancel_inflight', 'rotation_not_current',
    'protection_cancel_owned', 'protection_cancel_not_current', 'protection_evidence_not_current',
    'native_capacity', 'control_or_native_not_current', 'cancel_in_progress', 'cancel_backoff'})


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
    rotation: object = None
    rotation_outcomes: tuple = ()
    selection: object = None


@dataclass(frozen=True)
class RotationDecision:
    account_id: str
    cycle_generation: str
    observation_id: str
    victims: tuple
    incumbent_yields: tuple = ()
    candidate_yields: tuple = ()
    target_tokens: tuple = ()


@dataclass(frozen=True)
class RotationYield:
    kind: str
    identity: tuple
    state: str
    yield_pct_per_hour: Decimal | None


@dataclass(frozen=True)
class RotationOutcome:
    order_key: tuple
    status: str
    reason: str | None = None
    handle: object = None


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
class _ManualCancelIdentity:
    request_id: str
    order_keys: tuple


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


@dataclass(frozen=True)
class _ProtectionCancelOrigin:
    observation_id: str
    trade_version: int
    cycle_generation: str
    anchors: tuple[CurrentOrderFact, ...]
    ticket: _DeferredProtectionCancel
    attempt_order: CurrentOrderFact


@dataclass(frozen=True)
class _ManualOrigin:
    cycle_generation: str
    observation_id: str
    trade_version: int
    order_versions: tuple
    source_frame: object
    request: Mapping
    history: Mapping
    credential_expires_at: datetime | None
    allowed_order_ids: tuple
    named_scope: tuple | None
    named_scope_version: int | None


@dataclass(frozen=True)
class _ManualContext:
    cycle_generation: str
    batch_id: str  # real sealed manual correlation, never a352 batch/token
    round_number: int
    observation_id: str


class _ControlStore:
    def __init__(self, store, account_id, settings_loader=None):
        self.store = store
        self.settings_loader = settings_loader
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
        if self.settings_loader is not None:
            self.settings_loader()
        return StoredControl(**{**saved, 'account_id': self.account_id})

    def save(self, write):
        return self.save_with_admission(write, None)

    def save_with_admission(self, write, attempt):
        if write.account_id != self.account_id:
            raise ValueError('d2_control_account_identity')
        return CommittedControl(**self.store.lp_save_current_control({
            **asdict(write), 'account_id': self.pool_account_id}, admission=attempt))


def _transition_id(value):
    # JSON tuple encoding preserves epoch delimiters and integer identity.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class _StandaloneMutationLease:
    def __init__(self, action): self.action = action
    def allowed(self, action=None): return action is None or action == self.action
    def release(self): pass


class LPD2LiveExecution:
    def __init__(self, store, trading, *, account_id, venue, process_epoch, policy,
                 reader=None, writer=None, read_runtime=None, read_transport=None, clock=time.monotonic,
                 utc_now=lambda: datetime.now(UTC), load_settings_on_start=False):
        if (venue != 'polymarket' or type(account_id) is not str or not account_id
                or account_id != trading.config.wallet_address
                or type(process_epoch) is not str or not process_epoch):
            raise ValueError('d2_transport_identity')
        self.store, self.trading, self.account_id, self.venue = store, trading, account_id, venue
        self.process_epoch, self.policy = process_epoch, policy
        self._clock, self._utc_now = clock, utc_now
        if read_runtime is None and reader is None:
            factory_method=getattr(trading,'lp_async_source_factory',None)
            factory=factory_method(clock=clock,utc_now=utc_now,transport=read_transport) if callable(factory_method) else None
            if factory is not None:
                from .polymarket_lp_read_runtime import LPReadRuntime
                read_runtime=LPReadRuntime(factory,clock=clock,utc_now=utc_now)
        self.read_runtime=read_runtime
        self._read_store_lock=threading.RLock()
        self._read_store_owners={}
        self._read_store_values={}
        self._read_store_cancels={}
        self._read_store_error=None
        self._async_financial_ticket=None
        self._async_market_results=[]
        self._async_exact_requests={}
        self._async_exact_used={}
        self._async_exact_results={}
        self._async_generation=None
        self._async_low_sources={}
        self._read_wakeup=lambda:None
        self._risk_next={}
        self._risk_seen={}
        self._positions_full_coverage = reader is None  # owned adapter requests threshold0
        self.reader = reader if reader is not None else MarketReader(trading.lp_reader_source_port(), clock=clock)
        self.writer = writer if writer is not None else LPBusinessWriter(store)
        self.state = CurrentLPState(account_id, process_epoch, clock=clock, utc_now=utc_now)
        self.rounds = LPRoundCoordinator()
        self.control = LPControlOwner(self.state, _ControlStore(store, account_id, self.load_settings if load_settings_on_start else None), self, self,
                                      clock=clock, utc_now=utc_now)
        self._run_lock = threading.Lock()
        self._settings_lock = threading.RLock()
        self._settings_future = self._settings_thread = None
        self._committed_policy = policy
        self._settings_error = None
        self._settings_requested = None
        self._settings_loaded = False
        self._settings_repairable = False
        self._settings_required = False
        self._protection_lock = threading.Lock()
        self._closed = False
        self._admission = None
        self._record_requests = {}
        self._yield_rounds = ()
        self._yield_rounds_total = 0
        self._yield_diagnostic_instance = uuid4().hex
        self._yield_unknown_observations = ()
        # Immutable current-process ownership/provenance only. All business
        # quantities, statuses and latches remain exclusively in CurrentLPState.
        self._opening_order_associations = {}
        self._sealed_candidates = {}
        self._sealed_candidate_source = None
        self._sealed_candidate_sources = {}
        self._native_jobs = {}
        self._native_completion_generation = 0
        self._native_completion_consumed = 0
        self._manual_jobs = {}
        self._manual_groups = {}
        # Finite dispatch tickets for an already accepted protection trigger.
        # No request/native attempt exists until actual capacity is admitted.
        self._deferred_protection_cancels = {}
        self._active_sends = set()
        self._last_preparation_rejection = None
        self._last_current_evidence = None
        self._last_buy_uncertainty = None
        self._mutation_owner = self._mutation_acquire = None
        self._diagnostics = freeze(dict(version=1, latest_attempt=None, accepted_observation=None,
            admission=None, reader_coverage=None, round_start=None))
        self._protection_diagnostics = freeze(dict(entered=False, disposition='unavailable', skip_reason=None,
            attempt_id=None, attempted_at=None, observation_id=None, generation=None))
        self._diagnostic_outcomes = freeze(dict(total=0, omitted=0, by_kind={}, unknown_subtypes={}, recent=(),
            protection_cancel_outcomes={}))
        self._rotation_dispatched = None
        self._rotation_summary = None
        self._rotation_pending = ()
        self._cancel_blocks = freeze(dict(total=0, by_reason={}, recent=()))
        self._rotation_cancel_backoffs = {}

    def manual_call(self, operation):
        """Own the whole explicit command, including reliable SQL, until actual end."""
        with self.state._lock:
            if self._control_view().fatal:
                return dict(state='locked', reason='program_stopping')
            self._manual_jobs = {k:h for k,h in self._manual_jobs.items() if h._thread.is_alive()}
            identity, future = uuid4().hex, Future()
            def run():
                try: result = operation()
                except BaseException as exc: future.set_exception(exc)
                else: future.set_result(result)
            thread = threading.Thread(target=run, name='lp-d2-manual-command', daemon=False)
            handle = NativeAttempt(identity, future, thread)
            self._manual_jobs[identity] = handle
            thread.start()
        return handle.result()

    def _manual_observe(self, request=None, *, timeout=2, financial=True):
        if not self._run_lock.acquire(blocking=False):
            raise ValueError('current_read_in_progress')
        try:
            current = self.state.snapshot().current
            if not current.cycle_generation or self._control_view().fatal:
                raise ValueError('program_stopping')
            components = ('balance', 'open_orders', 'positions')
            if request is not None:
                components += (f"market:{request['condition_id']}:{request['token_id']}",)
            ticket = self.state.begin_observation(current.cycle_generation, ('balance','open_orders','positions'))
            frame = self.reader.read_current(ReadScope(self.account_id, components), time.monotonic()+timeout)
            as_of = self._utc_now()
            prepared = prepare_current_inputs(frame, None, self.policy, as_of)
            accepted = self.state.accept(ticket, frame, calculate_current(prepared, self.policy, as_of), evaluated_at=as_of)
            if accepted.observation is None or accepted.status not in ('accepted', 'partial'):
                raise ValueError('current_facts_unknown')
            snapshot = self.state.snapshot()
            if financial and any(f.state != 'known' for f in (snapshot.buy_reserved, snapshot.inventory_cost, snapshot.current_exposure)):
                raise ValueError('financial_facts_unknown')
            from .polymarket_lp_current_inputs import manual_snapshot
            return accepted.observation, manual_snapshot(frame, request, as_of) if request is not None else None, as_of
        finally:
            self._run_lock.release()

    def _manual_origin_current(self, origin):
        snapshot = self.state.snapshot()
        observation = snapshot.observation
        current = bool(self._dispatch_current(origin.cycle_generation) and observation is not None
            and observation.observation_id == origin.observation_id
            and snapshot.trade_version == origin.trade_version == observation.read_ticket.trade_version_at_start
            and observation.orders_complete and observation.positions_complete
            and tuple((o.key,o.terms_version) for o in snapshot.orders) == origin.order_versions
            and all(f.state == 'known' for f in (snapshot.buy_reserved,snapshot.inventory_cost,snapshot.current_exposure)))
        if not current:
            return False
        if origin.named_scope is not None:
            opening = snapshot.openings.get(origin.named_scope)
            if (opening is None or snapshot.scope_versions.get(origin.named_scope,0) != origin.named_scope_version
                    or opening.phase != 'opening'
                    or opening.stop_requested or opening.stop_loss_latched.value is True):
                return False
        # Waiting claim/signing/safety I/O does not extend any original authority.
        # These projections/validators are pure; no source or history is reread.
        from .polymarket_lp import PolymarketLPService
        from .polymarket_lp_current_inputs import manual_snapshot
        from .polymarket_lp_views import lp_history_eligibility
        now = self._utc_now()
        if origin.credential_expires_at is not None and now >= origin.credential_expires_at:
            return False
        try:
            values = manual_snapshot(origin.source_frame,origin.request,now)
            PolymarketLPService._validate_snapshot(origin.request,values,now=now,
                allowed_open_order_ids=origin.allowed_order_ids,reservations=())
            if origin.request.get('candidate_policy') == 'best_bid_minimum':
                from .polymarket_lp_risk import evaluate_lp_entry
                cid, token = origin.request['condition_id'],origin.request['token_id']
                catalog = origin.source_frame.sources[f'market:{cid}:{token}'].value['rewards']
                reward = next(v for v in catalog['markets'] if v['condition_id'] == cid)
                direction = dict(market=values['market'],book=values['book'],
                    reward_active=reward.get('reward_active'),daily_pool_usd=reward.get('daily_pool_usd'),
                    reward_checked_at=reward.get('reward_checked_at',reward.get('checked_at',catalog.get('checked_at'))),
                    reward_guidance_deadline=PolymarketLPService._reward_guidance_deadline(reward))
                evaluated = evaluate_lp_entry(direction,account=values['account'],now=now,reservations=(),
                    candidate=True,bid_level=origin.request.get('candidate_bid_level',1))
                if evaluated.get('state') != 'eligible':
                    return False
        except ValueError:
            return False
        return lp_history_eligibility(origin.history,now=now,
            condition_id=origin.request['condition_id'],token_id=origin.request['token_id']) is None

    @staticmethod
    def _manual_json(value):
        if isinstance(value, Decimal): return str(value)
        if isinstance(value, datetime): return value.isoformat()
        if isinstance(value, Mapping): return {str(k):LPD2LiveExecution._manual_json(v) for k,v in value.items()}
        if isinstance(value, (tuple,list)): return [LPD2LiveExecution._manual_json(v) for v in value]
        return value

    def manual_cancel(self, *, order_ids=None, condition_id=None, displayed_ids=None, permission=None):
        requested = list(order_ids if order_ids is not None else displayed_ids or ())
        try:
            observation, _, as_of = self._manual_observe(financial=False)
            if not observation.orders_complete:
                raise ValueError('current_orders_unknown')
        except ValueError as exc:
            if condition_id is not None:
                raise  # discovery failed; no authoritative empty selection exists
            return dict(requested=len(requested),canceled=[],not_canceled={oid:str(exc) for oid in requested},skipped=[])
        if permission is not None and not permission():
            return dict(requested=len(requested),canceled=[],not_canceled={oid:'mutation_blocked' for oid in requested},skipped=[])
        snapshot = self.state.snapshot()
        rows = {o.key[3]:o for o in observation.order_view.open_orders}
        if condition_id is not None:
            requested = [o.key[3] for o in observation.order_view.open_orders if o.key[1]==condition_id]
        selected, skipped = [], []
        for oid in requested:
            order = rows.get(oid)
            if order is None:
                skipped.append(dict(order_id=oid,reason='unknown_order'))
            elif order not in snapshot.active_orders:
                skipped.append(dict(order_id=oid,reason='not_active'))
            elif order not in selected:
                selected.append(order)
        if not selected:
            return dict(requested=len(requested),canceled=[],not_canceled={},skipped=skipped)
        with self.state._lock:
            current = self.state.snapshot()
            keys = [(self.account_id,self.venue,o.key[2],o.key[3]) for o in selected]
            for order,key in tuple(zip(selected,keys)):
                handle = self._native_jobs.get(key)
                if handle is not None and handle._thread.is_alive():
                    selected.remove(order);skipped.append(dict(order_id=order.key[3],reason='cancel_in_progress'))
            occupied = {h.identity.request_id for h in self._native_jobs.values() if h._thread.is_alive()}|self._active_sends
            permitted = (self._dispatch_current(observation.cycle_generation)
                and current.observation is not None and current.observation.observation_id == observation.observation_id
                and current.trade_version == observation.read_ticket.trade_version_at_start
                and all(o in current.active_orders for o in selected) and len(occupied)<2)
            if not selected:
                return dict(requested=len(requested),canceled=[],not_canceled={},skipped=skipped)
            if not permitted:
                return dict(requested=len(requested),canceled=[],not_canceled={o.key[3]:'native_or_current_not_available' for o in selected},skipped=skipped)
            identity = _ManualCancelIdentity(uuid4().hex,tuple(o.key for o in selected))
            selected = tuple(selected)
            future = Future()
            def cancel():
                try:
                    # This route has no RUN/breaker/nonLP/file-lock prerequisite.
                    if permission is not None and not permission():
                        future.set_result(dict(canceled=[],not_canceled={o.key[3]:'mutation_blocked' for o in selected}));return
                    with self.state._lock:
                        latest=self.state.snapshot()
                        if not self._dispatch_current(observation.cycle_generation) or any(o not in latest.active_orders for o in selected):
                            future.set_result(dict(canceled=[],not_canceled={o.key[3]:'program_stopping' for o in selected}));return
                    result=self.trading.cancel_orders_detailed(tuple(o.key[3] for o in selected))
                    canceled=[o.key[3] for o in selected if o.key[3] in result.get('canceled',())]
                    denied=result.get('not_canceled',{})
                    not_canceled={o.key[3]:str(denied.get(o.key[3],'cancel_result_unknown')) for o in selected if o.key[3] not in canceled}
                    stamp=SourceStamp(identity.request_id,checked_at=self._utc_now(),complete=True,state='known')
                    for order in selected:
                        if order.key[3] not in canceled:continue
                        proof=TerminalProof(order.key,'CANCELED',order.original_size,
                            Fact('unknown',sources=(stamp,),reason_codes=('cancel_final_fill_unknown',)),stamp,True)
                        disposition=self.state.accept_receipt_evidence(ReceiptEvidence(
                            identity.request_id+':'+order.key[3],identity.request_id,self.account_id,self.process_epoch,
                            observation.cycle_generation,order.key,order.side,order.terms_version,terminal_proof=proof))
                        self._last_current_evidence=(identity.request_id,'manual_cancel',disposition)
                    future.set_result(dict(canceled=canceled,not_canceled=not_canceled))
                except BaseException as exc:future.set_exception(exc)
            thread=threading.Thread(target=cancel,name='lp-d2-manual-cancel',daemon=False)
            handle=NativeAttempt(identity,future,thread)
            for order in selected:self._native_jobs[(self.account_id,self.venue,order.key[2],order.key[3])]=handle
            thread.start()
        result=handle.result()
        return dict(requested=len(requested),**result,skipped=skipped)

    def _bind_manual_group(self, group):
        original = group['identity']
        observation, values, as_of = self._manual_observe(original)
        with self.state._lock:
            snapshot = self.state.snapshot()
            if not self._protection_frame_current(snapshot):
                raise ValueError('current_facts_unknown')
            ids = tuple(group['entry_order_ids'])+tuple(group['augment_order_ids'])
            orders = tuple(o for o in snapshot.active_orders if o.key[3] in ids)
            if not ids or len(orders) != len(set(ids)):
                raise ValueError('manual_order_identity_unknown')
            scope = (self.account_id,original['condition_id'],original['token_id'],
                self.process_epoch+':manual:'+original['session_id'])
            for order in orders:
                association = self._opening_order_associations.get(order.key)
                if (order.side != 'BUY' or order.key[:3] != scope[:3]
                        or order.source != observation.order_view.orders_source
                        or association is not None and association.scope != scope
                        or any(f.state != 'known' for f in (order.price,order.original_size,order.size_matched,order.remaining,order.order_type,order.expiration))
                        or order.order_type.value != 'GTD' or (order.expiration.value.timestamp()
                            if isinstance(order.expiration.value,datetime) else order.expiration.value) != original['expiration']):
                    raise ValueError('manual_order_identity_unknown')
                if order.key[3] in group['entry_order_ids'] and (
                        order.price.value != Decimal(original['price']) or order.original_size.value != Decimal(original['quantity'])):
                    raise ValueError('manual_order_terms_mismatch')
            unknown = Fact('unknown')
            stamp = SourceStamp('manual-identity:'+original['session_id'],state='known',complete=True)
            opening = OpeningProtection(scope,'opening',None,ids,unknown,
                OpeningEconomics(scope,unknown,unknown,unknown,unknown,False),unknown,None,unknown,
                Fact('known',datetime.fromisoformat(original['review_at']),(stamp,)),'none',False,
                ScoringState(orders[0].key,'entry',unknown,None,None,None,orders[0].source),
                snapshot.scope_versions.get(scope,0)+1)
            if self.state.apply_protection(OpeningProtectionUpdate(
                    self._protection_guard(snapshot,scope,orders,observation=True),opening)) != 'applied':
                raise ValueError('manual_group_binding_stale')
            self._manual_groups[original['session_id']] = (scope,MappingProxyType(dict(original)))
            for order in orders:
                self._opening_order_associations[order.key] = _OpeningOrderAssociation(scope,order.key,
                    original.get('request_id','manual-identity:'+original['session_id']),
                    observation.observation_id,order.source)
            return opening

    def manual_status(self, session_id):
        if not session_id:
            groups = tuple(self._manual_groups)
            if len(groups) != 1:
                return dict(state='none',session_id=None,reason='session_id_required' if groups else 'session_not_found')
            session_id = groups[0]
        with self.state._lock:
            binding=self._manual_groups.get(session_id)
            opening=self.state.snapshot().openings.get(binding[0]) if binding else None
            if opening is None:
                return dict(state='needs_attention',session_id=session_id,reason='session_binding_pending')
            entry_ids=opening.entry_order_ids[:1]
            augment_ids=opening.entry_order_ids[1:]
        def amount(fact):
            return str(fact.value) if fact.state == 'known' and fact.value is not None else None
        return dict(state={'opening':'entry_open','closed':'complete'}.get(opening.phase,opening.phase),
            session_id=session_id,entry_order_id=next(iter(entry_ids),None),
            augment_order_ids=list(augment_ids),review_at=opening.review_at.value.isoformat()
                if opening.review_at.state == 'known' else None,
            stop_requested=opening.stop_requested,stop_loss_latched=opening.stop_loss_latched.value,
            buy_cost=amount(opening.economics.buy_cost_usd),fees=amount(opening.economics.fees_usd))

    def manual_stop(self, session_id, permission):
        if not session_id:
            snapshot = self.state.snapshot()
            groups = [sid for sid,(scope,_) in self._manual_groups.items()
                if scope in snapshot.openings and snapshot.openings[scope].phase != 'closed']
            if len(groups)>1:
                return dict(state='rejected',reason='session_ambiguous',session_ids=groups)
            if not groups:
                return dict(state='none',session_id=None)
            session_id=groups[0]
        try:
            group=self.store.lp_manual_group(self.account_id,str(session_id))
        except ValueError as exc:
            if str(exc)=='manual_group_unavailable':return dict(state='none',session_id=None)
            return dict(state='needs_attention',session_id=session_id,reason=str(exc))
        if session_id not in self._manual_groups:
            try:self._bind_manual_group(group)
            except ValueError as exc:return dict(state='needs_attention',session_id=session_id,reason=str(exc))
        try:observation,_,_=self._manual_observe(financial=False)
        except ValueError as exc:return dict(state='needs_attention',session_id=session_id,reason=str(exc))
        if not permission():
            return dict(state='locked',reason='mutation_blocked')
        self._associate_current_manual_sells()
        with self.state._lock:
            snapshot=self.state.snapshot();scope=self._manual_groups[session_id][0]
            if not self._dispatch_current(observation.cycle_generation) or not self._protection_frame_current(snapshot):
                return dict(state='needs_attention',session_id=session_id,reason='current_facts_unknown')
            opening=snapshot.openings[scope]
            if opening.phase=='closed':return dict(state='complete',session_id=session_id)
            orders=self._opening_orders(snapshot,opening)
            review=replace(opening,phase='review',stop_requested=True,review_status='awaiting_reconciliation',
                scope_version=snapshot.scope_versions.get(scope,0)+1)
            disposition=self.state.apply_protection(OpeningProtectionUpdate(
                self._protection_guard(snapshot,scope,orders,observation=True),review))
            if disposition!='applied':
                return dict(state='needs_attention',session_id=session_id,reason='current_facts_unknown')
            targets=[o.key[3] for o in orders if o in snapshot.active_orders]
        result=self.manual_cancel(order_ids=targets,permission=permission) if targets else dict(not_canceled={})
        return dict(state='needs_attention' if result['not_canceled'] else 'review',session_id=session_id,
            stop_requested=True,review_status='awaiting_reconciliation',
            review_at=opening.review_at.value.isoformat() if opening.review_at.state=='known' else None,
            stop_loss_latched=opening.stop_loss_latched.value)

    def manual_group(self, session_id):
        try:
            group = self.store.lp_manual_group(self.account_id,session_id)
        except ValueError as exc:
            reason = str(exc)
            raise ValueError('session_not_found' if reason == 'manual_group_unavailable' else reason) from exc
        if group is None:
            raise ValueError('session_not_found')
        binding = self._manual_groups.get(session_id)
        if binding is None:
            self._bind_manual_group(group)
            binding = self._manual_groups[session_id]
        opening = self.state.snapshot().openings.get(binding[0])
        if opening is None:
            raise ValueError('session_needs_attention')
        if opening.stop_requested or opening.phase == 'review':
            raise ValueError('session_review_exit')
        if opening.phase != 'opening':
            raise ValueError({'stop_loss_exit':'session_stop_loss_exit','passive_exit':'session_passive_exit',
                'complete':'session_not_active'}.get(opening.phase,'session_needs_attention'))
        if opening.stop_loss_latched.value is True:
            raise ValueError('session_stop_loss_exit')
        return group

    def _manual_augment_request(self, service, group, payload):
        original = group['identity']
        quantity = _maybe_decimal(payload.get('quantity'))
        if quantity is None or quantity <= 0:
            raise ValueError('quantity_invalid')
        price = _maybe_decimal(original['price'])
        if payload.get('price') is not None:
            price = _maybe_decimal(payload['price'])
            if price is None or price <= 0:
                raise ValueError('price_invalid')
        if price is None or price <= 0:
            raise ValueError('entry_price_unknown')
        for key in ('market_id','condition_id','token_id','outcome','review_at'):
            if key in payload and str(payload[key]) != str(original[key]):
                raise ValueError('manual_group_identity_mismatch')
        return service._normalize_request({**{k:original[k] for k in
            ('market_id','condition_id','token_id','outcome','review_at','price')},
            'price':price, 'quantity':quantity})

    def _manual_validate_augment(self, service, group, request, values, as_of):
        ids = tuple(group['entry_order_ids'])+tuple(group['augment_order_ids'])
        snapshot = self.state.snapshot()
        scope = self._manual_groups[group['identity']['session_id']][0]
        allowed = []
        for order in snapshot.active_orders:
            if order.key[3] not in ids:
                continue
            association = self._opening_order_associations.get(order.key)
            if (association is None or association.scope != scope or order.side != 'BUY'
                    or order.key[1:3] != (request['condition_id'],request['token_id'])):
                raise ValueError('manual_order_identity_unknown')
            if order.size_matched.state != 'known' or order.size_matched.value != 0:
                raise ValueError('session_needs_attention')
            allowed.append(order.key[3])
        session = dict(group['identity'],entry_order_id=next(iter(group['entry_order_ids']),None),
            augment_order_ids=group['augment_order_ids'])
        reason = service._augment_price_rejection(session,values,request['price'])
        if reason:
            raise ValueError(reason)
        return service._validate_snapshot(request,values,now=as_of,allowed_open_order_ids=allowed,reservations=())

    def manual_augment_preview(self, service, payload):
        try:
            group = self.manual_group(str(payload.get('session_id') or ''))
            request = self._manual_augment_request(service,group,payload)
            observation,values,as_of = self._manual_observe(request)
            facts = self._manual_validate_augment(service,group,request,values,as_of)
        except ValueError as exc:
            return dict(state='rejected',reason=str(exc))
        expires = as_of+timedelta(seconds=10)
        preview_id = self.store.create_preview(self._manual_json(dict(request,
            session_id=group['identity']['session_id'],preflight=facts)),
            expires_at=expires.isoformat(),created_at=as_of.isoformat(), lp_priority="reliable")
        return dict(state='previewed',preview_id=preview_id,expires_at=expires.isoformat(),
            request=dict(session_id=group['identity']['session_id'],quantity=request['quantity']),
            price=request['price'],preflight=facts,
            queue_protection_estimate=service._queue_protection_preview_estimate(request,values))

    def _manual_candidate_request(self, service, identity, observation, values, as_of):
        from .polymarket_lp_risk import evaluate_lp_entry
        from .polymarket_lp_views import _next_review_at
        cid, token = identity['condition_id'], identity['token_id']
        if not service._candidate_allowed(((cid, token),)):
            raise ValueError('candidate_cooling_down')
        catalog = observation.source_frame.sources[f'market:{cid}:{token}'].value['rewards']
        reward = next(v for v in catalog['markets'] if v['condition_id'] == cid)
        direction = dict(market=values['market'], book=values['book'],
            reward_active=reward.get('reward_active'), daily_pool_usd=reward.get('daily_pool_usd'),
            reward_checked_at=reward.get('reward_checked_at', reward.get('checked_at', catalog.get('checked_at'))),
            reward_guidance_deadline=service._reward_guidance_deadline(reward))
        level = identity.get('candidate_bid_level', identity.get('bid_level', 1))
        evaluated = evaluate_lp_entry(direction, account=values['account'], now=as_of,
            reservations=(), candidate=True, bid_level=level)
        if evaluated.get('state') != 'eligible':
            raise ValueError(next(iter(evaluated.get('reason_codes', ())), 'candidate_not_eligible'))
        guide = evaluated['guidance']
        return service._normalize_request(dict(identity, price=guide['price'], quantity=guide['quantity'],
            review_at=_next_review_at(as_of), candidate_policy='best_bid_minimum', candidate_bid_level=level))

    def manual_preview(self, service, payload, *, candidate=False):
        try:
            if candidate:
                if not isinstance(payload, Mapping):
                    raise ValueError('candidate_invalid')
                request = {}
                for key in ('market_id', 'condition_id', 'token_id'):
                    value = str(payload.get(key) or '').strip()
                    if not value:
                        raise ValueError(key+'_invalid')
                    request[key] = value
                request['outcome'] = str(payload.get('outcome') or '').upper()
                if request['outcome'] not in ('YES', 'NO'):
                    raise ValueError('outcome_invalid')
                if 'candidate_bid_level' in payload:
                    request['candidate_bid_level'] = payload['candidate_bid_level']
                observation, values, as_of = self._manual_observe(request)
                request = self._manual_candidate_request(service, request, observation, values, as_of)
            else:
                request = service._normalize_request(payload)
                observation, values, as_of = self._manual_observe(request)
            facts = service._validate_snapshot(request, values, now=as_of, reservations=())
        except ValueError as exc:
            return dict(state='rejected', reason=str(exc))
        expires = as_of + timedelta(seconds=10)
        preview_id = self.store.create_preview(self._manual_json(dict(request, preflight=facts)),
            expires_at=expires.isoformat(), created_at=as_of.isoformat(), lp_priority="reliable")
        return dict(state='previewed', preview_id=preview_id, expires_at=expires.isoformat(),
            request=request, preflight=facts,
            queue_protection_estimate=service._queue_protection_preview_estimate(request, values))

    def manual_entry(self, service, payload, key, lease, *, preview_id=None, group=None):
        credential = None
        if preview_id is not None:
            credential = self.store.lp_preview(preview_id)
            if credential is None:
                return dict(state='rejected',reason='preview_not_found')
            if self._utc_now() >= datetime.fromisoformat(credential['expires_at'].replace('Z','+00:00')):
                return dict(state='rejected',reason='preview_expired')
            if credential.get('consumed_at') is not None:
                return dict(state='rejected',reason='preview_consumed')
            if credential.get('session_id') != (group['identity']['session_id'] if group else None):
                return dict(state='rejected',reason='preview_session_mismatch')
            payload = credential
        if group:
            try:
                request = self._manual_augment_request(service,group,payload)
            except ValueError as exc:
                return dict(state='rejected',reason=str(exc))
        else:
            request = service._normalize_request(payload)  # malformed entry keeps ValueError/HTTP400
        named_scope = self._manual_groups[group['identity']['session_id']][0] if group else None
        named_version = self.state.snapshot().scope_versions.get(named_scope,0) if named_scope is not None else None
        try:
            observation, values, as_of = self._manual_observe(request)
            facts = (self._manual_validate_augment(service,group,request,values,as_of) if group else
                service._validate_snapshot(request, values, now=as_of, reservations=()))
            if credential is not None:
                prior = credential.get('preflight')
                best = prior.get('best_bid') if isinstance(prior,Mapping) else None
                if best is not None and Decimal(str(best)) != facts['best_bid']:
                    raise ValueError('best_bid_changed')
                if request.get('candidate_policy') == 'best_bid_minimum':
                    fresh = self._manual_candidate_request(service,request,observation,values,as_of)
                    if (fresh['price'],fresh['quantity']) != (request['price'],request['quantity']):
                        raise ValueError('candidate_changed')
            if group is None and any(o.key[1] == request['condition_id'] for o in self.state.snapshot().active_orders):
                raise ValueError('market_already_participating')
            history, reason = service._lp_history_fact(request['condition_id'], request['token_id'], now=as_of)
            if reason:
                raise ValueError(reason)
        except ValueError as exc:
            return dict(state='rejected', reason=str(exc))
        snapshot = self.state.snapshot()
        origin = _ManualOrigin(observation.cycle_generation, observation.observation_id,
            observation.read_ticket.trade_version_at_start, tuple((o.key,o.terms_version) for o in snapshot.orders),
            observation.source_frame,freeze(request),freeze(history),
            datetime.fromisoformat(credential['expires_at'].replace('Z','+00:00')) if credential else None,
            tuple(group['entry_order_ids'])+tuple(group['augment_order_ids']) if group else (),named_scope,named_version)
        sampled_round = self.rounds.snapshot().round_number
        command = {k:request[k] for k in ('market_id','condition_id','token_id','outcome','price','quantity','review_at')}
        command.update(kind='augment' if group else 'start' if preview_id is not None else 'entry',account_id=self.account_id,process_epoch=self.process_epoch,
            request_id=uuid4().hex,session_id=group['identity']['session_id'] if group else uuid4().hex,idempotency_key=key,expiration=facts['entry_expiration'],
            order_type='GTD',post_only=True)
        if request.get('estimated_target_quantity') is not None:
            command['estimated_target_quantity'] = request['estimated_target_quantity']
        try:
            claimed = self.store.lp_claim_manual_command(command,preview_id=preview_id)
        except ValueError as exc:
            return dict(state='rejected',reason=str(exc))
        except Exception:
            return dict(state='needs_attention',reason='manual_identity_unknown')
        if not claimed['claimed']:
            return claimed['response']
        command = claimed['command']
        terms = BuyTerms(Decimal(command['price']),Decimal(command['quantity']),'GTD',command['expiration'],True,())
        action = ActionIdentity(command['request_id'],self.account_id,BuyTarget(self.venue,command['token_id'],command['market_id'],terms))
        context = _ManualContext(origin.cycle_generation, 'manual:'+command['request_id'],
            sampled_round, origin.observation_id)
        record = RequestRecord(self.account_id,self.process_epoch,action.request_id,'d2:'+action.request_id,
            command['condition_id'],command['token_id'],'BUY',terms.price,terms.quantity,as_of,
            venue=self.venue,cycle_generation=context.cycle_generation,batch_id=context.batch_id,
            round_number=context.round_number,observation_id=context.observation_id,order_type='GTD',
            expiration=terms.expiration,post_only=True)
        self.writer.offer(record)
        with self.state._lock:
            occupied = {h.identity.request_id for h in self._native_jobs.values() if h._thread.is_alive()} | self._active_sends
            admitted = len(occupied) < 2 and self._manual_origin_current(origin)
            if admitted:
                self._active_sends.add(action.request_id)
        try:
            outcome = (self._buy_admitted(context, action, record, lease, manual_origin=origin,
                manual_command=command, submit_source=observation.source_frame.sources[f"market:{command['condition_id']}:{command['token_id']}"])
                if admitted else LocalOutcome(action,LocalStatus.NOT_SENT,True))
        finally:
            with self.state._lock:
                self._active_sends.discard(action.request_id)
        status = ('accepted' if outcome.status == LocalStatus.ACCEPTED else 'rejected'
            if outcome.status == LocalStatus.REJECTED else 'not_sent' if outcome.status == LocalStatus.NOT_SENT else 'unknown')
        if status == 'accepted':
            response = dict(state='entry_open',session_id=command['session_id'],entry_order_id=outcome.venue_order_id,
                price=command['price'],quantity=command['quantity'],review_at=command['review_at'],
                entry_expiration=command['expiration'],submit_status='accepted')
            if group:
                response.update(entry_order_id=next(iter(group['entry_order_ids']),None),
                    augment_order_id=outcome.venue_order_id,order_id=outcome.venue_order_id)
        elif status in ('rejected','not_sent'):
            response = dict(state='rejected',session_id=command['session_id'],reason='entry_submit_rejected',submit_status=status)
        else:
            response = dict(state='needs_attention',session_id=command['session_id'],submit_status='unknown')
        try:
            disposition = self.store.lp_finish_manual_command(command,outcome=status,response=response,
                venue_order_id=outcome.venue_order_id if status != 'not_sent' else None)
            if disposition == 'rejected':
                raise ValueError('manual_result_identity_conflict')
        except Exception:
            return dict(state='needs_attention',session_id=command['session_id'],submit_status='unknown',reason='manual_result_unknown')
        return response

    def _register_manual_opening(self, command, order_key, record, source):
        with self.state._lock:
            snapshot = self.state.snapshot()
            scope = (self.account_id,command['condition_id'],command['token_id'],self.process_epoch+':manual:'+command['session_id'])
            previous = self._opening_order_associations.get(order_key)
            if previous is not None and previous.scope != scope:
                raise ValueError('manual_order_already_owned')
            order = next(o for o in snapshot.orders if o.key == order_key)
            unknown = Fact('unknown')
            stamp = SourceStamp(command['request_id'],checked_at=record.requested_at,complete=True,state='known')
            existing = snapshot.openings.get(scope)
            if existing is not None:
                opening = replace(existing,entry_order_ids=tuple(dict.fromkeys((*existing.entry_order_ids,order_key[3]))),
                    scope_version=snapshot.scope_versions.get(scope,0)+1)
            else:
                opening = OpeningProtection(scope,'opening',None,(order_key[3],),order.size_matched,
                    OpeningEconomics(scope,unknown,unknown,unknown,unknown,False),Fact('known',False,(order.source,)),
                    None,unknown,Fact('known',datetime.fromisoformat(command['review_at']),(stamp,)),'none',False,
                    ScoringState(order.key,'entry',unknown,None,None,None,order.source),snapshot.scope_versions.get(scope,0)+1)
            scope_orders = tuple(o for o in snapshot.orders if o.key[:3] == scope[:3]
                and o.key[3] in opening.entry_order_ids)
            if self.state.apply_protection(OpeningProtectionUpdate(self._protection_guard(snapshot,scope,scope_orders),opening)) != 'applied':
                raise ValueError('manual_opening_not_applied')
            if command['session_id'] not in self._manual_groups:
                self._manual_groups[command['session_id']] = (scope, MappingProxyType(dict(command)))
            self._opening_order_associations[order_key] = _OpeningOrderAssociation(scope,order_key,command['request_id'],
                record.observation_id,stamp,requested_at=record.requested_at)
            book = source.value.get('book') if source and source.complete and not source.error else None
            if not isinstance(book,Mapping):
                return
            baseline = first_observation_baseline(book,price=order.price.value,own_remaining=Decimal(0))
            if baseline['state'] != 'known':
                return
            key = ProtectionKey('queue_bucket',scope,order.price.value,None)
            book_stamp = self._source_stamp(source)
            fact = ProtectionBaseline(key,(order_key[3],),order.price.value,Fact('known',baseline['baseline_front'],(book_stamp,)),
                'submit',book.get('received_at'),book.get('timestamp'),book.get('hash'),1,record.requested_at,Decimal('.5'),book_stamp)
            self.state.apply_protection(SetBaseline(self._protection_guard(self.state.snapshot(),key,(order,)),fact))

    def bind_mutation_admission(self, owner, acquire):
        if self._mutation_owner is not None or not callable(acquire):
            raise ValueError('d2_mutation_admission_already_bound')
        with self.state._lock:
            if self._run_lock.locked() or self._active_sends or any(h._thread.is_alive() for h in self._native_jobs.values()):
                raise ValueError('d2_mutation_admission_busy')
            self._mutation_owner, self._mutation_acquire = owner, acquire

    def _acquire_mutation(self, action):
        return self._mutation_acquire(action) if self._mutation_acquire else _StandaloneMutationLease(action)

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

    @staticmethod
    def _policy_from_settings(saved):
        from .polymarket_lp_accounting import ALLOCATION_FIELDS, allocation_settings
        allocation_settings(saved)
        return CalculationPolicy(None if saved['budget_usd'] is None else Decimal(saved['budget_usd']),
            saved['target_buy_count'], buy_price_level=saved['buy_price_level'],
            config_version=saved['config_version'], trading_config_version=saved['trading_config_version'],
            api_retry_interval_seconds=saved['api_retry_interval_seconds'],
            order_check_interval_seconds=saved['order_check_interval_seconds'],
            round_interval_seconds=saved['round_interval_seconds'],
            allocation_mode=saved.get('allocation_mode', 'minimum'),
            allocation_weights=tuple(saved.get('allocation_weights', ())),
            allocation_shadow_enabled=saved.get('allocation_shadow_enabled', False),
            allocation_fields=tuple(key for key in ALLOCATION_FIELDS if key in saved))

    def _settings_account(self):
        return hashlib.sha256(self.account_id.strip().casefold().encode('utf-8')).hexdigest()

    def load_settings(self):
        if not self._run_lock.acquire(blocking=False):
            raise ValueError('settings_iteration_pending')
        try:
            with self._settings_lock:
                if self._settings_future is not None:
                    raise ValueError('settings_save_pending')
            with self._settings_lock:
                self._settings_required = True
            try:
                saved = self.store.lp_load_current_settings(self._settings_account())
                policy = self._policy_from_settings(saved)
            except BaseException as exc:
                allocation_error = isinstance(exc, ValueError) and (str(exc).startswith('allocation_')
                    or str(exc) == 'rank_weights_requires_c2')
                with self._settings_lock:
                    self._settings_loaded = False
                    self._settings_repairable = allocation_error
                    self._settings_error = str(exc) if allocation_error else type(exc).__name__
                if not allocation_error:
                    raise
                # Keep control and repair available; unloaded settings still block native plans.
                saved = self.store.lp_load_current_settings(self._settings_account(), validate_allocation=False)
                with self._settings_lock:
                    self.policy = self._committed_policy = self._policy_from_settings(saved)
                return None
            with self._settings_lock:
                self.policy = self._committed_policy = policy
                self._settings_repairable = False
                self._settings_loaded, self._settings_error = True, None
            return policy
        finally:
            self._run_lock.release()

    def _collect_settings(self, *, publish=False):
        # Caller owns settings lock; native SQL is never run under it.
        if (self._settings_future is not None and self._settings_future.done()
                and not self._settings_thread.is_alive()):
            future, self._settings_future = self._settings_future, None
            try:
                self._committed_policy = self._policy_from_settings(future.result())
            except BaseException as exc:
                self._settings_error = type(exc).__name__
            else:
                self._settings_loaded, self._settings_repairable = True, False
                self._settings_error = None
        if publish:
            self.policy = self._committed_policy
        return self._settings_future is not None or self.policy is not self._committed_policy

    def settings_status(self):
        boundary = self._run_lock.acquire(blocking=False)
        try:
            with self._settings_lock:
                pending = self._collect_settings(publish=boundary)
                return MappingProxyType(dict(effective=self.policy, committed=self._committed_policy,
                    requested=self._settings_requested if pending else None,
                    pending=pending, native_inflight=bool(self._settings_thread and self._settings_thread.is_alive()),
                    error=self._settings_error, loaded=self._settings_loaded,
                    effective_round_interval_seconds=None))
        finally:
            if boundary:
                self._run_lock.release()

    def configure(self, payload, *, actor=None):
        from .polymarket_lp_accounting import ALLOCATION_FIELDS
        if not isinstance(payload, Mapping) or not payload:
            raise ValueError('auto_config_invalid')
        fields = {'budget_usd', 'target_buy_count', 'buy_price_level',
                  'api_retry_interval_seconds', 'order_check_interval_seconds', *ALLOCATION_FIELDS}
        if 'round_interval_seconds' in payload:
            raise ValueError('round_interval_seconds_retired_in_continuous_mode')
        if set(payload) - fields - {'expected_config_version'}:
            raise ValueError('auto_config_invalid')
        with self._settings_lock:
            self._collect_settings()
            if ((not self._settings_loaded and not self._settings_repairable) or self._settings_future is not None
                    or self.policy is not self._committed_policy):
                raise ValueError('settings_save_pending')
            if self._closed:
                raise ValueError('settings_stopping')
            policy = self.policy
            expected = payload.get('expected_config_version', policy.config_version)
            changes = {key: payload[key] for key in fields if key in payload}
            if type(expected) is not int or expected != policy.config_version or not changes:
                raise ValueError('config_version_changed')
            values = dict(account_id=self._settings_account(), budget_usd=policy.budget,
                target_buy_count=policy.target_count, buy_price_level=policy.buy_price_level,
                config_version=policy.config_version, trading_config_version=policy.trading_config_version,
                api_retry_interval_seconds=policy.api_retry_interval_seconds,
                order_check_interval_seconds=policy.order_check_interval_seconds,
                round_interval_seconds=policy.round_interval_seconds)
            values.update({key: list(policy.allocation_weights) if key == 'allocation_weights'
                else getattr(policy, key) for key in policy.allocation_fields})
            if 'budget_usd' in changes and changes['budget_usd'] is None:
                raise ValueError('auto_config_invalid')
            proposed = self.store._lp_current_settings_values({**values, **changes}, self._settings_account())
            if actor is not None and type(actor) is not str:
                raise ValueError('auto_config_invalid')
            trading = bool(set(changes) & {'budget_usd', 'target_buy_count', 'buy_price_level'})
            with self.state._lock:
                status, snapshot = self.control.status(), self.state.snapshot()
                if status.fatal is not None or snapshot.current.shutdown_requested:
                    raise ValueError('settings_stopping')
                if trading:
                    if not {'budget_usd', 'target_buy_count'} <= set(changes):
                        raise ValueError('auto_config_invalid')
                    if (snapshot.current.effective_running or status.requested_running
                            or status.requested_version != status.saved_version or status.native_save_in_flight
                            or status.save_state != 'saved' or self._run_lock.locked()
                            or self.rounds.snapshot().active_batch_id is not None
                            or self._active_sends or any(h._thread.is_alive() for h in self._native_jobs.values())
                            or any(o.side == 'BUY' for o in snapshot.active_orders)
                            or any(o.phase != 'closed' and not self._settings_zero_fill_resolved(snapshot, o)
                                for o in snapshot.openings.values())):
                        raise ValueError('pause_and_finish_automatic_buys_before_configuring')
                    observation = snapshot.observation
                    from .polymarket_lp_current_inputs import _current_account
                    if (observation is None or not observation.orders_complete or not observation.positions_complete
                            or observation.cycle_generation != snapshot.current.cycle_generation
                            or observation.read_ticket.trade_version_at_start != snapshot.trade_version
                            or not _current_account(observation.source_frame, self._utc_now()).complete
                            or any(f.state != 'known' for f in (snapshot.buy_reserved,
                                snapshot.inventory_cost, snapshot.current_exposure))
                            or snapshot.new_capacity.state != 'known' and policy.budget is not None):
                        raise ValueError('financial_facts_unknown')
            self._settings_requested = self._policy_from_settings({**proposed,
                'config_version': policy.config_version+1,
                'trading_config_version': policy.trading_config_version+int(trading)})
            future = self._settings_future = Future()
            self._settings_error = None
            def save():
                try:
                    saved = self.store.lp_save_current_settings(self._settings_account(),
                        expected_config_version=expected, changes=changes, actor=actor)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(saved)
            self._settings_thread = threading.Thread(target=save, name='lp-settings-save', daemon=False)
            self._settings_thread.start()
        return self.settings_status()

    def _settings_zero_fill_resolved(self, snapshot, opening):
        """Admission proof only; an unfilled opening keeps its lifecycle/economics."""
        scope, observation = opening.scope, snapshot.observation
        if (scope[0] != self.account_id or not opening.entry_order_ids or observation is None
                or observation.cycle_generation != snapshot.current.cycle_generation
                or observation.read_ticket.trade_version_at_start != snapshot.trade_version
                or not observation.orders_complete or not observation.positions_complete):
            return False
        sources = (observation.order_view.orders_source, observation.components['positions'])
        def complete(source):
            return bool(source.source_id and source.state == 'known' and source.complete
                and not source.error_code and source.checked_at is not None)
        if not all(complete(source) for source in sources):
            return False
        keys = {a.order_key for a in self._opening_order_associations.values() if a.scope == scope}
        entries = {(*scope[:3], oid) for oid in opening.entry_order_ids}
        orders = {o.key: o for o in snapshot.orders if o.key in keys}
        if not entries <= keys or set(orders) != keys:
            return False
        for key, order in orders.items():
            proof = order.terminal_proof
            if (key[:3] != scope[:3] or key in entries and order.side != 'BUY'
                    or any(f.state != 'known' for f in (order.price, order.original_size,
                        order.order_type, order.expiration, order.size_matched))
                    or order.original_size.value <= 0 or order.size_matched.value != 0
                    or proof is None or not proof.identity_verified or proof.order_key != key
                    or proof.status not in TERMINAL_ORDER_STATES or order.venue_status != proof.status
                    or proof.original_size.state != 'known' or proof.original_size.value != order.original_size.value
                    or proof.matched.state != 'known' or proof.matched.value != 0
                    or not complete(proof.source)
                    or any(source.checked_at < proof.source.checked_at for source in sources)):
                return False
            own = snapshot.own_fills.get(key)
            if own is not None and (not own.complete_for_target or not complete(own.source)
                    or any(f.state != 'known' or f.value != 0
                        for f in (own.quantity, own.notional_usd, own.fees_usd))):
                return False
        positions = tuple(p for p in snapshot.positions
            if (p.account_id, p.condition_id, p.token_id) == scope[:3])
        if (len(positions) > 1 or not positions and not self._positions_full_coverage
                or any(p.quantity.state != 'known' or p.quantity.value != 0 for p in positions)
                or any(o.side == 'SELL' and o.key[:3] == scope[:3] for o in snapshot.active_orders)):
            return False
        sell = snapshot.sells.get(scope)
        if sell is not None and (not sell.position_reconciled
                or sell.residual_quantity.state != 'known' or sell.residual_quantity.value != 0
                or sell.current_sells):
            return False
        attempts = tuple(a for a in (sell.passive, sell.protected) if a) if sell else ()
        attempts += tuple(a for (episode, _), a in snapshot.resolved_exit_attempts.items() if episode == scope)
        return all(a.native_state == 'ended' and (a.state == 'rejected' and a.order_key is None
            or a.state == 'terminal' and a.order_key in orders) for a in attempts)

    def request_running(self, running, *, actor=None):
        with self._settings_lock:
            if running is True and self._collect_settings():
                return ControlRequestResult('rejected', 'settings_save_pending', self.control.status())
            return self.control.request_running(running, actor=actor)

    def poll(self):
        self.settings_status()
        return self.control.poll(self._clock())

    def set_read_wakeup(self,wakeup):
        self._read_wakeup=wakeup
        if self.read_runtime is not None:self.read_runtime.start(wakeup)

    def _reconcile_async_reads(self):
        from .polymarket_lp_read_runtime import ReadDemand
        snapshot=self.state.snapshot();generation=snapshot.current.cycle_generation
        if not generation or self.read_runtime is None:return
        self.read_runtime.start(self._read_wakeup)
        if self._async_generation!=generation:
            self._async_generation=generation;self._async_low_sources={}
        if self._async_financial_ticket is not None and self._async_financial_ticket.cycle_generation!=generation:
            self._async_financial_ticket=None
        if self._async_financial_ticket is None:
            self._async_financial_ticket=self.state.begin_observation(generation,('balance','open_orders','positions'))
        ticket=self._async_financial_ticket
        demands=[ReadDemand(self.account_id,self.process_epoch,generation,'O',snapshot.trade_version,
            kind='financial',sources=('balance','open_orders','positions'),observation_id=ticket.observation_id)]
        for order in snapshot.active_orders:
            demands.append(ReadDemand(self.account_id,self.process_epoch,generation,order.key[3],order.terms_version,
                order.key[1],order.key[2],kind='buy' if order.side=='BUY' else 'sell'))
        for position in snapshot.positions:
            if position.quantity.state=='known' and position.quantity.value>0:
                demands.append(ReadDemand(self.account_id,self.process_epoch,generation,
                    'position:'+position.condition_id+':'+position.token_id,0,position.condition_id,position.token_id,kind='position'))
        valid={k:d for k,d in self._async_exact_requests.items() if d.generation==generation
            and d.terms_version==snapshot.trade_version and self._clock()-self._async_exact_used.get(k,0)<=65}
        self._async_exact_requests=valid
        self._async_exact_used={k:v for k,v in self._async_exact_used.items() if k in valid}
        self._async_exact_results={k:v for k,v in self._async_exact_results.items() if k in valid}
        demands.extend(valid.values())
        self.read_runtime.reconcile(demands)

    def _collect_async_reads(self):
        self._reconcile_async_reads()
        financial=None;markets=[]
        for result in self.read_runtime.drain_results():
            current=self.state.snapshot().current
            if (result.demand.account!=self.account_id or result.demand.process_epoch!=self.process_epoch
                    or result.demand.generation!=current.cycle_generation):continue
            if result.demand.kind=='financial':
                ticket=self._async_financial_ticket
                if ticket is not None and result.demand.observation_id==ticket.observation_id:
                    from .polymarket_lp_market_reader import CapturedSources
                    sources=dict(result.sources)
                    previous=self.state.snapshot().observation
                    if previous is not None and previous.cycle_generation==result.demand.generation:
                        sources.update({k:v for k,v in previous.source_frame.sources.items() if k.startswith('market')})
                    financial=(ticket,MarketReader.from_captured_sources(CapturedSources(self.account_id,sources)))
                    self._async_financial_ticket=None
            elif result.demand.kind=='buy':markets.append(result)
            elif result.demand.kind in ('sell','position'):
                from .polymarket_lp_async_source import market_value,closed_position_value
                from .polymarket_lp_market_reader import SourceResult
                try:
                    source=SourceResult(str(result.demand.key)+':'+str(result.attempt),
                        (closed_position_value(result.demand.condition_id,result.demand.token_id,result.sources)
                            if result.demand.kind=='position' else None)
                        or (market_value(result.demand.condition_id,result.sources) if result.complete else None),
                        complete=result.complete,checked_at=(result.sources.get('book') or result.sources.get('metadata')
                            or SourceResult('')).checked_at,error=None if result.complete else 'market_incomplete')
                    self._async_low_sources['market_'+result.demand.kind+':'+result.demand.condition_id+':'+result.demand.token_id]=source
                    self._async_low_sources['market:'+result.demand.condition_id+':'+result.demand.token_id]=source
                    with self.state._lock:
                        observation=self.state.snapshot().observation
                        if observation is not None:
                            frame=observation.source_frame
                            self.state._observation=replace(observation,source_frame=replace(frame,
                                sources=MappingProxyType({**frame.sources,**self._async_low_sources})))
                except (KeyError,TypeError,ValueError):pass
            else:
                self._async_exact_results[result.demand.order_id]=result
        if financial is not None:
            from .polymarket_lp_market_reader import CapturedSources
            ticket,frame=financial
            financial=(ticket,MarketReader.from_captured_sources(CapturedSources(self.account_id,
                {**frame.sources,**self._async_low_sources})))
        return financial,markets

    def _ensure_read_persistence(self):
        snapshot=self.state.snapshot()
        observation=snapshot.observation
        if observation is None or not observation.orders_complete:return
        for order in snapshot.active_orders:
            if order.side!='BUY' or order.key in self._read_store_owners:continue
            raw=next((r for r in observation.source_frame.orders if r.get('order_id')==order.key[3]
                and r.get('condition_id')==order.key[1] and r.get('token_id')==order.key[2]),None)
            if raw is None:continue
            try:
                result=self.store.lp_register_exchange_orders(self.account_id,order.key[2],[raw],session=dict(
                    account_id=self.account_id,condition_id=order.key[1],token_id=order.key[2],state='entry_open'))
                owner=result['session']
                if owner.get('account_id','').casefold()!=self.account_id.casefold():raise ValueError('read_owner_mismatch')
                record=owner.get('lp_read_protection',{}).get(order.key[3])
                if record is not None:
                    if record.get('condition_id')!=order.key[1] or record.get('token_id')!=order.key[2]:
                        raise ValueError('read_owner_mismatch')
                    self.state.restore_read_protection(order.key,record['consecutive_failures'],record['reason_codes'])
                    self._read_store_values[order.key]=record
                    if record.get('cancel_request_id'):self._read_store_cancels[order.key]=record['cancel_request_id']
                self._read_store_owners[order.key]=owner['session_id']
            except Exception as exc:
                self._read_store_error=type(exc).__name__
                raise RuntimeError('read_protection_store_unavailable') from None

    def _persist_read_protection(self, order_key=None, cancel_request_id=None):
        with self._read_store_lock:
            return self._persist_read_protection_locked(order_key,cancel_request_id)

    def _persist_read_protection_locked(self, order_key, cancel_request_id):
        snapshot=self.state.snapshot()
        for key,failure in snapshot.read_failures.items():
            if order_key is not None and key!=order_key:continue
            owner=self._read_store_owners.get(key)
            if owner is None:
                self._read_store_error='read_protection_owner_pending'
                continue
            record=dict(condition_id=key[1],token_id=key[2],consecutive_failures=failure.consecutive_failures,
                reason_codes=list(failure.reason_codes),cancel_request_id=cancel_request_id or self._read_store_cancels.get(key))
            if record==self._read_store_values.get(key):continue
            def patch(current,connection):
                if (current.get('account_id','').casefold()!=self.account_id.casefold()
                        or key[3] not in current.get('order_history',{})):raise ValueError('read_owner_mismatch')
                records=dict(current.get('lp_read_protection',{}))
                old=records.get(key[3],{})
                if old.get('cancel_request_id'):record['cancel_request_id']=old['cancel_request_id']
                records[key[3]]=record
                return dict(lp_read_protection=records)
            try:self.store.lp_update_session(owner,patch_builder=patch)
            except Exception as exc:
                self._read_store_error=type(exc).__name__
                raise RuntimeError('read_protection_store_unavailable') from None
            self._read_store_values[key]=record
            if record['cancel_request_id']:self._read_store_cancels[key]=record['cancel_request_id']
        self._read_store_error=None if all(k in self._read_store_owners for k in snapshot.read_failures) else 'read_protection_owner_pending'

    def _clear_unsent_read_cancel(self,key,request_id):
        # A known local refusal can retry. A failed/uncertain SQL clear retains
        # the marker and forbids replay until evidence resolves it.
        with self._read_store_lock:
            owner=self._read_store_owners.get(key)
            if owner is None:return
            def patch(current,connection):
                if current.get('account_id','').casefold()!=self.account_id.casefold():raise ValueError('read_owner_mismatch')
                records=dict(current.get('lp_read_protection',{}));value=dict(records.get(key[3],{}))
                if value.get('cancel_request_id')==request_id:value['cancel_request_id']=None
                records[key[3]]=value
                return dict(lp_read_protection=records)
            try:current=self.store.lp_update_session(owner,patch_builder=patch)
            except Exception as exc:self._read_store_error=type(exc).__name__;return
            record=current['lp_read_protection'][key[3]]
            self._read_store_values[key]=record
            if not record.get('cancel_request_id'):self._read_store_cancels.pop(key,None)

    def _apply_async_market_results(self,results):
        self._ensure_read_persistence()
        for result in results:
            self.state.apply_market_attempt(result)
            if result.cleanup_fault:self.state.apply_read_cleanup_fault(result)
        self._persist_read_protection()
        self._reconcile_async_reads()

    def _dispatch_read_failure_cancels(self):
        attempts=[];snapshot=self.state.snapshot()
        for key,failure in snapshot.read_failures.items():
            if key in self._read_store_cancels or key not in self._read_store_owners:continue
            progress=snapshot.progress.get(failure.protection_key)
            if failure.protection_key is None or progress is None or progress.cancel_targets:continue
            # Existing queue/UNKNOWN obligations keep their exact sender ownership.
            if any(t.order_key==key for p in snapshot.progress.values() for t in p.cancel_targets):continue
            order=next((o for o in snapshot.active_orders if o.key==key and o.side=='BUY'),None)
            if order is None:continue
            try:
                handle=self.cancel_independent(key,protection_key=failure.protection_key,
                    reason=failure.reason_codes[0])
            except (ValueError,RuntimeError):continue
            if handle is not None:attempts.append(handle)
        return tuple(attempts)

    def run_once(self, captured_candidates=None, *, timeout=2, attempted_at=None, capture_failed=False):
        if self._closed:
            raise RuntimeError('d2_closed')
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError('d2_run_in_progress')
        stage = 'read'
        try:
            with self._settings_lock:
                self._collect_settings(publish=True)
                policy = self.policy
            self.poll()
            current = self.state.snapshot().current
            markets=[]
            if self.read_runtime is not None:
                financial,markets=self._collect_async_reads()
                if financial is None:
                    self._apply_async_market_results(markets)
                    observation=self.state.snapshot().observation
                    if not markets or observation is None:return RunResult(Acceptance('pending'))
                    ticket,frame=observation.read_ticket,observation.source_frame
                else:ticket,frame=financial
            else:
                ticket = self.state.begin_observation(current.cycle_generation, ('balance', 'open_orders', 'positions'))
                frame = self.reader.read_current(ReadScope(self.account_id, related_markets=True), time.monotonic()+timeout)
            as_of = self._utc_now()
            stage = 'preparation'
            prepared = prepare_current_inputs(frame, captured_candidates, policy, as_of)
            stage = 'calculation'
            calculation = calculate_current(prepared, policy, as_of)
            stage = 'acceptance'
            accepted = (Acceptance('partial',observation) if self.read_runtime is not None and financial is None
                else self.state.accept(ticket, frame, calculation, evaluated_at=as_of))
            # Market attempts have their own exact-order identity/watermark.
            # A rejected financial ticket must not discard already drained
            # failures or cleanup faults for an otherwise current BUY.
            if self.read_runtime is not None:
                self._apply_async_market_results(markets)
            if self.read_runtime is not None and accepted.observation is not None:
                accepted=replace(accepted,observation=self.state.snapshot().observation)
                frame=accepted.observation.source_frame
                prepared=prepare_current_inputs(frame,captured_candidates,policy,as_of)
                calculation=calculate_current(prepared,policy,as_of)
            if accepted.observation is None:
                self._record_iteration(ticket, prepared, calculation, accepted, None, (), None,
                    captured_candidates, as_of, attempted_at, capture_failed=capture_failed)
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
            snapshot = self.state.snapshot()
            accepted_orders = {order.key: order for order in observation.orders}
            orders_accepted = orders_current and observation.orders_complete
            for row in frame.orders:
                order = accepted_orders.get((self.account_id, row['condition_id'], row['token_id'], row['order_id']))
                if (order is None or (order.side, order.venue_status, order.price.value,
                        order.original_size.value, order.size_matched.value, order.order_type.value,
                        order.expiration.value) != (row['side'], row['status'], row['price'],
                        Decimal(str(row['original_size'])), Decimal(str(row['size_matched'])),
                        row.get('order_type'), row.get('expiration'))):
                    orders_accepted = False
                    break
            selection = select_current_composition(prepared, policy, calculation,
                orders_accepted=orders_accepted, positions_accepted=snapshot.inventory_cost.state == 'known',
                financial_authority=snapshot.new_capacity.state == 'known', as_of=as_of,
                shadow_blocked_conditions=tuple(o.scope[1] for o in snapshot.openings.values()
                    if o.phase != 'closed' and o.stop_requested) if policy.allocation_shadow_enabled else ())
            fixed = self._fixed_plan(observation, prepared, selection)
            # LPControlOwner uses this same peer coordination lock while its
            # activation callback publishes round then state. No native I/O is
            # performed under it; seal/finish cannot race control application.
            with self.state._lock:
                stage = 'round_start'
                start = self.rounds.begin_round(self._control_view(), orders, fixed_buys=fixed)
            # Observational failures must never replace a control/trading result.
            self._record_iteration(ticket, prepared, selection, accepted, start, (), fixed,
                captured_candidates, as_of, attempted_at, capture_failed=capture_failed)
            if start.status != StartStatus.BATCH:
                return RunResult(accepted, start, selection=selection)
            self._sealed_candidates = {(start.token.batch_id, a.request_id): row
                for a in start.token.actions if isinstance(a.target, BuyTarget)
                for row in prepared.sources['candidates'].value
                if not row.get('existing') and (row['condition_id'], row['token_id']) in selection.new_target_keys
                and row['token_id'] == a.target.token_id and row.get('market_id') == a.target.market_id}
            self._sealed_candidate_source = prepared.sources['candidates']
            per_direction = prepared.sources.get('candidate_sources')
            self._sealed_candidate_sources = per_direction.value if per_direction else {}
            stage = 'local_attempts'
            outcomes = tuple(self._attempt(start.token, action, observation) for action in start.token.actions)
            finish = self.complete_local_attempts(start.token, outcomes)
            self._record_iteration(ticket, prepared, selection, accepted, start, outcomes, fixed,
                captured_candidates, as_of, attempted_at, capture_failed=capture_failed)
            self._sealed_candidates.clear()
            self._sealed_candidate_source = None
            self._sealed_candidate_sources = {}
            rotation = None
            if start.token.kind == BatchKind.NORMAL_BUY:
                victims = tuple(o for o in observation.order_view.open_orders
                    if o.side == 'BUY' and o.key[3] in selection.victim_order_ids)
                rotation = RotationDecision(self.account_id, observation.cycle_generation,
                    observation.observation_id, victims,
                    tuple(RotationYield('incumbent', o.key,
                        'known' if selection.yields.get(o.key[3]) is not None else 'unknown',
                        selection.yields.get(o.key[3])) for o in observation.order_view.open_orders if o.side == 'BUY'),
                    tuple(RotationYield('candidate', (token,), 'known' if rate is not None else 'unknown', rate)
                        for token, rate in selection.candidate_yields.items()), selection.target_tokens)
            return RunResult(accepted, start, finish, outcomes, rotation, selection=selection)
        except BaseException:
            self.record_attempt_failure(attempted_at=attempted_at, stage=stage)
            raise
        finally:
            self._run_lock.release()

    def _record_iteration(self, ticket, prepared, calculation, accepted, start, outcomes, fixed,
                          capture, evaluated_at, attempted_at, *, capture_failed=False):
        try:
            round_number = self.rounds.snapshot().round_number
            diagnostic_snapshot = self.state.snapshot()
            summary = summarize_iteration(attempted_at=attempted_at or ticket.started_at, ticket=ticket,
                prepared_frame=prepared, calculation=calculation, acceptance=accepted,
                round_start=start, outcomes=outcomes, fixed_plan=fixed, capture=capture,
                evaluated_at=evaluated_at, round_number=round_number,
                accepted_capacity=diagnostic_snapshot.new_capacity)
            if capture_failed:
                summary = dict(summary, latest_attempt=dict(summary['latest_attempt'],
                    capture_failed=True, capture_failure_reason='capture_unavailable'))
            if calculation.allocation_shadow is not None:
                try:
                    shadow = yield_diagnostic_value(calculation.allocation_shadow)
                    logger.info('prediction_lp_allocation_shadow %s', shadow)
                except Exception as exc:
                    from .polymarket_lp_allocation import shadow_result
                    shadow = shadow_result('error', error=type(exc).__name__, reason_codes=('shadow_exception',))
                summary = dict(summary, allocation_shadow=shadow)
            try:
                summary = dict(summary, yield_observation=summarize_yield_observation(
                    prepared, calculation, ticket, accepted, evaluated_at,
                    round_start=start, outcomes=outcomes, fixed_plan=fixed,
                    receipt_orders=diagnostic_snapshot.orders))
            except Exception:
                summary = dict(summary, yield_observation=dict(available=False))
            with self.state._lock:
                try:
                    observed = summary.get('yield_observation', {})
                    if observed.get('available'):
                        observed = dict(observed)
                        unknown_observations = freeze(observed.pop('unknown_incumbents')['samples'])
                        summary['yield_observation'] = observed
                        try:
                            history = yield_diagnostic_value(self._yield_rounds)
                        except Exception:
                            history = []
                            self._yield_diagnostic_instance = uuid4().hex
                        identity = observed['identity']['observation_id']
                        duplicate = any(r['identity']['observation_id'] == identity for r in history)
                        rounds = freeze([r for r in history
                            if r['identity']['observation_id'] != identity] + [observed])[-YIELD_ROUND_LIMIT:]
                        self._yield_unknown_observations = unknown_observations
                        self._yield_rounds_total += not duplicate
                        self._yield_rounds = rounds
                    summary['yield_rounds'] = dict(instance_id=self._yield_diagnostic_instance,
                        total_observations=self._yield_rounds_total, round_limit=YIELD_ROUND_LIMIT,
                        row_limit=YIELD_ROW_LIMIT, recent=self._yield_rounds)
                except Exception:
                    # Intentionally reset an uncertain ring and rotate its ID so
                    # consumers see a discontinuity, not partial/corrupt history.
                    # This resets diagnostics only, never trading state.
                    self._yield_rounds, self._yield_unknown_observations = (), ()
                    self._yield_rounds_total = 0
                    self._yield_diagnostic_instance = uuid4().hex
                    summary['yield_observation'] = dict(available=False)
                    summary['yield_rounds'] = dict(available=False)
                self._diagnostics = freeze({**{k: v for k, v in summary.items() if k != 'local_outcomes'}, 'accepted_observation':
                    summary['accepted_observation'] or self._diagnostics.get('accepted_observation'),
                    'round_start': summary['round_start'] if start else self._diagnostics.get('round_start')})
        except Exception:
            pass

    def record_attempt_failure(self, *, attempted_at, stage):
        try:
            with self.state._lock:
                self._diagnostics = freeze({**self._diagnostics,
                    'latest_attempt': dict(attempted_at=attempted_at, stage=stage,
                        disposition='failed', reason='other', capture=None)})
        except Exception:
            pass
        self._record_protection_status(entered=False, disposition='skipped', skip_reason=stage+'_failed')

    def begin_scheduled_attempt(self):
        try:
            attempted_at = self._utc_now()
        except Exception:
            attempted_at = None
        self._record_protection_status(entered=False, disposition='pending',
            new_attempt=True, attempted_at=attempted_at)
        return attempted_at

    def _record_protection_status(self, *, entered, disposition, skip_reason=None,
                                  new_attempt=False, attempted_at=None):
        try:
            with self.state._lock:
                snapshot = self.state.snapshot()
                values = dict(self._protection_diagnostics)
                if new_attempt:
                    values.update(attempt_id=uuid4().hex, attempted_at=attempted_at,
                        observation_id=None, generation=snapshot.current.cycle_generation)
                if entered and snapshot.observation is not None:
                    values.update(observation_id=snapshot.observation.observation_id,
                        generation=snapshot.observation.cycle_generation)
                self._protection_diagnostics = freeze(dict(values, entered=entered,
                    disposition=disposition, skip_reason=skip_reason))
        except Exception:
            pass

    def diagnostics(self):
        try:
            with self.state._lock:
                snapshot = self.state.snapshot()
                result = project_diagnostics(self._diagnostics, as_of=self._utc_now())
                result['protection'] = {**self._protection_diagnostics,
                    'cancel_outcomes': self._diagnostic_outcomes['protection_cancel_outcomes']}
                if self.read_runtime is not None:
                    runtime=self.read_runtime.status()
                    result['async_reads']=yield_diagnostic_value({k:v for k,v in runtime.items() if k!='scopes'})
                    result['async_reads']['failures']=dict(total=len(snapshot.read_failures),
                        recent=[dict(order_id=key[3][:96],consecutive_failures=f.consecutive_failures,reason_codes=f.reason_codes)
                            for key,f in list(snapshot.read_failures.items())[:20]])
                    result['async_reads']['persistence_error']=self._read_store_error
                result['local_outcomes'] = self._diagnostic_outcomes
                result['cancel_blocks'] = diagnostic_json_copy(self._cancel_blocks)
                unknown = [y for key, y in snapshot.yields.items() if y.unknown_since_monotonic is not None
                    and any(o.key == key and o.side == 'BUY' for o in snapshot.active_orders)]
                now = self._clock()
                result['unknown_buy_yields'] = dict(total=len(unknown), omitted=max(0, len(unknown)-20),
                    recent=[dict(order_id=y.order_key[3], unknown_since_at=y.unknown_since_at,
                        age_seconds=max(0, now-y.unknown_since_monotonic),
                        due_at_600=now >= y.unknown_deadline_monotonic) for y in unknown[:20]])
                try:
                    details = {r.get('order_id'): r for r in self._yield_unknown_observations}
                except Exception:
                    details = {}
                for row, state in zip(result['unknown_buy_yields']['recent'], unknown[:20]):
                    try:
                        detail = details.get(row['order_id'], {})
                        current = bool(detail.get('accepted_input') and any(
                            s.source_id == detail.get('order_source_id') for s in state.input_sources))
                        enrichment = dict(yield_diagnostic_value(detail), reason_is_current=current,
                            reason_codes=detail.get('reason_codes', ()) if current else ('reason_unavailable',),
                            attempted_reason_codes=detail.get('reason_codes', ()))
                        if not current:
                            latest = result.get('yield_observation', {})
                            relevant = ('open_orders', 'market:'+state.order_key[1]+':'+state.order_key[2])
                            failures = [error for error in latest.get('source_errors', {}).get('samples', ())
                                if error.get('source') in relevant or
                                (error.get('condition_id'),error.get('token_id')) == state.order_key[1:3]]
                            enrichment['attempted_source_failures'] = [{k: error.get(k) for k in
                                ('source', 'source_error', 'error_class', 'retry_cache_hit',
                                 'remaining_retry_seconds', 'inflight_age_seconds', 'http_status')}
                                for error in failures[:2]]
                            if failures and not detail:
                                enrichment['attempted_reason_codes'] = ('order_inputs_not_current',)
                                enrichment['reason_observation_id'] = latest.get('identity', {}).get('observation_id')
                        row.update(enrichment)
                    except Exception:
                        row.update(reason_is_current=False, reason_codes=('reason_unavailable',), attempted_reason_codes=())
                return result
        except Exception:
            logger.exception('prediction_lp_diagnostics_failed')
            return dict(version=1, available=False)

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

    def _dispatch_current(self, generation, *, running=False):
        """Final memory-only linearization, after any waiting admission I/O."""
        with self.state._lock:
            current = self._control_view()
            return (not current.fatal and current.cycle_generation == generation
                and (not running or current.effective_running))

    def _fixed_plan(self, observation, prepared, calculation):
        source = prepared.sources['candidates']
        snapshot = self.state.snapshot()
        independent_excess = (len(prepared.current_buys) > self.policy.target_count
            or self.policy.budget is not None and snapshot.inventory_cost.state == 'known'
            and snapshot.buy_reserved.state == 'known'
            and snapshot.buy_reserved.value > self.policy.budget)
        if (self.read_runtime is not None and (not self.read_runtime.status()['new_buy_allowed'] or self._read_store_error)
                or self._settings_required and not self._settings_loaded
                or not source.complete or source.error or source.value is None
                or snapshot.new_capacity.state != 'known'):
            if (not (self._settings_required and not self._settings_loaded)
                    and independent_excess and calculation.selection_state == 'DECISION'
                    and calculation.victim_order_ids and observation.orders_complete
                    and 'orders_not_accepted' not in calculation.selection_reasons):
                return FixedBuyPlan(self.account_id, observation.cycle_generation,
                    observation.observation_id, observation.observation_id + ':fixed', ())
            return None
        if not isinstance(source.value, tuple) or any(not isinstance(row, Mapping) for row in source.value):
            return None
        targets = []
        snapshot = self.state.snapshot()
        slots = max(0, self.policy.target_count - sum(o.side == 'BUY' for o in snapshot.active_orders))
        money = snapshot.new_capacity.value
        for condition, token in calculation.new_target_keys:
            rows = [r for r in source.value if not r.get('existing')
                and r.get('condition_id') == condition and r.get('token_id') == token]
            if len(rows) != 1:
                return None  # ambiguous identity is not an empty fixed plan
            row = rows[0]
            if ('policy_version' in row and row['policy_version'] != calculation.policy_version):
                return None
            if any(o.phase != 'closed' and o.stop_requested and o.scope[1:3] == (row.get('condition_id'), row.get('token_id'))
                    for o in self.state.snapshot().openings.values()):
                continue
            try:
                if (any(type(row.get(key)) is not str or not row[key]
                        for key in ('condition_id', 'token_id', 'market_id'))
                        or type(row.get('venue', self.venue)) is not str
                        or not external_account_matches(row.get('account_id', self.account_id), self.account_id)):
                    return None
                terms = BuyTerms(Decimal(str(row['price'])), Decimal(str(row['quantity'])),
                    row['order_type'], row.get('expiration'), row['post_only'], tuple(row.get('send_options', ())))
                cost = terms.price * terms.quantity
                if not slots or cost > money:
                    continue
                targets.append(BuyTarget(row.get('venue', self.venue), row['token_id'], row['market_id'], terms))
                slots -= 1
                money -= cost
            except (KeyError, TypeError, ValueError):
                return None
        return FixedBuyPlan(self.account_id, observation.cycle_generation,
            observation.observation_id, observation.observation_id + ':fixed', tuple(targets))

    def complete_local_attempts(self, token, outcomes):
        with self.state._lock:
            return self.rounds.finish_local_attempts(self._control_view(), token, outcomes)

    def _rotation_current(self, decision, order):
        snapshot = self.state.snapshot()
        control, observation = self._control_view(), snapshot.observation
        current = next((o for o in snapshot.active_orders if o.key == order.key), None)
        return bool(not control.fatal and control.effective_running
            and control.account_id == decision.account_id
            and control.cycle_generation == decision.cycle_generation
            and observation is not None and observation.observation_id == decision.observation_id
            and observation.orders_complete and current is not None
            and current.terms_version == order.terms_version
            and not self._protection_cancel_owned(order.key))

    def dispatch_rotation(self, result):
        decision = result.rotation
        with self.state._lock:
            observation = self.state.snapshot().observation
            if decision is None:
                self._remember_rotation(result)
                return result
            if (observation is None
                    or observation.observation_id != decision.observation_id
                    or self._rotation_dispatched == decision.observation_id):
                return result
            self._rotation_dispatched = decision.observation_id
        outcomes = []
        for order in decision.victims:
            if not self._rotation_current(decision, order):
                self._record_cancel_block('rotation_not_current', phase='before_request', order_key=order.key)
                outcomes.append(RotationOutcome(order.key, 'not_sent', 'rotation_not_current'))
                continue
            with self.state._lock:
                if self._rotation_cancel_backoff(decision, order):
                    outcomes.append(RotationOutcome(order.key, 'suppressed', 'cancel_backoff'))
                    continue
            try:
                handle = self.cancel_independent(order.key, rotation_origin=(decision, order))
                outcomes.append(RotationOutcome(order.key, 'pending' if handle else 'not_sent',
                    None if handle else 'rotation_not_current', handle))
            except Exception as exc:
                reason = str(exc)
                known = {'mutation_not_admitted', 'independent_cancel_not_current',
                    'independent_cancel_identity', 'native_capacity'}
                outcomes.append(RotationOutcome(order.key, 'not_sent' if reason in known else 'error',
                    reason if reason in known else 'unexpected_dispatch_error'))
        result = replace(result, rotation_outcomes=tuple(outcomes))
        self._remember_rotation(result)
        return result

    def _remember_rotation(self, result):
        """Keep one bounded value projection; native jobs retain their own lifetime."""
        try:
            decision, selection = result.rotation, result.selection
            def bounded(rows):
                rows = tuple(rows)
                return dict(total=len(rows), samples=list(rows[:20]), omitted=max(0, len(rows)-20))
            def yield_row(row):
                value = dict(kind=row.kind, identity=list(row.identity), state=row.state,
                    yield_pct_per_hour=None if row.yield_pct_per_hour is None else str(row.yield_pct_per_hour))
                if row.kind == 'incumbent' and selection:
                    try:
                        value['observation'] = yield_diagnostic_value({k: v for k, v in
                            selection.incumbent_observations.get(row.identity[3], {}).items()
                            if k not in ('account_id', 'condition_id', 'token_id', 'order_id', 'state', 'yield_pct_per_hour')})
                    except Exception:
                        value['observation'] = None
                return value
            known_reasons = {'balance_unknown', 'budget_unknown', 'orders_unknown', 'positions_unknown',
                'financial_authority_unknown', 'candidates_incomplete', 'orders_not_accepted',
                'rotation_not_current', 'mutation_not_admitted', 'independent_cancel_not_current',
                'independent_cancel_identity', 'native_capacity', 'unexpected_dispatch_error', 'cancel_backoff'}
            def reasons(values):
                counts = {}
                for reason in values:
                    key = reason if reason in known_reasons else 'other'
                    counts[key] = counts.get(key, 0)+1
                return counts
            counts, samples, pending = {}, [], []
            for outcome in result.rotation_outcomes:
                counts[outcome.status] = counts.get(outcome.status, 0)+1
                request_id = outcome.handle.identity.request_id if outcome.handle else None
                samples.append(dict(order_key=list(outcome.order_key), status=outcome.status,
                    reason=outcome.reason, request_id=request_id))
                if outcome.handle:
                    pending.append((request_id, outcome.handle))
            outcomes = bounded(samples)
            outcomes.update(counts=counts, reason_counts=reasons(o.reason for o in result.rotation_outcomes if o.reason))
            summary = dict(selection_state=selection.selection_state if selection else 'NO_DECISION',
                reason_counts=reasons(selection.selection_reasons if selection else ()),
                decision_states=dict(selection.decision_states) if selection else {},
                selected=bounded(dict(kind='incumbent', order_id=order_id)
                    for order_id in selection.retained_order_ids) if selection else bounded(()),
                new_targets=bounded(dict(condition_id=condition, token_id=token)
                    for condition, token in selection.new_target_keys) if selection else bounded(()),
                identity=None if decision is None else dict(account_id=decision.account_id,
                    cycle_generation=decision.cycle_generation, observation_id=decision.observation_id),
                incumbent_yields=bounded(yield_row(row) for row in decision.incumbent_yields) if decision else bounded(()),
                candidate_yields=bounded(yield_row(row) for row in decision.candidate_yields) if decision else bounded(()),
                victims=bounded(dict(order_key=list(order.key), terms_version=order.terms_version,
                    price=str(order.price.value), remaining=str(order.remaining.value),
                    original_size=str(order.original_size.value), order_type=order.order_type.value,
                    expiration=order.expiration.value)
                    for order in decision.victims) if decision else bounded(()), outcomes=outcomes)
            with self.state._lock:
                try:
                    observation = self._diagnostics.get('yield_observation', {})
                    summary['source_errors'] = observation.get('source_errors')
                    summary['read_ticket'] = observation.get('read_ticket')
                except Exception:
                    summary['source_errors'], summary['read_ticket'] = None, None
                self._rotation_summary, self._rotation_pending = summary, tuple(pending)
        except Exception:
            # Diagnostics cannot replace or suppress the trading result.
            logger.exception('prediction_lp_rotation_diagnostics_failed')
            with self.state._lock:
                self._rotation_summary = dict(selection_state='UNKNOWN', reason_counts={'other':1},
                    available=False, error='diagnostics_unavailable')
                self._rotation_pending = ()

    def rotation_diagnostics(self):
        """Memory-only GET. Read completed Futures without waiting or replaying."""
        try:
            return self._rotation_diagnostics()
        except Exception:
            logger.exception('prediction_lp_rotation_diagnostics_failed')
            return dict(selection_state='UNKNOWN', available=False, error='diagnostics_unavailable')

    def _rotation_diagnostics(self):
        with self.state._lock:
            summary = diagnostic_json_copy(self._rotation_summary)
            pending = self._rotation_pending
        if summary is None or not pending:
            return summary
        outcomes = summary['outcomes']
        counts = outcomes['counts']
        for request_id, handle in pending:
            if not handle._future.done():
                continue
            try:
                completed = handle.result(timeout=0)
                status = completed.status.value
                reason = completed.reason_code or 'control_or_native_not_current'
            except Exception:
                status = 'error'
            counts['pending'] -= 1
            counts[status] = counts.get(status, 0)+1
            for row in outcomes['samples']:
                if row['request_id'] == request_id:
                    row['status'] = status
                    if status == 'not_sent':
                        row['reason'] = reason
            if status == 'not_sent':
                reasons = outcomes['reason_counts']
                reasons[reason] = reasons.get(reason, 0)+1
        if counts.get('pending') == 0:
            counts.pop('pending')
        return summary

    def _attempt(self, token, action, observation, *, cancel_order=None, mutation_lease=None, protection_target=None,
                 protection_origin=None, rotation_origin=None):
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
        reliable_cancel=(isinstance(token,_IndependentContext) and token.protection_key is not None
            and token.protection_key.kind=='read_failure')
        if reliable_cancel:
            # The disposable writer is not the durability boundary for a new
            # protection. Audit first, then commit the no-replay marker; only
            # after both writes do the existing fresh sender guards execute.
            try:
                if not self.store.lp_log_business_record(record):raise RuntimeError('read_cancel_audit_conflict')
                self._persist_read_protection(order.key,action.request_id)
            except Exception as exc:
                self._read_store_error=type(exc).__name__
                return LocalOutcome(action,LocalStatus.NOT_SENT,True,reason_code='control_or_native_not_current')
        else:self.writer.offer(record)
        with self.state._lock:
            control = self._control_view()
            protection = isinstance(token, _IndependentContext) and token.protection_key is not None
            permitted = ((control.effective_running or protection) and not control.fatal
                and control.cycle_generation == token.cycle_generation and action.account_id == self.account_id
                and target.venue == self.venue)
            block_reason = None if permitted else ('independent_cancel_identity'
                if action.account_id != self.account_id or target.venue != self.venue else 'control_or_native_not_current')
            owned = {h.identity.request_id for h in self._native_jobs.values() if h._thread.is_alive()}
            occupied = (owned | self._active_sends) - {action.request_id}
            if isinstance(target, CancelTarget):
                existing = self._native_jobs.get((self.account_id, target.venue, target.token_id, target.existing_order_id))
                if permitted and existing and existing._thread.is_alive() and existing.identity.request_id != action.request_id:
                    permitted, block_reason = False, 'independent_cancel_inflight'
                if not isinstance(token, _IndependentContext):
                    # Resume cleanup cannot steal a deferred protection target
                    # or replay an already attempted (including UNKNOWN) one.
                    if permitted and self._protection_cancel_owned(order.key):
                        permitted, block_reason = False, 'protection_cancel_owned'
            if permitted and len(occupied) >= 2:
                permitted, block_reason = False, 'native_capacity'
            if permitted:
                self._active_sends.add(action.request_id)
        reason = self._preparation_error(target) if isinstance(target, BuyTarget) else block_reason
        if not permitted or reason:
            reason = reason or 'control_or_native_not_current'
            self._last_preparation_rejection = (action.request_id, reason)
            with self.state._lock:
                self._active_sends.discard(action.request_id)
            outcome = LocalOutcome(action, LocalStatus.NOT_SENT, True,
                reason_code=reason if isinstance(target, CancelTarget) else None)
        else:
            try:
                outcome = self._cancel(action, observation, order, protection=protection, lease=mutation_lease,
                    protection_key=token.protection_key if protection else None, protection_target=protection_target,
                    protection_origin=protection_origin, rotation_origin=rotation_origin) if isinstance(target, CancelTarget) else self._buy(token, action, record)
            finally:
                with self.state._lock:
                    self._active_sends.discard(action.request_id)
        reason = outcome.reason_code or reason
        if reliable_cancel and outcome.status==LocalStatus.NOT_SENT:
            self._clear_unsent_read_cancel(order.key,action.request_id)
        if isinstance(target, CancelTarget) and outcome.status == LocalStatus.NOT_SENT:
            self._record_cancel_block(reason, phase='completed', order_key=order.key,
                request_id=action.request_id, generation=token.cycle_generation)
            if (rotation_origin is not None and isinstance(token, _IndependentContext)
                    and token.protection_key is None and protection_origin is None):
                decision, original_order = rotation_origin
                with self.state._lock:
                    self._prune_rotation_cancel_backoffs()
                    key = (*self._rotation_cancel_key(decision, original_order), reason)
                    self._rotation_cancel_backoffs[key] = dict(retry_at=self._clock()+10.0, suppressed_count=0)
                    while len(self._rotation_cancel_backoffs) > 256:
                        self._rotation_cancel_backoffs.pop(next(iter(self._rotation_cancel_backoffs)))
        self.writer.offer(ReceiptRecord(self.account_id, self.process_epoch, action.request_id,
            record.session_id, record.condition_id, record.token_id, outcome.venue_order_id or record.order_id,
            'accepted' if outcome.status == LocalStatus.ACCEPTED else
            'not_sent' if outcome.status == LocalStatus.NOT_SENT else
            'rejected' if outcome.status == LocalStatus.REJECTED else 'unknown',
            self._utc_now(), reason))
        if isinstance(token, _IndependentContext) and token.protection_key is not None:
            self._finish_protection_cancel(token.protection_key, outcome)
        try:
            with self.state._lock:
                self._diagnostic_outcomes = append_outcome(self._diagnostic_outcomes, outcome, token=token,
                    reason=protection_target.reason if protection_target else reason,
                    protection=isinstance(token, _IndependentContext) and token.protection_key is not None)
        except Exception:
            pass
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
        lease = self._acquire_mutation('BUY')
        if lease is None:
            return LocalOutcome(action, LocalStatus.NOT_SENT, True)
        try:
            return self._buy_admitted(token, action, record, lease)
        finally:
            lease.release()

    def _buy_admitted(self, token, action, record, lease, *, manual_origin=None, manual_command=None, submit_source=None):
        terms = action.target.terms
        if self.read_runtime is not None and (not self.read_runtime.status()['new_buy_allowed'] or self._read_store_error):
            return LocalOutcome(action, LocalStatus.NOT_SENT, True)
        mutation = 'MANUAL_BUY' if manual_origin is not None else 'BUY'
        def current():
            return self._manual_origin_current(manual_origin) if manual_origin is not None else self._dispatch_current(token.cycle_generation, running=True)
        if not lease.allowed(mutation) or not current():
            return LocalOutcome(action, LocalStatus.NOT_SENT, True)
        try:
            signed = self.trading.lp_create_limit_order(token_id=record.token_id, price=terms.price,
                quantity=terms.quantity, side='BUY', post_only=terms.post_only, expiration=terms.expiration,
                **dict(terms.send_options))
        except Exception:
            return LocalOutcome(action, LocalStatus.NOT_SENT, True)
        if not lease.allowed(mutation) or not current():
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
                if manual_command is None:
                    self._register_opening(key)
                    self._register_submit_baseline(token, action, record, key)
                else:
                    self._register_manual_opening(manual_command,key,record,submit_source)
            elif applied != 'stale_evidence':
                return self._uncertain_buy(token, action, record, native_started_at, 'invalid_receipt', venue_id, status)
        return LocalOutcome(action, LocalStatus.ACCEPTED, True, venue_id, status)

    def _cancel(self, action, observation, order, *, protection=False, lease=None,
                protection_key=None, protection_target=None, protection_origin=None, rotation_origin=None):
        owned = lease is None
        lease = self._acquire_mutation('ROTATION_CANCEL' if rotation_origin is not None else 'CANCEL') if owned else lease
        if lease is None:
            return LocalOutcome(action, LocalStatus.NOT_SENT, True, reason_code='mutation_not_admitted')
        try:
            return self._cancel_admitted(action, observation, order, protection=protection, lease=lease,
                protection_key=protection_key, protection_target=protection_target, protection_origin=protection_origin,
                rotation_origin=rotation_origin)
        finally:
            if owned:
                lease.release()

    def _cancel_admitted(self, action, observation, order, *, protection, lease, protection_key, protection_target,
                         protection_origin, rotation_origin=None):
        target = action.target
        prior=self._read_store_cancels.get(order.key)
        if prior is not None and prior!=action.request_id:
            return LocalOutcome(action, LocalStatus.NOT_SENT, True, reason_code='protection_cancel_owned')
        if not isinstance(target, CancelTarget) or target.venue != self.venue:
            return LocalOutcome(action, LocalStatus.NOT_SENT, True, reason_code='independent_cancel_identity')
        if (order.key[0] != self.account_id or order.key[2:] != (target.token_id, target.existing_order_id)
                or order.side != 'BUY'):
            return LocalOutcome(action, LocalStatus.NOT_SENT, True, reason_code='independent_cancel_identity')
        if not lease.allowed('ROTATION_CANCEL' if rotation_origin is not None else 'CANCEL'):
            return LocalOutcome(action, LocalStatus.NOT_SENT, True, reason_code='mutation_lease_not_current')
        with self.state._lock:
            if not self._dispatch_current(observation.cycle_generation, running=not protection):
                return LocalOutcome(action, LocalStatus.NOT_SENT, True, reason_code='control_or_native_not_current')
            if rotation_origin is not None and not self._rotation_current(*rotation_origin):
                return LocalOutcome(action, LocalStatus.NOT_SENT, True, reason_code='rotation_not_current')
            if protection_origin is not None and not self._protection_cancel_origin_current(
                    self.state.snapshot(), protection_key, protection_origin):
                return LocalOutcome(action, LocalStatus.NOT_SENT, True, reason_code='protection_cancel_not_current')
            if protection_target is not None:
                snapshot = self.state.snapshot()
                guard = self._protection_guard(snapshot, protection_key, (order,))
                if self.state.apply_protection(CancelEvidenceUpdate(guard, protection_target)) != 'applied':
                    return LocalOutcome(action, LocalStatus.NOT_SENT, True, reason_code='protection_evidence_not_current')
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
            reads=self.read_runtime.status() if self.read_runtime is not None else None
            control=self.control.status()
            dependency=reads.get('next_dependency_monotonic') if reads else None
            # A returned control thread can still have an uncollected Future.
            # Its completion is runnable; retry_wait owns a separate deadline.
            now=self._clock()
            control_pending=control.save_state in ('pending','saving','retrying')
            retry=control.retry_at_monotonic if control.save_state=='retry_wait' and not control.fatal else None
            dependencies=[d for d in (dependency,retry) if d is not None]
            wait=None
            if not (owned or self._active_sends
                    or any(h._thread.is_alive() for h in self._manual_jobs.values())
                    or self._settings_future is not None or control.native_save_in_flight or control_pending
                    or self._native_completion_generation != self._native_completion_consumed
                    or any(d<=now for d in dependencies)):
                if dependencies:
                    wait=dict(kind='dependencies',deadline=(self._utc_now()+timedelta(seconds=min(dependencies)-now)).isoformat())
                elif reads and reads.get('waiting_for_event'):
                    wait=dict(kind='events')
            return dict(read_runtime=reads,plan_wait=wait,
                read_persistence_error=self._read_store_error,control=control,
                round=self.rounds.snapshot() if self.state.snapshot().current.cycle_generation else None,
                native_inflight=len(owned | self._active_sends), writer=self.writer.status(), closed=self._closed,
                last_preparation_rejection=self._last_preparation_rejection,
                last_current_evidence=self._last_current_evidence,
                last_buy_uncertainty=self._last_buy_uncertainty)

    def _record_cancel_block(self, reason, *, phase, order_key=None, request_id=None, generation=None, **details):
        """Bounded observations only; never invent a request for an early refusal."""
        if reason not in _CANCEL_BLOCK_REASONS:
            raise ValueError('cancel_block_reason_invalid')
        valid_key = (isinstance(order_key, tuple) and len(order_key) == 4
            and order_key[0] == self.account_id and all(type(v) is str and v for v in order_key))
        with self.state._lock:
            values = self._cancel_blocks
            counts = dict(values['by_reason'])
            counts[reason] = counts.get(reason, 0)+1
            sample = dict(phase=phase, reason=reason, **details)
            if valid_key:
                sample['order_key'] = list(order_key)
            if request_id is not None:
                sample['request_id'] = request_id
            if generation is not None:
                sample['generation'] = generation
            self._cancel_blocks = freeze(dict(total=values['total']+1, by_reason=counts,
                recent=(*values['recent'], sample)[-20:]))
        logger.info('lp_cancel_blocked', extra=dict(process_epoch=self.process_epoch,
            phase=phase, reason_code=reason, request_id=request_id,
            order_key=order_key if valid_key else None, generation=generation, **details))

    def _rotation_cancel_key(self, decision, order):
        return (order.key[0], self.venue, *order.key[1:], decision.cycle_generation, order.terms_version)

    def _prune_rotation_cancel_backoffs(self):
        snapshot, now = self.state.snapshot(), self._clock()
        orders = {order.key: order.terms_version for order in snapshot.active_orders if order.side == 'BUY'}
        self._rotation_cancel_backoffs = {key: value for key, value in self._rotation_cancel_backoffs.items()
            if value['retry_at'] > now and key[5] == snapshot.current.cycle_generation
            and orders.get((key[0], *key[2:5])) == key[6]}

    def _rotation_cancel_backoff(self, decision, order):
        """Throttle unchanged never-sent targets, with no request or timer replay."""
        self._prune_rotation_cancel_backoffs()
        identity = self._rotation_cancel_key(decision, order)
        for key, value in self._rotation_cancel_backoffs.items():
            if key[:-1] == identity:
                value['suppressed_count'] += 1
                self._record_cancel_block('cancel_backoff', phase='before_request', order_key=order.key,
                    generation=decision.cycle_generation, original_reason=key[-1],
                    remaining_seconds=max(0., value['retry_at']-self._clock()),
                    suppressed_count=value['suppressed_count'])
                return True
        return False

    def cancel_independent(self, order_key, *, protection_key=None, reason=None, protection_origin=None,
                           rotation_origin=None):
        """Normal exact-ID BUY cancellation outside the sealed BUY barrier.

        This native handle remains owned across logical control transitions.
        It is not a round token and never goes through round finish.
        """
        with self.state._lock:
            existing = self._native_jobs.get((self.account_id, self.venue, order_key[2], order_key[3])) if (
                isinstance(order_key, tuple) and len(order_key) == 4 and order_key[0] == self.account_id) else None
            if existing and existing._thread.is_alive():
                self._record_cancel_block('cancel_in_progress', phase='before_request', order_key=order_key)
                return existing
        lease = self._acquire_mutation('ROTATION_CANCEL' if rotation_origin is not None else 'CANCEL')
        if lease is None:
            self._record_cancel_block('mutation_not_admitted', phase='before_request', order_key=order_key)
            if protection_key is not None:
                return None
            raise ValueError('mutation_not_admitted')
        transferred = False
        try:
            with self.state._lock:
                if not isinstance(order_key, tuple) or len(order_key) != 4 or order_key[0] != self.account_id:
                    self._record_cancel_block('independent_cancel_identity', phase='before_request')
                    raise ValueError('independent_cancel_identity')
                key = (self.account_id, self.venue, order_key[2], order_key[3])
                existing = self._native_jobs.get(key)
                if existing and existing._thread.is_alive():
                    self._record_cancel_block('cancel_in_progress', phase='before_request', order_key=order_key)
                    return existing
                self._native_jobs = {k: h for k, h in self._native_jobs.items() if h._thread.is_alive()}
                snapshot = self.state.snapshot()
                if rotation_origin is not None and not self._rotation_current(*rotation_origin):
                    self._record_cancel_block('rotation_not_current', phase='before_request', order_key=order_key)
                    return None
                if protection_origin is not None and not self._protection_cancel_origin_current(
                        snapshot, protection_key, protection_origin):
                    self._record_cancel_block('protection_cancel_not_current', phase='before_request', order_key=order_key)
                    return None  # the original never-started ticket remains deferred
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
                    self._record_cancel_block('independent_cancel_not_current', phase='before_request', order_key=order_key)
                    raise ValueError('independent_cancel_not_current')
                if len({h.identity.request_id for h in self._native_jobs.values()} | self._active_sends) >= 2:
                    self._record_cancel_block('native_capacity', phase='before_request', order_key=order_key)
                    if protection:
                        return None  # no fabricated request or attempt on deferral
                    raise RuntimeError('native_capacity')
                if (rotation_origin is not None and protection_key is None and protection_origin is None
                        and self._rotation_cancel_backoff(*rotation_origin)):
                    return None
                identity = ActionIdentity(str(uuid4()), self.account_id,
                    CancelTarget(self.venue, order_key[2], order_key[3]))
                context = _IndependentContext(control.cycle_generation, 'independent:' + identity.request_id,
                    self.rounds.snapshot().round_number, observation.observation_id, protection_key)
                target = (CancelTargetState(current_order.key, identity.request_id, current_order.remaining,
                    self._utc_now(), reason, 'pending', 'running', None,
                    Fact('unknown'), Fact('unknown'), False) if protection_key is not None else None)
                future = Future()
                def run():
                    try:
                        outcome = self._attempt(context, identity, observation, cancel_order=current_order,
                            mutation_lease=lease, protection_target=target, protection_origin=protection_origin,
                            rotation_origin=rotation_origin)
                    except BaseException as exc:
                        future.set_exception(exc)
                    else:
                        future.set_result(outcome)
                    finally:
                        try:
                            lease.release()
                        finally:
                            self._publish_native_completion()
                thread = threading.Thread(target=run, name='lp-d2-independent-cancel', daemon=False)
                handle = NativeAttempt(identity, future, thread)
                self._native_jobs[key] = handle
                thread.start()
                transferred = True
                return handle
        finally:
            if not transferred:
                lease.release()

    def _protection_guard(self, snapshot, scope, orders, *, observation=False):
        return ProtectionGuard(scope, snapshot.current.cycle_generation,
            snapshot.scope_versions.get(scope, 0), tuple((o.key, o.terms_version) for o in orders),
            snapshot.observation.observation_id if observation else None, uuid4().hex)

    def _protection_cancel_owned(self, order_key):
        snapshot = self.state.snapshot()
        return (any(t.order_key == order_key for p in snapshot.progress.values() for t in p.cancel_targets)
            or any(t.order.key == order_key for tickets in self._deferred_protection_cancels.values() for t in tickets))

    def _protection_frame_current(self, snapshot):
        current = self.state.snapshot()
        observation = current.observation
        return bool(observation is not None and snapshot.observation is not None
            and observation.observation_id == snapshot.observation.observation_id
            and current.trade_version == snapshot.trade_version == observation.read_ticket.trade_version_at_start
            and current.current.cycle_generation == snapshot.current.cycle_generation == observation.cycle_generation)

    def _protection_cancel_origin_current(self, snapshot, key, origin):
        observation, baseline = snapshot.observation, snapshot.baselines.get(key)
        progress = snapshot.progress.get(key)
        if (observation is None or baseline is None or progress is None
                or observation.observation_id != origin.observation_id
                or snapshot.trade_version != origin.trade_version or observation.read_ticket.trade_version_at_start != origin.trade_version
                or snapshot.current.cycle_generation != origin.cycle_generation or observation.cycle_generation != origin.cycle_generation
                or not observation.orders_complete
                or origin.ticket not in self._deferred_protection_cancels.get(key, ())
                or tuple(o.key for o in origin.anchors) != tuple((*key.scope[:3], oid) for oid in baseline.anchor_order_ids)
                or any(t.order_key == origin.ticket.order.key for t in progress.cancel_targets)):
            return False
        orders = {o.key: o for o in snapshot.orders}
        if origin.attempt_order.key != origin.ticket.order.key:
            return False
        for original in (*origin.anchors, origin.attempt_order):
            order = orders.get(original.key)
            if (order is None or order.side != 'BUY' or order.terms_version != original.terms_version
                    or any(getattr(order, field).state != 'known' or getattr(original, field).state != 'known'
                        or getattr(order, field).value != getattr(original, field).value
                        for field in ('price', 'original_size', 'order_type', 'expiration'))):
                return False
        target = orders[origin.ticket.order.key]
        # The ticket fixes identity and original terms; matched progress belongs
        # to this fresh attempt and may advance without replacing that ticket.
        if (any(getattr(origin.ticket.order, field).state != 'known'
                    or getattr(target, field).value != getattr(origin.ticket.order, field).value
                    for field in ('price', 'original_size', 'order_type', 'expiration'))
                or target not in snapshot.active_orders or target.remaining.state != 'known' or target.remaining.value <= 0
                or origin.attempt_order.remaining.state != 'known'
                or target.remaining.value != origin.attempt_order.remaining.value):
            return False
        for original in origin.anchors:
            anchor = orders[original.key]
            proof = anchor.terminal_proof
            if (anchor.size_matched.state != 'known' or anchor.size_matched.value != 0
                    or anchor.price.value != key.price or proof is None and anchor.presence != 'present'
                    or proof is not None and (not proof.identity_verified or proof.order_key != anchor.key
                        or proof.matched.state != 'known' or proof.matched.value != 0
                        or proof.original_size.state != 'known' or proof.original_size.value != anchor.original_size.value
                        or proof.source.state != 'known' or not proof.source.complete)):
                return False
        return True

    def _dispatch_protection_cancels(self):
        attempts = []
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
                if not self._protection_frame_current(snapshot):
                    return attempts  # a later normal tick may validate the unchanged original tickets
                progress = current.progress.get(key)
                if (progress is None or ticket.order.key in self._read_store_cancels
                        or any(t.order_key==ticket.order.key for p in current.progress.values() for t in p.cancel_targets)):
                    continue
                order = next((o for o in current.active_orders if o.key == ticket.order.key), None)
                if (order is None or order.side != 'BUY' or order.remaining.state != 'known' or order.remaining.value <= 0
                        or any(getattr(order, name).value != getattr(ticket.order, name).value
                            for name in ('price', 'original_size', 'order_type', 'expiration'))
                        or order.price.value != key.price):
                    continue  # never replace the original target with a new/changed order
                existing = self._native_jobs.get((self.account_id, self.venue, order.key[2], order.key[3]))
                if existing and existing._thread.is_alive():
                    retained.append(ticket)
                    continue  # a real independent request already owns this exact target
                origin = _ProtectionCancelOrigin(observation.observation_id, current.trade_version,
                    current.current.cycle_generation, tuple(next(o for o in current.orders if o.key == anchor.key)
                        for anchor in anchors), ticket, order)
                handle = self.cancel_independent(order.key, protection_key=key, reason=ticket.reason,
                    protection_origin=origin)
                retained.append(ticket)  # removed only after actual target registration, never on guard denial
                if handle is not None:
                    attempts.append(handle)
            if retained:
                self._deferred_protection_cancels[key] = tuple(retained)
            else:
                self._deferred_protection_cancels.pop(key, None)
        return attempts

    def _register_submit_baseline(self, token, action, record, order_key):
        row = self._sealed_candidates[(token.batch_id, action.request_id)]
        source = self._sealed_candidate_sources.get(
            f'{record.condition_id}:{record.token_id}', self._sealed_candidate_source)
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
            manual_scopes = {v[0] for v in self._manual_groups.values()}
            related = tuple(o for o in snapshot.openings.values()
                if o.scope[:3] == order_key[:3] and o.scope not in manual_scopes)
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
        if self.read_runtime is not None:
            from .polymarket_lp_read_runtime import ReadDemand
            from .polymarket_lp_market_reader import SourceResult
            snapshot=self.state.snapshot()
            if kind=='scoring':
                order=next((o for o in snapshot.active_orders if o.key[3]==identity[0]),None)
                if order is None:return SourceResult(component,complete=False,error='identity_unknown')
                condition,token,target=order.key[1:]
            elif kind=='order':condition,token,target=identity
            else:condition,token=identity;target=''
            self._async_exact_used[component]=self._clock()
            self._async_exact_requests[component]=ReadDemand(self.account_id,self.process_epoch,
                snapshot.current.cycle_generation,component,snapshot.trade_version,condition,token,
                kind='exact',sources=(kind,),target_order_id=target)
            self._reconcile_async_reads()
            result=self._async_exact_results.get(component)
            if (result is None or result.demand.generation!=snapshot.current.cycle_generation
                    or result.demand.terms_version!=snapshot.trade_version):
                return SourceResult(component,complete=False,error='in_progress')
            return result.sources.get(kind,SourceResult(component,complete=False,error='timeout'))
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
        read_failure_targets = {target.order_key for key, progress in snapshot.progress.items()
            if key.kind == 'read_failure' for target in progress.cancel_targets}
        # These exact registered IDs have accepted pending cancel obligations,
        # independently of any current-process BUY/opening association. The same
        # Reader/deadline and canonical receipt guards own their final proof.
        for order in snapshot.orders:
            if order.key in deferred_anchors or order.key in read_failure_targets:
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
        risk_due=self.read_runtime is None or self._clock()>=self._risk_next.get(scope,0)
        risk_book,risk_market=book,market
        risk_stamp=stamp
        if self.read_runtime is not None:
            risk_source=observation.source_frame.sources.get(f'market_position:{scope[1]}:{scope[2]}')
            risk_stamp=self._source_stamp(risk_source) if risk_source else SourceStamp('')
            risk_due=risk_due or bool(risk_source and self._risk_seen.get(scope)!=risk_source.source_id)
            risk_data=risk_source.value if risk_source and risk_source.complete and not risk_source.error else {}
            risk_book,risk_market=risk_data.get('book'),risk_data.get('metadata')
            if not isinstance(risk_book,Mapping):risk_book=None
            else:
                try:
                    _freshness(risk_book.get('received_at'),self._utc_now(),'book_freshness')
                    _freshness(risk_book.get('timestamp'),self._utc_now(),'book_timestamp')
                    if (risk_book.get('condition_id'),risk_book.get('token_id'))!=scope[1:3]:risk_book=None
                except ValueError:risk_book=None
            sell_source=observation.source_frame.sources.get(f'market_sell:{scope[1]}:{scope[2]}')
            if sell_source is not None:
                sell_data=sell_source.value if sell_source.complete and not sell_source.error else {}
                book=sell_data.get('book')
                if isinstance(book,Mapping):
                    try:
                        _freshness(book.get('received_at'),self._utc_now(),'book_freshness')
                        _freshness(book.get('timestamp'),self._utc_now(),'book_timestamp')
                    except ValueError:book=None
                else:book=None
        risk_evaluate=risk_due if self.read_runtime is not None else risk_book is not None
        risk_input = dict(book=risk_book, market=risk_market)
        fact = lambda value, sources: Fact('known', value, sources) if value is not None else Fact('unknown', sources=sources)
        own = tuple(o for o in (*buys, *sales) if o is not None)
        amounts_known = complete and all(o.notional_usd.state == 'known' for o in own)
        sources = tuple(o.source for o in own)
        cost = sum((o.notional_usd.value for o in buys if o), Decimal(0)) if amounts_known and bought else None
        revenue = sum((o.notional_usd.value for o in sales if o), Decimal(0)) if amounts_known and bought else None
        exit_value = _executable_bid_value(risk_input, residual.value) if risk_book and residual.state == 'known' else None
        projected = _projected_taker_fee(risk_input, residual.value) if risk_book and residual.state == 'known' else None
        if residual.state == 'known' and residual.value == 0:
            exit_value, projected = Decimal(0), Decimal(0)
        fees = sum((o.fees_usd.value for o in own), Decimal(0))+projected if (
            complete and bought and all(o.fees_usd.state == 'known' for o in own) and projected is not None) else None
        economics = OpeningEconomics(scope, fact(cost, sources), fact(revenue, sources), fact(fees, (*sources, risk_stamp)),
            fact(exit_value, (risk_stamp,)), reconciled)
        if self.read_runtime is not None and not risk_evaluate:
            economics=replace(opening.economics,buy_cost_usd=fact(cost,sources),
                sold_revenue_usd=fact(revenue,sources),position_reconciled=reconciled)
            if previous_sell is not None and previous_sell.residual_quantity.value!=residual.value:
                economics=replace(economics,residual_exit_value_usd=Fact('unknown'),fees_usd=Fact('unknown'))
        loss = economics.opening_loss_usd
        if self.read_runtime is not None and risk_evaluate and loss.state=='known':
            self._risk_next[scope]=self._clock()+60
            if risk_source is not None:self._risk_seen[scope]=risk_source.source_id
        latch = risk_evaluate and not opening.stop_loss_latched.value and loss.state == 'known' and loss.value >= STOP_LOSS
        if opening.stop_loss_latched.value or latch:
            book=risk_book or book
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

    def _sell_dispatch_current(self, scope, attempt, generation, origin):
        with self.state._lock:
            snapshot = self.state.snapshot()
            sell = snapshot.sells.get(scope)
            pending = (sell.passive if attempt.kind == 'passive_exit' else sell.protected) if sell else None
            positions = tuple(p for p in snapshot.positions
                if (p.account_id, p.condition_id, p.token_id) == scope[:3])
            return bool(self._dispatch_current(generation) and self._sell_origin_current(snapshot, scope, origin)
                and pending == attempt and sell.position_reconciled
                and sell.residual_quantity.state == 'known' and sell.residual_quantity.value == origin.residual
                and attempt.submit_residual_quantity.state == 'known' and attempt.submit_residual_quantity.value == origin.residual
                and attempt.submit_sold_quantity.state == 'known' and attempt.submit_sold_quantity.value == origin.sold
                and attempt.submit_quantity.state == 'known' and 0 < attempt.submit_quantity.value <= origin.residual
                and len(positions) == 1 and positions[0].quantity.state == 'known' and positions[0].quantity.value == origin.residual
                and not sell.current_sells
                and not any(o.side == 'SELL' and o.key[:3] == scope[:3] for o in snapshot.active_orders))

    def _start_exit(self, scope, kind, quantity, sold, price, origin):
        lease = self._acquire_mutation('SELL')
        if lease is None:
            return None
        transferred = False
        try:
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
                # Only our pending-attempt/role writes advance this expectation.
                # Observation/trade/terms/quantity and their sources stay original.
                dispatch_origin = replace(origin, guard=replace(origin.guard,
                    expected_scope_version=self.state.snapshot().scope_versions[scope]))
                generation = snapshot.current.cycle_generation
                record = RequestRecord(self.account_id, self.process_epoch, identity.request_id, 'd2:'+identity.request_id,
                    scope[1], scope[2], 'SELL', price, quantity, self._utc_now(), None, self.venue, generation,
                    'protection:'+identity.request_id, self.rounds.snapshot().round_number,
                    snapshot.observation.observation_id, attempt.order_type.value, None, kind == 'passive_exit', ())
                self.writer.offer(record)
                handle = self._start_protection_native(identity,
                    lambda: self._execute_exit(scope, attempt, record, generation, lease, dispatch_origin),
                    lease=lease, diagnostic_record=record)
                transferred = True
                return handle
        finally:
            if not transferred:
                lease.release()

    def _start_protection_native(self, identity, operation, *, lease, diagnostic_record=None):
        owned = {h.identity.request_id for h in self._native_jobs.values() if h._thread.is_alive()} | self._active_sends
        if len(owned) >= 2:
            raise RuntimeError('native_capacity')
        self._native_jobs = {k: h for k, h in self._native_jobs.items() if h._thread.is_alive()}
        future = Future()
        def run():
            try: result = operation()
            except BaseException as exc: future.set_exception(exc)
            else:
                if diagnostic_record is not None:
                    try:
                        with self.state._lock:
                            self._diagnostic_outcomes = append_outcome(self._diagnostic_outcomes, result,
                                record=diagnostic_record, protection=diagnostic_record.kind == 'CANCEL')
                    except Exception:
                        pass
                future.set_result(result)
            finally: lease.release()
        thread = threading.Thread(target=run, name='lp-d2-protection-send', daemon=False)
        handle = NativeAttempt(identity, future, thread)
        self._native_jobs[(self.account_id, self.venue, identity.scope[2], identity.request_id)] = handle
        thread.start()
        return handle

    def _execute_exit(self, scope, attempt, record, generation, lease, origin):
        outcome, venue_id, signed = 'not_sent', None, None
        try:
            if not lease.allowed('SELL') or not self._sell_dispatch_current(scope, attempt, generation, origin):
                with self.state._lock:
                    self._publish_sell_attempt(scope, replace(attempt, state='rejected', native_state='ended'))
                return _ProtectionOutcome(attempt.request_id, outcome, venue_id)
            signed = (self.trading.lp_create_limit_order(token_id=scope[2], side='SELL', price=attempt.price.value,
                quantity=attempt.submit_quantity.value, expiration=None, post_only=True)
                if attempt.kind == 'passive_exit' else None)
            if not lease.allowed('SELL') or not self._sell_dispatch_current(scope, attempt, generation, origin):
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
        lease = self._acquire_mutation('SELL_CANCEL')
        if lease is None:
            return None
        transferred = False
        try:
            with self.state._lock:
                if self._control_view().fatal or len({h.identity.request_id for h in self._native_jobs.values()
                        if h._thread.is_alive()} | self._active_sends) >= 2:
                    return None
                snapshot = self.state.snapshot()
                if any(h._thread.is_alive() and getattr(h.identity, 'scope', None) == scope
                        and getattr(h.identity, 'kind', None) == 'cancel_exit' for h in self._native_jobs.values()):
                    return None
                if not self._sell_origin_current(snapshot, scope, origin):
                    return None
                identity = _ProtectionAction(uuid4().hex, self.account_id, scope, 'cancel_exit')
                updated = replace(attempt, cancel_request_id=identity.request_id)
                record = RequestRecord(self.account_id, self.process_epoch, identity.request_id, 'd2:'+identity.request_id,
                    scope[1], scope[2], 'CANCEL', attempt.price.value, attempt.submit_quantity.value, self._utc_now(),
                    attempt.order_key[3], self.venue, snapshot.current.cycle_generation, 'protection:'+identity.request_id,
                    self.rounds.snapshot().round_number, snapshot.observation.observation_id,
                    attempt.order_type.value, attempt.expiration.value, None, ())
                self.writer.offer(record)
                def cancel():
                    outcome = 'unknown'
                    try:
                        if not lease.allowed('SELL_CANCEL'):
                            outcome = 'not_sent'
                        else:
                            with self.state._lock:
                                current = self.state.snapshot()
                                if (not self._dispatch_current(record.cycle_generation)
                                        or not self._sell_origin_current(current, scope, origin)):
                                    return _ProtectionOutcome(identity.request_id, 'not_sent', attempt.order_key[3])
                                self._publish_sell_attempt(scope, updated, observation=True)
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
                handle = self._start_protection_native(identity, cancel, lease=lease, diagnostic_record=record)
                transferred = True
                return handle
        finally:
            if not transferred:
                lease.release()

    def _finish_protection_cancel(self, key, outcome):
        with self.state._lock:
            snapshot = self.state.snapshot()
            progress = snapshot.progress.get(key)
            previous = next((t for t in progress.cancel_targets if t.request_id == outcome.identity.request_id), None) if progress else None
            if previous is None:
                if outcome.status == LocalStatus.NOT_SENT:
                    return  # no SDK call, no registered target; the original ticket remains retryable
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
            source = observation.source_frame.sources.get(f'market_book:{identity[1]}:{identity[2]}',
                observation.source_frame.sources.get(f'market:{identity[1]}:{identity[2]}'))
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

    def _manual_review_deadlines(self):
        with self.state._lock:
            snapshot = self.state.snapshot()
            if not self._protection_frame_current(snapshot):
                return
            for scope, _ in tuple(self._manual_groups.values()):
                opening = snapshot.openings.get(scope)
                if (opening is None or opening.phase == 'closed' or opening.stop_requested
                        or opening.review_at.state != 'known' or self._utc_now() < opening.review_at.value):
                    continue
                orders = tuple(o for o in snapshot.orders if o.key[:3] == scope[:3]
                    and o.key[3] in opening.entry_order_ids)
                review = replace(opening,phase='review',stop_requested=True,review_status='awaiting_reconciliation',
                    scope_version=snapshot.scope_versions.get(scope,0)+1)
                self.state.apply_protection(OpeningProtectionUpdate(
                    self._protection_guard(snapshot,scope,orders,observation=True),review))
                snapshot = self.state.snapshot()

    def _publish_native_completion(self):
        with self.state._lock:
            self._native_completion_generation += 1
        # The callback may run before this thread physically exits. Retain the
        # notification until a later protection pass observes that true exit.
        try:
            self._read_wakeup()
        except Exception:
            logger.exception('prediction_lp_native_completion_wakeup_failed')

    def run_protection_once(self, *, timeout=2):
        with self.state._lock:
            completed = self._native_completion_consumed
            if not any(h._future.done() and h._thread.is_alive() for h in self._native_jobs.values()):
                completed = self._native_completion_generation
        self._record_protection_status(entered=True, disposition='entered')
        try:
            result = self._run_protection_once(timeout=timeout)
        except BaseException:
            self._record_protection_status(entered=True, disposition='failed')
            raise
        if self._protection_diagnostics['disposition'] == 'entered':
            self._record_protection_status(entered=True, disposition='completed')
        with self.state._lock:
            # A completion after this pass's capacity check remains pending.
            self._native_completion_consumed = max(self._native_completion_consumed, completed)
        return result

    def _run_protection_once(self, *, timeout=2):
        """Evaluate the accepted raw frame; protection never acquires a new book."""
        with self._protection_lock:
            read_attempts=self._dispatch_read_failure_cancels()
            snapshot = self.state.snapshot()
            observation = snapshot.observation
            if (self._closed or snapshot.current.shutdown_requested or snapshot.current.fatal_id
                    or observation is None or not observation.orders_complete
                    or observation.cycle_generation != snapshot.current.cycle_generation
                    or observation.read_ticket.trade_version_at_start != snapshot.trade_version):
                reason = ('closed' if self._closed else 'shutdown' if snapshot.current.shutdown_requested else
                    'fatal' if snapshot.current.fatal_id else 'no_observation' if observation is None else
                    'orders_incomplete' if not observation.orders_complete else
                    'generation_changed' if observation.cycle_generation != snapshot.current.cycle_generation else 'stale_observation')
                self._record_protection_status(entered=True, disposition='skipped', skip_reason=reason)
                return ()
            self._manual_review_deadlines()
            deadline = time.monotonic()+timeout
            if self._refresh_protection_evidence(deadline):
                self._record_protection_status(entered=True, disposition='skipped', skip_reason='observation_invalidated')
                return ()  # receipts invalidate the old observation; ordinary reader supplies the next one
            self._register_first_seen(snapshot)
            snapshot = self.state.snapshot()
            attempts = [*read_attempts,*self._dispatch_protection_cancels()]
            if not self._protection_frame_current(snapshot):
                return tuple(attempts)
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
                source = observation.source_frame.sources.get(f'market_book:{key.scope[1]}:{key.scope[2]}',
                    observation.source_frame.sources.get(f'market:{key.scope[1]}:{key.scope[2]}'))
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
                    if not self._protection_frame_current(snapshot):
                        return tuple(a for a in attempts if a is not None)
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
            self._evaluate_scoring(deadline)
            for scope in tuple(self.state.snapshot().openings):
                attempts.extend(self._evaluate_opening(scope))
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
        async_done=True
        if self.read_runtime is not None:
            # Stop admission first. Results already committed by the owner stay
            # durable; late retiring results cannot change a replacement owner.
            self.read_runtime.reconcile(())
            while True:
                self.read_runtime.drain_results()
                left=max(0,deadline-time.monotonic())
                async_done=self.read_runtime.close(min(.02,left))
                if async_done or left<=0:break
        reader_done = self.reader.close(timeout=max(0, deadline-time.monotonic()))
        control_done = self.control.close(max(0, deadline-time.monotonic())).writer_drained
        with self.state._lock:
            handles = (*self._native_jobs.values(), *self._manual_jobs.values())
        for handle in handles:
            handle._thread.join(timeout=max(0, deadline-time.monotonic()))
        with self._settings_lock:
            settings_thread = self._settings_thread
        if settings_thread is not None:
            settings_thread.join(timeout=max(0, deadline-time.monotonic()))
        # A synchronous batch may also be inside a real native call. Neither
        # local control reset nor this bounded close releases that owner.
        run_done = self._run_lock.acquire(timeout=max(0, deadline-time.monotonic()))
        if run_done:
            self._run_lock.release()
        try:
            self.writer.close(timeout=max(0, deadline-time.monotonic()))
        except TimeoutError:
            return False
        settings_done = not self.settings_status()['pending']
        return (async_done and reader_done and control_done and run_done and settings_done
            and all(not h._thread.is_alive() for h in handles))
