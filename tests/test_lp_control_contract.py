from datetime import UTC, datetime
from decimal import Decimal
import threading
import time

import open_trader.polymarket_lp_current_state as cs
import open_trader.polymarket_lp_control as control
from open_trader.polymarket_lp_market_reader import CapturedSources, MarketReader, SourceResult
from open_trader.polymarket_lp_market_calculation import CalculationResult

T = datetime(2026, 10, 16, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class FatalPort:
    def __init__(self):
        self.signals = []

    def trip(self, signal):
        self.signals.append(signal)


class ActivationPort:
    def __init__(self, state):
        self.state = state
        self.transitions = []

    def apply(self, transition):
        self.transitions.append(transition)
        # The external integration owns generation allocation and activation.
        generation = ('initial-cycle' if transition.cause == 'startup' else
            'resumed-cycle' if transition.cause == 'explicit_resume' else self.state.snapshot().current.cycle_generation)
        self.state.apply_effective_control(transition, generation)
        return cs.AppliedControl(generation)


class StorePort:
    def __init__(self, running=True):
        self.running = running
        self.load_error = None
        self.writes = []

    def load(self, account_id):
        if self.load_error:
            raise self.load_error
        return cs.StoredControl(account_id, self.running, True, T, T)

    def save(self, write):
        self.writes.append(write)
        return cs.CommittedControl(write.request_id, write.request_version, write.desired_running, T)


def setup(store=None):
    clock = Clock()
    state = cs.CurrentLPState('A', 'process', clock=clock, utc_now=lambda: T)
    store, fatal = store or StorePort(), FatalPort()
    activation = ActivationPort(state)
    owner = control.LPControlOwner(state, store, activation, fatal, clock=clock, utc_now=lambda: T)
    return owner, state, store, activation, fatal, clock


def initialize(owner):
    return owner.initialize(cs.ValidatedStartAdmission('process', 'consumed-permit'))


def test_startup_control_read_failure_has_no_running_default():
    store = StorePort()
    store.load_error = OSError('storage unavailable')
    owner, state, store, activation, fatal, clock = setup(store)
    signal = initialize(owner)
    assert isinstance(signal, cs.FatalControlSignal)
    assert signal.origin == 'startup_read'
    assert signal.outcome == 'failed'
    assert signal.attempt_number == 0
    assert owner.status().current.effective_running is None
    assert owner.status().current.shutdown_requested is True
    assert owner.status().current.phase == 'stopping'
    assert owner.status().fatal == signal
    assert fatal.signals == [signal]
    assert activation.transitions == []
    assert store.writes == []


class BlockingStore(StorePort):
    def __init__(self, outcomes=(True,), running=True):
        super().__init__(running)
        self.outcomes = outcomes
        self.entered = [threading.Event() for _ in outcomes]
        self.release = [threading.Event() for _ in outcomes]
        self.returned = [threading.Event() for _ in outcomes]
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def save(self, write):
        with self.lock:
            index = len(self.writes)
            self.writes.append(write)
            self.active += 1
            self.max_active = max(self.active, self.max_active)
        try:
            self.entered[index].set()
            if not self.release[index].wait(3):
                raise AssertionError('test did not release storage')
            if not self.outcomes[index]:
                raise OSError('explicit save failure')
            return cs.CommittedControl(write.request_id, write.request_version, write.desired_running, T)
        finally:
            with self.lock:
                self.active -= 1
            self.returned[index].set()

    def release_all(self):
        for event in self.release:
            event.set()


def poll_until(owner, clock, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = owner.poll(clock())
        if predicate(status):
            return status
        threading.Event().wait(0.001)
    raise AssertionError(f'public status did not converge: {owner.status()}')


def accept_empty_observation(state):
    source = MarketReader.from_captured_sources(CapturedSources('A', {
        name: SourceResult(name, [] if name != 'balance' else ('20', '20'), checked_at=T)
        for name in ('open_orders', 'positions', 'balance')}))
    ticket = state.begin_observation(state.snapshot().current.cycle_generation, tuple(source.sources))
    return state.accept(ticket, source, CalculationResult(Decimal('0'), Decimal('0'), Decimal('20')), evaluated_at=T)


def test_pause_is_immediate_while_save_is_blocked():
    owner, state, store, activation, fatal, clock = setup(BlockingStore())
    initialize(owner)
    try:
        result = owner.request_running(False)
        assert store.entered[0].wait(3)
        assert result.status == 'accepted'
        assert owner.status().current.effective_running is False
        assert owner.status().native_save_in_flight is True
        assert owner.status().saved_version == 0
        assert owner.status().requested_version == 1
        assert owner.status().save_state in ('pending', 'saving')
        assert accept_empty_observation(state).status == 'accepted'
        assert state.snapshot().observation is not None
        assert not fatal.signals
    finally:
        store.release_all()
        owner.close(3)


def test_first_save_failure_retries_once_and_reports_actual_commit():
    owner, state, store, activation, fatal, clock = setup(BlockingStore((False, True)))
    initialize(owner)
    try:
        owner.request_running(False)
        assert store.entered[0].wait(3)
        store.release[0].set()
        assert store.returned[0].wait(3)
        failed = poll_until(owner, clock, lambda s: not s.native_save_in_flight)
        assert failed.save_state == 'retry_wait'
        assert failed.retry_at_monotonic == 101
        assert failed.saved_version == 0
        clock.value = 100.999
        owner.poll(clock())
        assert len(store.writes) == 1
        clock.value = 101
        owner.poll(clock())
        assert store.entered[1].wait(3)
        assert owner.status().save_state == 'retrying'
        assert owner.status().saved_version == 0
        assert owner.status().current.effective_running is False
        store.release[1].set()
        saved = poll_until(owner, clock, lambda s: s.save_state == 'saved')
        assert saved.saved_version == 1
        assert saved.attempt_number == 2
        assert len(store.writes) == 2
        assert store.max_active == 1
        assert not fatal.signals
    finally:
        store.release_all()
        owner.close(3)


def test_retry_failure_emits_fatal_shutdown_once():
    owner, state, store, activation, fatal, clock = setup(BlockingStore((False, False)))
    initialize(owner)
    try:
        owner.request_running(False)
        assert store.entered[0].wait(3)
        store.release[0].set()
        poll_until(owner, clock, lambda s: s.save_state == 'retry_wait')
        clock.value = 101
        owner.poll(clock())
        assert store.entered[1].wait(3)
        store.release[1].set()
        failed = poll_until(owner, clock, lambda s: not s.native_save_in_flight)
        assert failed.current.shutdown_requested is True
        assert failed.fatal.origin == 'save_retry_exhausted'
        assert failed.fatal.attempt_number == 2
        assert failed.current.effective_running is False
        assert failed.saved_version == 0
        assert failed.save_state == 'failed'
        clock.value = 200
        owner.poll(clock())
        owner.poll(clock())
        assert len(fatal.signals) == 1
        assert len(store.writes) == 2
        assert owner.request_running(True).reason == 'stopping'
    finally:
        store.release_all()
        owner.close(3)


def test_old_save_completion_cannot_acknowledge_latest_control():
    owner, state, store, activation, fatal, clock = setup(BlockingStore((True, True, True)))
    initialize(owner)
    try:
        owner.request_running(False)
        assert store.entered[0].wait(3)
        store.release[0].set()
        poll_until(owner, clock, lambda s: s.saved_version == 1)
        resume = owner.request_running(True)
        assert resume.status == 'accepted'
        assert resume.transition is None
        assert store.entered[1].wait(3)
        pause = owner.request_running(False)
        assert pause.status == 'accepted'
        assert pause.state.current.control_version == 3
        assert pause.state.current.effective_running is False
        store.release[1].set()
        poll_until(owner, clock, lambda s: s.saved_version == 2)
        assert store.entered[2].wait(3)
        assert owner.status().current.control_version == 3
        assert owner.status().current.effective_running is False
        assert owner.status().saved_version == 2
        assert owner.status().requested_version == 3
        assert all(t.cause != 'explicit_resume' for t in activation.transitions)
        store.release[2].set()
        saved = poll_until(owner, clock, lambda s: s.saved_version == 3)
        assert saved.current.effective_running is False
        assert saved.save_state == 'saved'
        assert [(w.request_version, w.desired_running) for w in store.writes] == [(1, False), (2, True), (3, False)]
        assert store.max_active == 1
    finally:
        store.release_all()
        owner.close(3)


def test_close_retains_actual_writer_until_completion():
    owner, state, store, activation, fatal, clock = setup(BlockingStore())
    initialize(owner)
    try:
        owner.request_running(False)
        assert store.entered[0].wait(3)
        first = owner.close(0)
        assert first.writer_drained is False
        assert first.native_request_id == store.writes[0].request_id
        assert owner.status().native_save_in_flight is True
        assert len(store.writes) == 1
        assert not store.returned[0].is_set()
        assert owner.request_running(True).reason == 'stopping'
        store.release[0].set()
        final = owner.close(3)
        assert final.writer_drained is True
        assert final.native_request_id is None
        assert store.returned[0].is_set()
        assert owner.status().native_save_in_flight is False
        assert owner.status().saved_version == 1
        assert store.max_active == 1
    finally:
        store.release_all()
        owner.close(3)


def test_write_watchdog_never_duplicates_uncertain_inflight_write():
    owner, state, store, activation, fatal, clock = setup(BlockingStore())
    initialize(owner)
    try:
        owner.request_running(False)
        assert store.entered[0].wait(3)
        clock.value = 110
        assert owner.poll(clock()).fatal is None
        clock.value = 110.001
        unknown = owner.poll(clock())
        assert unknown.save_state == 'unknown'
        assert unknown.fatal.origin == 'save_timeout'
        assert unknown.fatal.outcome == 'unknown'
        assert unknown.current.shutdown_requested is True
        assert unknown.native_save_in_flight is True
        assert unknown.saved_version == 0
        assert len(store.writes) == 1
        assert owner.close(0).writer_drained is False
        clock.value = 112
        store.release[0].set()
        assert owner.close(3).writer_drained is True
        late = owner.status()
        assert late.save_state == 'saved'
        assert late.saved_version == 1
        assert late.current.shutdown_requested is True
        assert late.current.phase == 'stopping'
        assert late.current.effective_running is False
        assert late.fatal == unknown.fatal
        assert len(fatal.signals) == 1
        assert len(store.writes) == 1
        assert store.max_active == 1
        assert all(t.cause != 'explicit_resume' for t in activation.transitions)
    finally:
        store.release_all()
        owner.close(3)


def test_resume_during_pending_pause_save_is_rejected_without_deferred_resume():
    owner, state, store, activation, fatal, clock = setup(BlockingStore((True, True)))
    initialize(owner)
    try:
        owner.request_running(False)
        assert store.entered[0].wait(3)
        rejected = owner.request_running(True)
        assert rejected.status == 'rejected'
        assert rejected.reason == 'control_save_pending'
        assert rejected.state.requested_version == 1
        assert rejected.state.requested_running is False
        assert accept_empty_observation(state).status == 'accepted'
        store.release[0].set()
        saved = poll_until(owner, clock, lambda s: s.saved_version == 1)
        assert saved.current.effective_running is False
        assert saved.requested_version == 1
        assert len(store.writes) == 1
        assert all(t.cause != 'explicit_resume' for t in activation.transitions)
        explicit = owner.request_running(True)
        assert explicit.status == 'accepted'
        assert explicit.state.current.effective_running is False
        assert store.entered[1].wait(3)
        store.release[1].set()
        resumed = poll_until(owner, clock, lambda s: s.current.effective_running is True)
        assert resumed.saved_version == 2
        assert resumed.current.control_version == 2
    finally:
        store.release_all()
        owner.close(3)


def test_running_duplicate_request_has_no_control_transition_or_save():
    owner, state, store, activation, fatal, clock = setup()
    initialize(owner)
    before = owner.status()
    transitions = tuple(activation.transitions)
    duplicate = owner.request_running(True)
    assert duplicate.status == 'noop'
    assert duplicate.transition is None
    assert owner.status() == before
    assert tuple(activation.transitions) == transitions
    assert store.writes == []
    assert not fatal.signals
    assert owner.close(0).writer_drained is True
