"""Issue #87: refresh the persisted #77 monitor selection on generation change.

``PredictionMonitorSelectionDriver`` is a production-only daemon owned by
``PredictionRuntime``. It watches ``relation_catalog.generation_meta()`` for a
new generation key, queues distinct keys (bounded to 32) while the #52 live
resolver is busy, and then runs one #77 discovery pass over the latest catalog
whenever the live outcome-tracking server is idle.

The pass reuses ``relation_generation_problem()``, ``run_discovery()``,
``select_monitor_components()`` and ``MonitorSelectionStore`` unchanged.
Retained selection entries keep their component only while the recompiled
sub-problem fingerprints still match; new components are discovered once per
pass. The persisted selection is written only on a complete successful pass.
Pending/failure state is in-memory only and exposed through ``status()``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping

from open_trader.prediction_live_resolver import LIVE_BUDGET, LIVE_LIMITS
from open_trader.prediction_monitor_selection import (
    MonitorSelectionStore,
    SelectedComponent,
    problem_for_component,
    relation_generation_problem,
    run_discovery,
    select_monitor_components,
)
from open_trader.prediction_n_leg import ArbitrageProblem, fingerprint


logger = logging.getLogger(__name__)

_PENDING_MAXLEN = 32
_RETRY_SECONDS = 5.0
_MAX_COMPONENTS = 10
_CODE_VERSION = "issue-87"


class PredictionMonitorSelectionDriver:
    """Poll generation changes and refresh the selected monitor set when idle."""

    def __init__(
        self,
        *,
        relation_catalog: object,
        selection_store: MonitorSelectionStore,
        idle_check: Callable[[], bool],
        poll_interval: float = 1.0,
    ) -> None:
        if not callable(idle_check):
            raise ValueError("idle_check must be callable")
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or poll_interval <= 0
        ):
            raise ValueError("poll_interval must be positive")
        self._relation_catalog = relation_catalog
        self._selection_store = selection_store
        self._idle_check = idle_check
        self._poll_interval = float(poll_interval)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: deque[tuple[int, str]] = deque(maxlen=_PENDING_MAXLEN)
        self._observed_key: tuple[int, str] | None = None
        self._failures = 0
        self._next_attempt_at: float | None = None
        self._applied_generation: int | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="prediction-monitor-selection-driver",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            self._thread = None

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "selection_pending": len(self._pending),
                "selection_failures_consecutive": self._failures,
                "selection_applied_generation": self._applied_generation,
            }

    def _loop(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            try:
                self._tick()
            except Exception:
                logger.exception("prediction_monitor_selection_driver tick failed")

    def _tick(self) -> None:
        key = self._generation_key(self._relation_catalog.generation_meta())
        with self._lock:
            if key != self._observed_key:
                self._observed_key = key
                if key not in self._pending:
                    self._pending.append(key)
            pending = bool(self._pending)
            blocked = (
                self._next_attempt_at is not None
                and time.monotonic() < self._next_attempt_at
            )
        if not pending or blocked:
            return
        # FIFO: one idle check gates the whole discovery pass. Once started the
        # pass finishes even if live becomes busy mid-pass (run_discovery keeps
        # the module-level idle_capacity() seam, which defaults True).
        if not self._idle_check():
            return
        self._process()

    def _process(self) -> None:
        try:
            self._run_pass()
        except Exception:
            logger.exception("prediction_monitor_selection_driver pass failed")
            self._record_failure()

    def _run_pass(self) -> None:
        meta = self._relation_catalog.generation_meta()
        key = self._generation_key(meta)
        generation = key[0]
        rows = dict(self._relation_catalog.current_generation())
        problem, components = relation_generation_problem(rows)
        if problem is None:
            self._selection_store.save({})
            self._record_success(key)
            return
        components_by_id = {
            component.component_id: component for component in components
        }
        _, current = self._selection_store.load()
        retained = self._retain(current, problem, components_by_id)
        new_components = tuple(
            component
            for component in components
            if component.component_id not in retained
        )
        results = run_discovery(
            problem,
            new_components,
            budget=LIVE_BUDGET,
            limits=LIVE_LIMITS,
            generation=generation,
            code_version=_CODE_VERSION,
            max_components=_MAX_COMPONENTS,
        )
        candidates = {
            component.component_id: resolution
            for component, resolution in zip(new_components, results)
        }
        selection = select_monitor_components(
            candidates,
            retained,
            problem=problem,
            components=components_by_id,
            max_slots=_MAX_COMPONENTS,
        )
        self._selection_store.save(selection)
        self._record_success(key)

    def _retain(
        self,
        current: Mapping[str, SelectedComponent],
        problem: ArbitrageProblem,
        components_by_id: Mapping[str, object],
    ) -> dict[str, SelectedComponent]:
        retained: dict[str, SelectedComponent] = {}
        for component_id, selected in current.items():
            component = components_by_id.get(component_id)
            if component is None:
                continue
            sub = problem_for_component(problem, component)
            if (
                fingerprint({"constraint_model": sub.constraint_model})
                == selected.relation_fingerprint
                and fingerprint({"terminal_state_sets": sub.terminal_state_sets})
                == selected.terminal_fingerprint
            ):
                retained[component_id] = selected
        return retained

    def _record_success(self, key: tuple[int, str]) -> None:
        with self._lock:
            self._observed_key = key
            self._pending.clear()
            self._failures = 0
            self._next_attempt_at = None
            self._applied_generation = key[0]

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._next_attempt_at = time.monotonic() + _RETRY_SECONDS

    @staticmethod
    def _generation_key(meta: Mapping[str, object]) -> tuple[int, str]:
        return (int(meta.get("generation", 0)), str(meta.get("fingerprint", "")))


__all__ = ["PredictionMonitorSelectionDriver"]
