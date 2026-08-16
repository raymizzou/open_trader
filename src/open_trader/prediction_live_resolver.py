"""Issue #52: live N-leg resolver for the production Prediction runtime.

The resolver owns the #82 runtime relation graph, the #83 latest-snapshot-wins
scheduler, and the #84 Market/ExecutionSolution interpretation. One daemon
thread advances the catalog generation, prunes the persisted #77 selected
monitor set to components whose structure still matches the recompiled model,
dispatches live Polymarket books to the bounded solver server, and turns
completed worker evidence into a verified MarketSolution without any second
solver pass.

Scope: no discovery/selection, no orders, no ORDER_READY, no partial-fill
proofs (#74/#85), and Predict.fun legs fail closed. The solver server is
injected and never closed here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from open_trader.prediction_market_solution import (
    AccountView,
    ExecutionSolution,
    MarketSolution,
    build_solve_request,
    execution_solution_from_market,
    market_solution_from_verification,
)
from open_trader.prediction_monitor_selection import (
    MonitorSelectionStore,
    SelectedComponent,
    problem_for_component,
    relation_generation_problem,
)
from open_trader.prediction_n_leg import (
    ActionPayout,
    ArbitrageProblem,
    OracleBudget,
    TerminalAtom,
    canonical_payload,
    fingerprint,
)
from open_trader.prediction_runtime_graph import RuntimeRelationGraph
from open_trader.prediction_snapshot_scheduler import (
    ComponentSnapshot,
    LegBook,
    SnapshotLeg,
    SnapshotScheduler,
)
from open_trader.prediction_solver import BenchmarkLimits
from open_trader.prediction_solver_verified import (
    CANDIDATE_EVIDENCE_SCHEMA_V1,
    PROOF_REQUEST_SCHEMA_V1,
    CandidateEvidence,
    ProofInput,
    model_fingerprint,
    quote_fingerprint,
    solver_evidence_from_payload,
    verification_result_from_payload,
    verify,
)
from open_trader.prediction_solver_worker import WorkerOutcome, WorkerRequest


logger = logging.getLogger(__name__)

LIVE_BUDGET = OracleBudget(
    max_quantity_vectors=9, max_joint_states=2, max_support_rechecks=1
)
LIVE_LIMITS = BenchmarkLimits(
    soft_time_limit_ms=1_000,
    hard_time_limit_ms=2_000,
    memory_limit_bytes=1 << 30,
    max_constraint_generation_rounds=3,
)
USD_UNITS_PER_DOLLAR = 1_000_000


def normalize_problem(problem: ArbitrageProblem) -> ArbitrageProblem:
    """Normalize one compiled problem to integer micro-USDC units."""
    actions = tuple(
        replace(
            action,
            settlement_asset_id="usd-micro",
            valuation_unit_id="usd-micro",
            asset_valuation_rule_id="usd-micro-v1",
        )
        for action in problem.actions
    )
    states = tuple(
        replace(
            state,
            atoms=tuple(_normalize_atom(atom) for atom in state.atoms),
        )
        for state in problem.terminal_state_sets
    )
    return replace(
        problem,
        valuation_unit_id="usd-micro",
        actions=actions,
        terminal_state_sets=states,
    )


def _normalize_atom(atom: TerminalAtom) -> TerminalAtom:
    payouts = tuple(
        ActionPayout(
            payout.action_id,
            (
                0
                if payout.payout_lower_bound_per_lot_units == 0
                else (
                    USD_UNITS_PER_DOLLAR
                    if payout.payout_lower_bound_per_lot_units == 1
                    else payout.payout_lower_bound_per_lot_units
                )
            ),
        )
        for payout in atom.payouts
    )
    return replace(atom, payouts=payouts)


class _OutcomeTrackingServer:
    """Forward scheduler submits and expose only completed worker outcomes."""

    def __init__(self, solver_server: object) -> None:
        self._server = solver_server
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[WorkerRequest, Future[WorkerOutcome]]] = {}
        self._ready: list[tuple[WorkerRequest, WorkerOutcome]] = []
        self._closed = False

    def submit(self, request: WorkerRequest) -> Future[WorkerOutcome]:
        with self._lock:
            if self._closed:
                raise RuntimeError("outcome tracking server is closed")
            future = self._server.submit(request)
            self._pending[request.request_id] = (request, future)
        future.add_done_callback(lambda completed: self._record(request, completed))
        return future

    def consume_ready(self) -> list[tuple[WorkerRequest, WorkerOutcome]]:
        with self._lock:
            ready = self._ready
            self._ready = []
            return ready

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending.clear()
            self._ready.clear()

    def _record(
        self, request: WorkerRequest, completed: Future[WorkerOutcome]
    ) -> None:
        try:
            outcome = completed.result()
        except BaseException:
            outcome = None
        with self._lock:
            self._pending.pop(request.request_id, None)
            if outcome is not None:
                self._ready.append((request, outcome))


class PredictionLiveResolver:
    """Own the graph, scheduler, and solution map for one live N-leg loop."""

    def __init__(
        self,
        *,
        data_dir: str | Path,
        relation_catalog: object,
        monitor: object,
        solver_server: object,
        selection_store: MonitorSelectionStore,
        store: object,
        execution: object,
        poll_interval: float = 0.25,
        account_freshness_seconds: float = 60.0,
        code_version: str = "issue-52",
    ) -> None:
        if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if (
            isinstance(account_freshness_seconds, bool)
            or not isinstance(account_freshness_seconds, (int, float))
            or account_freshness_seconds <= 0
        ):
            raise ValueError("account_freshness_seconds must be positive")
        self._data_dir = Path(data_dir)
        self._relation_catalog = relation_catalog
        self._monitor = monitor
        self._execution = execution
        self._store = store
        self._selection_store = selection_store
        self._code_version = str(code_version)
        self._poll_interval = float(poll_interval)
        self._account_freshness = timedelta(seconds=account_freshness_seconds)
        self._tracking = _OutcomeTrackingServer(solver_server)
        self._graph = RuntimeRelationGraph(
            generation_source=relation_catalog.current_generation,
            data_dir=self._data_dir,
            generation_meta_source=relation_catalog.generation_meta,
            code_version=self._code_version,
        )
        self._scheduler = SnapshotScheduler(
            self._tracking,
            snapshot_for=self._snapshot_for,
            build_solve_request=self._build_solve_request,
        )
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._problem_map: dict[str, ArbitrageProblem] = {}
        self._selection: dict[str, SelectedComponent] = {}
        self._solutions: dict[
            str, tuple[MarketSolution, ExecutionSolution | None]
        ] = {}
        self._request_components: dict[str, str] = {}
        self._applied_generation: tuple[int, str] | None = None
        self._account_view_cache: AccountView | None = None
        self._account_view_cached_at: datetime | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop_event.clear()
            self._reconcile()
            self._thread = threading.Thread(
                target=self._loop,
                name="prediction-live-resolver",
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
            self._scheduler.close()
            self._tracking.close()

    def solutions(self) -> list[dict[str, object]]:
        with self._lock:
            selection = dict(self._selection)
            solutions = dict(self._solutions)
        ordered = sorted(
            solutions,
            key=lambda component_id: (
                -(
                    selection[component_id].admission_score
                    if component_id in selection
                    else 0
                ),
                component_id,
            ),
        )
        return [
            {
                "component_id": component_id,
                "market": canonical_payload(market),
                "execution": (
                    canonical_payload(execution)
                    if execution is not None
                    else None
                ),
            }
            for component_id in ordered
            for market, execution in (solutions[component_id],)
        ]

    def _loop(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            try:
                self._tick()
            except Exception:
                logger.exception("prediction_live_resolver tick failed")

    def _tick(self) -> None:
        generation = self._graph.refresh()
        key = (
            int(generation.get("generation", 0)),
            str(generation.get("fingerprint", "")),
        )
        if key != self._applied_generation:
            self._reconcile()
            self._applied_generation = key
        self._scheduler.refresh(tuple(self._selection.values()))
        for request, outcome in self._tracking.consume_ready():
            self._handle_outcome(request, outcome)

    def _reconcile(self) -> None:
        rows = dict(self._relation_catalog.current_generation())
        problem, components = relation_generation_problem(rows)
        problem_map: dict[str, ArbitrageProblem] = {}
        raw_problems: dict[str, ArbitrageProblem] = {}
        for component in components:
            raw = problem_for_component(problem, component)
            raw_problems[component.component_id] = raw
            problem_map[component.component_id] = normalize_problem(raw)
        _, persisted = self._selection_store.load()
        kept = {
            component_id: selected
            for component_id, selected in persisted.items()
            if (
                component_id in raw_problems
                and fingerprint(
                    {"constraint_model": raw_problems[component_id].constraint_model}
                )
                == selected.relation_fingerprint
                and fingerprint(
                    {
                        "terminal_state_sets": raw_problems[
                            component_id
                        ].terminal_state_sets
                    }
                )
                == selected.terminal_fingerprint
            )
        }
        with self._lock:
            self._problem_map = problem_map
            self._selection = kept
            self._solutions = {
                component_id: solution
                for component_id, solution in self._solutions.items()
                if component_id in kept
            }
        if set(kept) != set(persisted):
            self._selection_store.save(kept)

    def _snapshot_for(self, selected: SelectedComponent) -> ComponentSnapshot | None:
        problem = self._problem_map.get(selected.component_id)
        if problem is None:
            return None
        legs: list[SnapshotLeg] = []
        for action in problem.actions:
            if action.venue_id != "polymarket":
                return None
            token = action.market_contract_id
            book = self._monitor.cross_venue_books((token,)).get(token)
            if book is None:
                return None
            meta = self._monitor.cross_venue_book_meta(token)
            legs.append(
                SnapshotLeg(
                    leg_id=action.action_id,
                    book=LegBook(
                        bids=tuple(book.bids),
                        asks=tuple(book.asks),
                        taker_fee_bps=Decimal("0"),
                        available=True,
                    ),
                    received_at=book.confirmed_at,
                    exchange_time=meta.get("exchange_time"),
                    sequence=meta.get("sequence"),
                )
            )
        if not legs:
            return None
        return ComponentSnapshot(selected.component_id, tuple(legs))

    def _build_solve_request(
        self, selected: SelectedComponent, snapshot: ComponentSnapshot
    ) -> WorkerRequest:
        problem = self._problem_map.get(selected.component_id)
        if problem is None:
            raise ValueError(f"no live problem for component {selected.component_id}")
        # ponytail: fee/haircut/tick stay zero; real fee/tick policy is #74/#85.
        request = build_solve_request(
            problem,
            snapshot,
            budget=LIVE_BUDGET,
            limits=LIVE_LIMITS,
            price_units_per_quote_unit=USD_UNITS_PER_DOLLAR,
        )
        with self._lock:
            self._request_components[request.request_id] = selected.component_id
        return request

    def _handle_outcome(
        self, request: WorkerRequest, outcome: WorkerOutcome
    ) -> None:
        component_id = self._request_components.pop(request.request_id, None)
        if component_id is None:
            return
        problem = request.request.problem
        if (
            outcome.status != "OK"
            or not outcome.cleanup_proven
            or outcome.response is None
            or outcome.response.evidence is None
        ):
            self._drop_solution(component_id)
            return
        try:
            solver_evidence = solver_evidence_from_payload(outcome.response.evidence)
            if solver_evidence.candidate is None:
                self._drop_solution(component_id)
                return
            proof_input = ProofInput(
                PROOF_REQUEST_SCHEMA_V1,
                request.request,
                request.limits,
                quote_fingerprint(problem),
                int(self._graph.current_generation().get("generation", 0)),
                self._code_version,
            )
            evidence = CandidateEvidence(
                CANDIDATE_EVIDENCE_SCHEMA_V1,
                proof_input,
                "cp_sat",
                outcome.solver_version or "unavailable",
                model_fingerprint(problem),
                fingerprint({"quantities": solver_evidence.candidate.quantities}),
                solver_evidence,
            )
            verification = verification_result_from_payload(
                verify(canonical_payload(evidence)), source=evidence
            )
            market = market_solution_from_verification(
                component_id, problem, evidence, verification
            )
        except (TypeError, ValueError, OverflowError):
            market = None
        if market is None:
            self._drop_solution(component_id)
            return
        execution = self._execution_solution(component_id, market, problem)
        with self._lock:
            self._solutions[component_id] = (market, execution)

    def _execution_solution(
        self,
        component_id: str,
        market: MarketSolution,
        problem: ArbitrageProblem,
    ) -> ExecutionSolution | None:
        account = self._account_view()
        if account is None:
            return None
        try:
            max_unsettled = int(
                (self._store.n_leg_safety_config_latest() or {})
                .get("config", {})
                .get("max_total_unsettled_capital_units", 0)
            )
            return execution_solution_from_market(
                market,
                problem,
                account,
                max_total_unsettled_capital=max_unsettled,
            )
        except (TypeError, ValueError, RuntimeError):
            return None

    def _account_view(self) -> AccountView | None:
        now = datetime.now(UTC)
        with self._lock:
            cached = self._account_view_cache
            cached_at = self._account_view_cached_at
            if (
                cached is not None
                and cached_at is not None
                and now - cached_at <= self._account_freshness
            ):
                return cached
            view: AccountView | None = None
            fetch = getattr(self._execution, "n_leg_account_view", None)
            if callable(fetch):
                raw = fetch()
                if isinstance(raw, AccountView):
                    try:
                        unsettled = int(
                            self._store.n_leg_control().get(
                                "total_unsettled_capital_units", 0
                            )
                        )
                    except (TypeError, ValueError, RuntimeError):
                        unsettled = 0
                    view = replace(raw, unsettled_capital_units=unsettled)
            self._account_view_cache = view
            self._account_view_cached_at = now
            return view

    def _drop_solution(self, component_id: str) -> None:
        with self._lock:
            self._solutions.pop(component_id, None)


__all__ = [
    "LIVE_BUDGET",
    "LIVE_LIMITS",
    "USD_UNITS_PER_DOLLAR",
    "PredictionLiveResolver",
    "normalize_problem",
]
