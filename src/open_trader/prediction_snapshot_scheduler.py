"""Issue #83: latest-snapshot-wins scheduler over the selected monitor set.

The scheduler turns each selected component's newest book snapshot into at most
one in-flight raw solve plus one pending replacement. Snapshots whose economic
content is unchanged from the last solved input only refresh timing
qualification; stale results whose input is no longer the newest snapshot are
dropped; worker failure, timeout, or a busy/unavailable solver server records
``UNKNOWN`` without an immediate retry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Literal

from open_trader.prediction_arbitrage import BookLevel
from open_trader.prediction_monitor_selection import SelectedComponent
from open_trader.prediction_n_leg import fingerprint
from open_trader.prediction_solver_worker import WorkerOutcome, WorkerRequest


SolveStatus = Literal["feasible", "infeasible", "unknown"]
SnapshotSource = Callable[[SelectedComponent], "ComponentSnapshot | None"]
SolveRequestBuilder = Callable[
    [SelectedComponent, "ComponentSnapshot"], WorkerRequest
]


@dataclass(frozen=True, slots=True)
class LegBook:
    """Venue-agnostic executable-book view used for the economic fingerprint."""

    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    taker_fee_bps: Decimal | None
    available: bool


@dataclass(frozen=True, slots=True)
class SnapshotLeg:
    """One component leg: book plus local/venue timing and venue sequence."""

    leg_id: str
    book: LegBook
    received_at: datetime | None
    exchange_time: datetime | None
    sequence: int | None


@dataclass(frozen=True, slots=True)
class ComponentSnapshot:
    component_id: str
    legs: tuple[SnapshotLeg, ...]


@dataclass(frozen=True, slots=True)
class SnapshotSolveResult:
    """The raw solve result recorded for the current economic snapshot."""

    component_id: str
    status: SolveStatus
    cost_bound: int | None
    economic_fingerprint: str
    order_ready: bool


def economic_fingerprint(snapshot: ComponentSnapshot) -> str:
    """Hash only cost-relevant fields; timing and sequence never enter."""
    if not snapshot.legs:
        raise ValueError("component snapshot must have at least one leg")
    return fingerprint(
        {
            "component_id": snapshot.component_id,
            "legs": tuple(
                {
                    "leg_id": leg.leg_id,
                    "bids": _levels(leg.book.bids),
                    "asks": _levels(leg.book.asks),
                    "taker_fee_bps": (
                        None
                        if leg.book.taker_fee_bps is None
                        else format(leg.book.taker_fee_bps, "f")
                    ),
                    "available": leg.book.available,
                }
                for leg in snapshot.legs
            ),
        }
    )


def _levels(levels: Sequence[BookLevel]) -> tuple[tuple[str, str], ...]:
    return tuple((format(level.price, "f"), format(level.size, "f")) for level in levels)


def order_ready(
    snapshot: ComponentSnapshot, *, now: datetime, freshness: timedelta
) -> bool:
    """Fail closed unless every leg has all timing fields and is fresh."""
    if not snapshot.legs:
        return False
    return all(
        leg.received_at is not None
        and leg.exchange_time is not None
        and leg.sequence is not None
        and (now - leg.received_at).total_seconds() <= freshness.total_seconds()
        and (now - leg.exchange_time).total_seconds() <= freshness.total_seconds()
        for leg in snapshot.legs
    )


def raw_solve_result(outcome: WorkerOutcome) -> tuple[SolveStatus, int | None]:
    """Map one worker outcome to the raw status/cost_bound result of #83."""
    if outcome.cleanup_proven and outcome.status == "OK" and outcome.response is not None:
        evidence = outcome.response.evidence
        if isinstance(evidence, Mapping):
            native = evidence.get("native_status")
            if native in {"OPTIMAL", "FEASIBLE"}:
                cost = evidence.get("cost_upper_bound_units")
                return "feasible", cost if isinstance(cost, int) else None
            if native == "INFEASIBLE":
                return "infeasible", None
    return "unknown", None


@dataclass
class _ComponentState:
    latest_snapshot: ComponentSnapshot | None = None
    latest_fingerprint: str | None = None
    in_flight: tuple[Future[WorkerOutcome], str] | None = None
    pending: tuple[SelectedComponent, ComponentSnapshot] | None = None
    last_solved_fingerprint: str | None = None
    latest_result: SnapshotSolveResult | None = None


class SnapshotScheduler:
    """Real-time scheduler: one in-flight solve plus one pending per component."""

    def __init__(
        self,
        solver_server: object,
        *,
        snapshot_for: SnapshotSource,
        build_solve_request: SolveRequestBuilder,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        freshness: timedelta = timedelta(seconds=30),
    ) -> None:
        if not callable(snapshot_for) or not callable(build_solve_request):
            raise ValueError("snapshot_for and build_solve_request must be callable")
        if not callable(getattr(solver_server, "submit", None)):
            raise ValueError("solver_server must expose submit(request)")
        if freshness <= timedelta(0):
            raise ValueError("freshness must be positive")
        self._solver_server = solver_server
        self._snapshot_for = snapshot_for
        self._build_solve_request = build_solve_request
        self._now_fn = now_fn
        self._freshness = freshness
        self._lock = RLock()
        self._states: dict[str, _ComponentState] = {}
        self._closed = False

    def refresh(
        self, components: Sequence[SelectedComponent]
    ) -> dict[str, str]:
        """Pull the newest snapshot per component and run one scheduling pass."""
        outcomes: dict[str, str] = {}
        for component in components:
            snapshot = self._snapshot_for(component)
            if snapshot is None:
                continue
            if snapshot.component_id != component.component_id:
                raise ValueError(
                    f"snapshot_for returned {snapshot.component_id!r} for "
                    f"component {component.component_id!r}"
                )
            outcomes[component.component_id] = self._observe(component, snapshot)
        return outcomes

    def result(self, component_id: str) -> SnapshotSolveResult | None:
        with self._lock:
            state = self._states.get(component_id)
            return None if state is None else state.latest_result

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for state in self._states.values():
                if state.in_flight is not None:
                    state.in_flight[0].cancel()

    def _observe(
        self, component: SelectedComponent, snapshot: ComponentSnapshot
    ) -> str:
        current = economic_fingerprint(snapshot)
        with self._lock:
            if self._closed:
                return "closed"
            state = self._states.setdefault(component.component_id, _ComponentState())
            state.latest_snapshot = snapshot
            state.latest_fingerprint = current
            if state.last_solved_fingerprint == current:
                self._refresh_order_ready(state)
                return "deduped"
            if state.in_flight is not None:
                if state.in_flight[1] == current:
                    state.pending = None
                    return "deduped"
                state.pending = (component, snapshot)
                return "replaced"
            return self._dispatch(component, snapshot, current, state)

    def _dispatch(
        self,
        component: SelectedComponent,
        snapshot: ComponentSnapshot,
        current: str,
        state: _ComponentState,
    ) -> str:
        try:
            request = self._build_solve_request(component, snapshot)
            future = self._solver_server.submit(request)
        except Exception:
            state.last_solved_fingerprint = current
            state.latest_result = SnapshotSolveResult(
                component.component_id,
                "unknown",
                None,
                current,
                order_ready(
                    snapshot, now=self._now_fn(), freshness=self._freshness
                ),
            )
            return "failed"
        state.in_flight = (future, current)
        future.add_done_callback(
            lambda completed: self._completed(component.component_id, current, completed)
        )
        return "scheduled"

    def _completed(
        self,
        component_id: str,
        input_fingerprint: str,
        completed: Future[WorkerOutcome],
    ) -> None:
        try:
            status, cost_bound = raw_solve_result(completed.result())
        except Exception:
            status, cost_bound = "unknown", None
        with self._lock:
            state = self._states.get(component_id)
            if (
                state is None
                or state.in_flight is None
                or state.in_flight[0] is not completed
                or state.in_flight[1] != input_fingerprint
            ):
                return
            state.in_flight = None
            pending = state.pending
            state.pending = None
            if state.latest_fingerprint != input_fingerprint:
                if pending is not None:
                    self._dispatch(
                        pending[0],
                        pending[1],
                        economic_fingerprint(pending[1]),
                        state,
                    )
                return
            state.last_solved_fingerprint = input_fingerprint
            snapshot = state.latest_snapshot
            state.latest_result = SnapshotSolveResult(
                component_id,
                status,
                cost_bound,
                input_fingerprint,
                order_ready(
                    snapshot, now=self._now_fn(), freshness=self._freshness
                )
                if snapshot is not None
                else False,
            )

    def _refresh_order_ready(self, state: _ComponentState) -> None:
        result = state.latest_result
        snapshot = state.latest_snapshot
        if result is None or snapshot is None:
            return
        state.latest_result = replace(
            result,
            order_ready=order_ready(
                snapshot, now=self._now_fn(), freshness=self._freshness
            ),
        )
