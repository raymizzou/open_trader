"""Reliable LP control persistence over the single CurrentLPState authority."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Callable, Protocol
import time
import threading
from concurrent.futures import Future

from .polymarket_lp_current_state import (
    AppliedControl, CommittedControl, ControlCloseReport, ControlRequestResult,
    ControlStatus, ControlWrite, CurrentLPState, EffectiveControlTransition,
    FatalControlSignal, StoredControl, ValidatedStartAdmission,
)


class ControlStorePort(Protocol):
    def load(self, account_id: str) -> StoredControl: ...
    def save(self, write: ControlWrite) -> CommittedControl: ...


class ControlActivationPort(Protocol):
    def apply(self, transition: EffectiveControlTransition) -> AppliedControl: ...


class FatalControlPort(Protocol):
    def trip(self, signal: FatalControlSignal) -> None: ...


def _optional_utc(value) -> bool:
    return value is None or (isinstance(value, datetime) and value.utcoffset() is not None
        and value.utcoffset().total_seconds() == 0)


class LPControlOwner:
    def __init__(self, state: CurrentLPState, store: ControlStorePort,
                 activation: ControlActivationPort, fatal: FatalControlPort, *,
                 clock: Callable[[], float] = time.monotonic,
                 utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._state, self._store, self._activation, self._fatal_port = state, store, activation, fatal
        self._clock, self._utc_now = clock, utc_now
        self._lock = state._lock
        self._latest: ControlWrite | None = None
        self._inflight: ControlWrite | None = None
        self._future: Future | None = None
        self._thread: threading.Thread | None = None
        self._closing = False
        self._episode_attempts = 0
        self._completed_monotonic = None
        self._attempt_admission = None

    def _set_status(self, **changes):
        self._state._control_status = replace(self._state._control_status, **changes)

    def status(self) -> ControlStatus:
        with self._lock:
            return replace(self._state._control_status, current=self._state._current,
                native_save_in_flight=self._thread is not None and self._thread.is_alive())

    def _trip(self, origin, outcome, error_code, *, write=None, attempt=0):
        current = self.status()
        if current.fatal:
            return current.fatal
        signal = FatalControlSignal(f'{self._state.process_epoch}:fatal:{origin}',
            self._state.process_epoch, self._state.account_id, origin,
            write.request_id if write else None, write.request_version if write else None,
            attempt, outcome, error_code, self._utc_now(), self._clock())
        self._state._current = replace(self._state._current, phase='stopping',
            shutdown_requested=True, fatal_id=signal.fatal_id)
        self._set_status(fatal=signal, error_code=error_code)
        self._fatal_port.trip(signal)
        return signal

    def initialize(self, admission: ValidatedStartAdmission) -> EffectiveControlTransition | FatalControlSignal:
        with self._lock:
            if self.status().fatal:
                return self.status().fatal
            if self._state._current.control_version is not None:
                raise ValueError('control_already_initialized')
            try:
                if (not isinstance(admission, ValidatedStartAdmission)
                        or admission.process_epoch != self._state.process_epoch or not admission.permit_claim_id):
                    raise ValueError('invalid_start_admission')
                stored = self._store.load(self._state.account_id)
                if (not isinstance(stored, StoredControl) or stored.account_id != self._state.account_id
                        or type(stored.desired_running) is not bool or type(stored.ever_enabled) is not bool
                        or not _optional_utc(stored.enabled_at) or not _optional_utc(stored.updated_at)):
                    raise ValueError('invalid_stored_control')
            except Exception as exc:
                return self._trip('startup_read', 'failed', type(exc).__name__)
            transition = EffectiveControlTransition(self._state.account_id, self._state.process_epoch,
                (self._state.process_epoch, 0), 0, None, None, stored.desired_running, 'startup')
            self._apply_transition(transition)
            self._set_status(requested_running=stored.desired_running, requested_version=0,
                saved_version=0, save_state='saved')
            return transition

    def _apply_transition(self, transition: EffectiveControlTransition) -> None:
        applied = self._activation.apply(transition)
        current = self._state._current
        if (not isinstance(applied, AppliedControl) or not applied.cycle_generation
                or current.transition_id != transition.transition_id
                or current.control_version != transition.control_version
                or current.effective_running is not transition.effective_running
                or current.cycle_generation != applied.cycle_generation):
            raise ValueError('activation_did_not_publish_control')

    def _activate(self, running: bool, version: int, cause: str) -> EffectiveControlTransition:
        current = self._state._current
        transition = EffectiveControlTransition(self._state.account_id, self._state.process_epoch,
            (self._state.process_epoch, version), version, current.control_version,
            current.effective_running, running, cause)
        self._apply_transition(transition)
        return transition

    def request_running(self, running: bool, *, actor: str | None = None) -> ControlRequestResult:
        with self._lock:
            status = self.status()
            if type(running) is not bool or status.current.control_version is None:
                return ControlRequestResult('rejected', 'identity_error', status)
            if self._closing or status.current.shutdown_requested:
                return ControlRequestResult('rejected', 'stopping', status)
            if (running and status.requested_running is False
                    and status.saved_version != status.requested_version):
                return ControlRequestResult('rejected', 'control_save_pending', status)
            if running == status.requested_running:
                return ControlRequestResult('noop', None, status)
            version = status.requested_version + 1
            write = ControlWrite(self._state.account_id, self._state.process_epoch, version,
                f'{self._state.process_epoch}:control:{version}', running, self._utc_now(), actor)
            if self._future is None and status.save_state != 'retry_wait':
                self._episode_attempts = 0
            self._latest = write
            self._set_status(requested_running=running, requested_version=version,
                save_state='retry_wait' if status.save_state == 'retry_wait' else 'pending')
            transition = None if running else self._activate(False, version, 'pause')
            if self._future is None and status.save_state != 'retry_wait':
                self._start_attempt()
            return ControlRequestResult('accepted', None, self.status(), transition)

    def _start_attempt(self):
        write = self._latest
        self._inflight = write
        self._future = future = Future()
        self._completed_monotonic = None
        self._set_status(save_state='pending', native_save_in_flight=True,
            attempt_started_monotonic=None, attempt_deadline_monotonic=None)
        def run():
            with self._lock:
                started = self._clock()
                self._episode_attempts += 1
                self._set_status(save_state='saving' if self._episode_attempts == 1 else 'retrying',
                    attempt_number=self._episode_attempts, retry_at_monotonic=None,
                    attempt_started_monotonic=started, attempt_deadline_monotonic=started + 10)
                admitted_save = getattr(self._store, 'save_with_admission', None)
                if callable(admitted_save):
                    from .lp_persistence import WriteAdmission
                    self._attempt_admission = WriteAdmission(started + 10, self._clock)
                else:
                    self._attempt_admission = None
            try:
                result = (admitted_save(write, self._attempt_admission) if callable(admitted_save)
                          else self._store.save(write))
            except BaseException as exc:
                self._completed_monotonic = self._clock()
                future.set_exception(exc)
            else:
                self._completed_monotonic = self._clock()
                future.set_result(result)
        self._thread = threading.Thread(target=run, name='lp-control-save', daemon=False)
        self._thread.start()

    def poll(self, now_monotonic: float) -> ControlStatus:
        with self._lock:
            status = self.status()
            if (self._future is not None and status.attempt_deadline_monotonic is not None
                    and now_monotonic > status.attempt_deadline_monotonic
                    and (not self._future.done() or self._completed_monotonic > status.attempt_deadline_monotonic)
                    and status.fatal is None):
                known_not_written = (self._attempt_admission is not None
                    and self._attempt_admission.cancel_before_begin())
                if not known_not_written:
                    self._set_status(save_state='unknown', error_code='save_unreturned')
                    self._trip('save_timeout', 'unknown', 'save_unreturned',
                        write=self._inflight, attempt=status.attempt_number)
            if self._future is not None and self._future.done() and not self._thread.is_alive():
                future, write = self._future, self._inflight
                self._future, self._inflight = None, None
                self._set_status(native_save_in_flight=False)
                try:
                    committed = future.result()
                    if (not isinstance(committed, CommittedControl) or committed.request_id != write.request_id
                            or committed.request_version != write.request_version
                            or committed.desired_running is not write.desired_running
                            or committed.committed_at is None or not _optional_utc(committed.committed_at)):
                        raise ValueError('invalid_commit_identity')
                except BaseException as exc:
                    if self._episode_attempts == 1 and not self.status().fatal:
                        self._set_status(save_state='retry_wait', error_code=type(exc).__name__,
                            retry_at_monotonic=self._completed_monotonic + 1)
                    else:
                        self._set_status(save_state='failed', error_code=type(exc).__name__)
                        if self._episode_attempts >= 2:
                            self._trip('save_retry_exhausted', 'failed', type(exc).__name__,
                                write=write, attempt=self._episode_attempts)
                else:
                    self._set_status(saved_version=committed.request_version, save_state='saved', error_code=None)
                    if (self._latest == write and write.desired_running
                            and not self._state._current.shutdown_requested and not self._closing):
                        self._activate(True, write.request_version, 'explicit_resume')
                    if self._latest != write and not self._state._current.shutdown_requested:
                        self._episode_attempts = 0
                        self._start_attempt()
            status = self.status()
            if (self._future is None and status.save_state == 'retry_wait'
                    and now_monotonic >= status.retry_at_monotonic and not status.fatal):
                self._start_attempt()
            return self.status()

    def close(self, timeout_seconds: float) -> ControlCloseReport:
        deadline = time.monotonic() + max(0, timeout_seconds)
        with self._lock:
            self._closing = True
        while True:
            with self._lock:
                status = self.poll(self._clock())
                thread = self._thread
                native_pending = self._future is not None or (thread is not None and thread.is_alive())
                save_pending = status.saved_version != status.requested_version and status.fatal is None
                drained = not native_pending and not save_pending
                remaining = deadline - time.monotonic()
                if drained or remaining <= 0:
                    return ControlCloseReport(drained,
                        self._inflight.request_id if self._inflight else None, status.fatal)
            if thread is not None and thread.is_alive():
                thread.join(timeout=min(remaining, 0.01))
            else:
                # Retry business time may be controlled independently of this bounded wait.
                time.sleep(min(remaining, 0.01))
