"""Process-local LP rounds and immutable action identity; no trading or I/O.

351 supplies already accepted 357 views and serializes control/admission with
these short memory operations. A result never authorizes a send on its own.
"""
from dataclasses import dataclass, field, replace
from enum import StrEnum
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5


class ValidatedStartupControlView(Protocol):
    @property
    def account_id(self) -> str: ...
    @property
    def transition_id(self) -> str: ...
    @property
    def control_version(self) -> int: ...
    @property
    def validated(self) -> bool: ...
    @property
    def fatal(self) -> bool: ...


class EffectiveControlTransitionView(Protocol):
    @property
    def account_id(self) -> str: ...
    @property
    def transition_id(self) -> str: ...
    @property
    def control_version(self) -> int: ...
    @property
    def previous_running(self) -> bool: ...
    @property
    def effective_running(self) -> bool: ...
    @property
    def cause(self) -> str: ...
    @property
    def admitted(self) -> bool: ...


class CurrentControlView(Protocol):
    @property
    def account_id(self) -> str: ...
    @property
    def transition_id(self) -> str: ...
    @property
    def control_version(self) -> int: ...
    @property
    def cycle_generation(self) -> str: ...
    @property
    def effective_running(self) -> bool: ...
    @property
    def fatal(self) -> bool: ...


class CurrentOrder(Protocol):
    @property
    def account_id(self) -> str: ...
    @property
    def venue(self) -> str: ...
    @property
    def token_id(self) -> str: ...
    @property
    def order_id(self) -> str: ...
    @property
    def side(self) -> str: ...


class CurrentOrderView(Protocol):
    @property
    def account_id(self) -> str: ...
    @property
    def cycle_generation(self) -> str: ...
    @property
    def observation_id(self) -> str: ...
    @property
    def accepted(self) -> bool: ...
    @property
    def orders_current(self) -> bool: ...
    @property
    def orders_complete(self) -> bool: ...
    @property
    def orders_error(self) -> str | None: ...
    @property
    def open_orders(self) -> tuple[CurrentOrder, ...]: ...


class ActionKind(StrEnum):
    BUY_NEW = 'BUY_NEW'
    CANCEL_EXISTING_BUY = 'CANCEL_EXISTING_BUY'


class BatchKind(StrEnum):
    STARTUP_CANCEL = 'STARTUP_CANCEL'
    NORMAL_BUY = 'NORMAL_BUY'


SendOption = str | bool | int | Decimal | None


@dataclass(frozen=True)
class BuyTerms:
    price: Decimal
    quantity: Decimal
    order_type: str
    expiration: int | None = None
    post_only: bool = False
    send_options: tuple[tuple[str, SendOption], ...] = ()

    def __post_init__(self):
        if (not isinstance(self.price, Decimal) or not self.price.is_finite()
                or not 0 < self.price < 1 or not isinstance(self.quantity, Decimal)
                or not self.quantity.is_finite() or self.quantity <= 0 or not self.order_type):
            raise ValueError('exact finite BUY terms required')
        if (not isinstance(self.send_options, tuple)
                or any(not isinstance(item, tuple) or len(item) != 2
                       or not isinstance(item[0], str) or not item[0]
                       or not isinstance(item[1], (str, bool, int, Decimal, type(None)))
                       for item in self.send_options)
                or len({key for key, _ in self.send_options}) != len(self.send_options)):
            raise ValueError('immutable unique send options required')


@dataclass(frozen=True)
class BuyTarget:
    venue: str
    token_id: str
    market_id: str
    terms: BuyTerms
    kind: ActionKind = field(default=ActionKind.BUY_NEW, init=False)

    def __post_init__(self):
        if not all((self.venue, self.token_id, self.market_id)) or not isinstance(self.terms, BuyTerms):
            raise ValueError('fixed BUY identity and terms required')


@dataclass(frozen=True)
class FixedBuyPlan:
    account_id: str
    cycle_generation: str
    observation_id: str
    plan_id: str
    targets: tuple[BuyTarget, ...]

    def __post_init__(self):
        if not isinstance(self.targets, tuple) or any(not isinstance(t, BuyTarget) for t in self.targets):
            raise ValueError('immutable BUY targets required')


@dataclass(frozen=True)
class CancelTarget:
    venue: str
    token_id: str
    existing_order_id: str
    side: str = field(default='BUY', init=False)
    kind: ActionKind = field(default=ActionKind.CANCEL_EXISTING_BUY, init=False)

    def __post_init__(self):
        if not all((self.venue, self.token_id, self.existing_order_id)):
            raise ValueError('exact existing BUY identity required')


@dataclass(frozen=True)
class ActionIdentity:
    request_id: str
    account_id: str
    target: 'BuyTarget | CancelTarget'


@dataclass(frozen=True)
class RoundBatchToken:
    account_id: str
    cycle_generation: str
    batch_id: str
    round_number: int
    kind: BatchKind
    observation_id: str
    actions: tuple[ActionIdentity, ...]


class LocalStatus(StrEnum):
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    NOT_SENT = 'not_sent'
    UNKNOWN_TIMEOUT = 'unknown_timeout'
    UNKNOWN_ERROR = 'unknown_error'


@dataclass(frozen=True)
class LocalOutcome:
    identity: ActionIdentity
    status: LocalStatus
    local_attempt_finished: bool
    venue_order_id: str | None = None
    receipt_status: str | None = None


class CycleStatus(StrEnum):
    INITIALIZED = 'initialized'
    APPLIED = 'applied'
    DUPLICATE = 'duplicate'
    STALE = 'stale'
    INVALID_IDENTITY = 'invalid_identity'


class FinishStatus(StrEnum):
    COMPLETED = 'completed'
    INCOMPLETE = 'incomplete'
    DUPLICATE = 'duplicate'
    STALE = 'stale'
    CONTROL_NOT_CURRENT = 'control_not_current'
    IDENTITY_ERROR = 'identity_error'


@dataclass(frozen=True)
class RoundFinish:
    status: FinishStatus
    cycle_generation: str
    round_number: int


class StartStatus(StrEnum):
    OBSERVE_ONLY = 'observe_only'
    BLOCKED = 'blocked'
    BUSY = 'busy'
    NEEDS_FIXED_PLAN = 'needs_fixed_plan'
    BATCH = 'batch'


@dataclass(frozen=True)
class CycleUpdate:
    status: CycleStatus
    cycle_generation: str
    round_number: int
    reset_provider_wait: bool = False
    reset_unknown_since: bool = False


@dataclass(frozen=True)
class RoundSnapshot:
    account_id: str
    cycle_generation: str
    round_number: int
    applied_transition_id: str
    control_version: int
    active_batch_id: str | None = None
    active_batch_kind: str | None = None
    phase: str = 'idle'


@dataclass(frozen=True)
class RoundStart:
    status: StartStatus
    token: 'RoundBatchToken | None' = None
    reason: str | None = None


class LPRoundCoordinator:
    """One instance per existing runtime lifetime; 357 retains control authority."""

    def __init__(self):
        self._state: RoundSnapshot | None = None
        self._process_epoch: str | None = None
        self._active: RoundBatchToken | None = None
        self._completed: dict[str, RoundBatchToken] = {}
        self._cycle_ordinal = 0
        self._transitions: dict[str, tuple] = {}

    def initialize(self, startup: ValidatedStartupControlView, *, process_epoch: str) -> CycleUpdate:
        if not (startup.validated and not startup.fatal and startup.account_id
                and startup.transition_id and process_epoch and startup.control_version >= 0):
            raise ValueError('validated nonfatal startup control required')
        if self._state is not None:
            return CycleUpdate(CycleStatus.INVALID_IDENTITY, self._state.cycle_generation, self._state.round_number)
        self._process_epoch = process_epoch
        generation = str(uuid5(NAMESPACE_URL, f'lp-cycle:{len(process_epoch)}:{process_epoch}:0'))
        self._state = RoundSnapshot(startup.account_id, generation, 0,
                                    startup.transition_id, startup.control_version)
        return CycleUpdate(CycleStatus.INITIALIZED, generation, 0, True, True)

    def apply_control(self, transition: EffectiveControlTransitionView) -> CycleUpdate:
        state = self.snapshot()
        def result(status):
            return CycleUpdate(status, state.cycle_generation, state.round_number)
        if (transition.account_id != state.account_id or not transition.admitted
                or not transition.transition_id):
            return result(CycleStatus.INVALID_IDENTITY)
        identity = (transition.account_id, transition.control_version,
                    transition.previous_running, transition.effective_running, transition.cause)
        known = self._transitions.get(transition.transition_id)
        if known is not None:
            return result(CycleStatus.DUPLICATE if known == identity else CycleStatus.INVALID_IDENTITY)
        if transition.control_version < state.control_version:
            return result(CycleStatus.STALE)
        if transition.control_version == state.control_version:
            return result(CycleStatus.INVALID_IDENTITY)
        if transition.previous_running == transition.effective_running:
            return result(CycleStatus.DUPLICATE)
        resume = (not transition.previous_running and transition.effective_running
                  and transition.cause == 'explicit_resume')
        if transition.previous_running != transition.effective_running and not (
                resume or transition.cause == 'pause' and not transition.effective_running):
            return result(CycleStatus.INVALID_IDENTITY)
        self._transitions[transition.transition_id] = identity
        self._state = replace(state, applied_transition_id=transition.transition_id,
                              control_version=transition.control_version)
        if resume:
            self._cycle_ordinal += 1
            generation = str(uuid5(NAMESPACE_URL,
                f'lp-cycle:{len(self._process_epoch)}:{self._process_epoch}:{self._cycle_ordinal}'))
            self._active = None
            self._completed.clear()
            self._state = replace(self._state, cycle_generation=generation, round_number=0,
                                  active_batch_id=None, active_batch_kind=None, phase='idle')
            return CycleUpdate(CycleStatus.APPLIED, generation, 0, True, True)
        return CycleUpdate(CycleStatus.APPLIED, state.cycle_generation, state.round_number)

    def begin_round(self, control: CurrentControlView, observation: CurrentOrderView, *, fixed_buys: FixedBuyPlan | None = None) -> RoundStart:
        state = self.snapshot()
        if not self._control_is_current(control):
            return RoundStart(StartStatus.BLOCKED, reason='control_not_current')
        if not control.effective_running or control.fatal:
            return RoundStart(StartStatus.OBSERVE_ONLY)
        state = self.snapshot()
        if self._active is not None:
            return RoundStart(StartStatus.BUSY, self._active)
        if not (observation.account_id == state.account_id
                and observation.cycle_generation == state.cycle_generation
                and observation.observation_id and observation.accepted
                and observation.orders_current and observation.orders_complete
                and observation.orders_error is None):
            return RoundStart(StartStatus.BLOCKED, reason='orders_not_current_complete')
        seen = set()
        for order in observation.open_orders:
            if (order.account_id != state.account_id or not order.venue
                    or not order.token_id or not order.order_id
                    or order.side not in {'BUY', 'SELL'} or order.order_id in seen):
                return RoundStart(StartStatus.BLOCKED, reason='order_identity')
            seen.add(order.order_id)
        if state.round_number == 0:
            kind = BatchKind.STARTUP_CANCEL
            targets = tuple(CancelTarget(o.venue, o.token_id, o.order_id)
                            for o in observation.open_orders if o.side == 'BUY')
        else:
            if fixed_buys is None:
                return RoundStart(StartStatus.NEEDS_FIXED_PLAN)
            if (fixed_buys.account_id != state.account_id
                    or fixed_buys.cycle_generation != state.cycle_generation
                    or fixed_buys.observation_id != observation.observation_id
                    or not fixed_buys.plan_id):
                return RoundStart(StartStatus.BLOCKED, reason='plan_identity')
            kind = BatchKind.NORMAL_BUY
            targets = fixed_buys.targets
        token = RoundBatchToken(state.account_id, state.cycle_generation, str(uuid4()),
                                state.round_number, kind,
                                observation.observation_id,
                                tuple(ActionIdentity(str(uuid4()), state.account_id, t) for t in targets))
        self._active = token
        self._state = replace(state, active_batch_id=token.batch_id,
                              active_batch_kind=token.kind, phase='active')
        return RoundStart(StartStatus.BATCH, token)

    def finish_local_attempts(self, control: CurrentControlView, token: RoundBatchToken, outcomes: tuple[LocalOutcome, ...]) -> RoundFinish:
        state = self.snapshot()
        def result(status):
            return RoundFinish(status, state.cycle_generation, state.round_number)
        if token.account_id != state.account_id:
            return result(FinishStatus.IDENTITY_ERROR)
        if token.cycle_generation != state.cycle_generation:
            return result(FinishStatus.STALE)
        if not self._control_is_current(control) or not control.effective_running or control.fatal:
            return result(FinishStatus.CONTROL_NOT_CURRENT)
        if self._completed.get(token.batch_id) == token:
            return result(FinishStatus.DUPLICATE)
        if token != self._active:
            return result(FinishStatus.IDENTITY_ERROR)
        expected = {action.request_id: action for action in token.actions}
        seen = set()
        for outcome in outcomes:
            identity = outcome.identity
            if (identity.request_id in seen or expected.get(identity.request_id) != identity
                    or outcome.status not in tuple(LocalStatus)):
                return result(FinishStatus.IDENTITY_ERROR)
            if (isinstance(identity.target, CancelTarget)
                    and outcome.venue_order_id is not None
                    and outcome.venue_order_id != identity.target.existing_order_id):
                return result(FinishStatus.IDENTITY_ERROR)
            seen.add(identity.request_id)
        if seen != set(expected) or any(not o.local_attempt_finished for o in outcomes):
            return result(FinishStatus.INCOMPLETE)
        self._completed[token.batch_id] = token
        self._active = None
        self._state = replace(state, round_number=state.round_number + 1,
                              active_batch_id=None, active_batch_kind=None, phase='idle')
        return RoundFinish(FinishStatus.COMPLETED, state.cycle_generation, state.round_number + 1)

    def _control_is_current(self, control: CurrentControlView) -> bool:
        state = self.snapshot()
        return (control.account_id == state.account_id
                and control.cycle_generation == state.cycle_generation
                and control.control_version == state.control_version
                and control.transition_id == state.applied_transition_id)

    def snapshot(self) -> RoundSnapshot:
        if self._state is None:
            raise RuntimeError('initialize must succeed first')
        return self._state
