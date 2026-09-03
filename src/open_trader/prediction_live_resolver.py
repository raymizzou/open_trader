"""Issue #52: live N-leg resolver for the production Prediction runtime.

The resolver owns the #82 runtime relation graph, the #83 latest-snapshot-wins
scheduler, and the #84 Market/ExecutionSolution interpretation. One daemon
thread advances the catalog generation, prunes the persisted #77 selected
monitor set to components whose structure still matches the recompiled model,
dispatches live Polymarket books to the bounded solver server, and turns
completed worker evidence into a verified MarketSolution without any second
solver pass.

Scope: no discovery/selection, no orders, no ORDER_READY, and Predict.fun
legs fail closed. The synchronous #74 partial-fill proof runs on the
execution hot path and its records are cached and persisted. The solver
server is injected and never closed here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3

from open_trader.prediction_market_solution import (
    EXECUTABLE_REASON,
    AccountView,
    ComponentResolution,
    ExecutionSolution,
    MarketSolution,
    build_solve_request,
    execution_solution_from_market,
    resolution_from_verification,
)
from open_trader.prediction_monitor_selection import (
    MonitorSelectionStore,
    SelectedComponent,
    problem_for_component,
    relation_generation_problem,
)
from open_trader.prediction_n_leg import (
    ActionPayout,
    ActionSide,
    ArbitrageProblem,
    OracleBudget,
    TerminalAtom,
    canonical_payload,
    fingerprint,
)
from open_trader.prediction_n_leg_episodes import (
    EpisodeTracker,
)
from open_trader.prediction_n_leg_execution import (
    PartialFillProofRecord,
    partial_fill_proof_from_payload,
)
from open_trader.prediction_n_leg_mode import DEFAULT_SAFETY_CONFIG
from open_trader.prediction_n_leg_read_model import (
    FEE_STATE_CHARGING,
    FEE_STATE_FREE,
    FEE_STATE_UNKNOWN,
    would_submit_predicate,
)
from open_trader.prediction_partial_fill import (
    PARTIAL_FILL_UNKNOWN,
    fill_adversary_problem_from_market_solution,
    prove_partial_fill,
)
from open_trader.prediction_runtime_graph import RuntimeRelationGraph
from open_trader.prediction_snapshot_scheduler import (
    ComponentSnapshot,
    LegBook,
    SnapshotLeg,
    SnapshotScheduler,
    order_ready,
)
from open_trader.prediction_solver import BenchmarkLimits
from open_trader.prediction_solver_verified import (
    CANDIDATE_EVIDENCE_SCHEMA_V1,
    PROOF_REQUEST_SCHEMA_V1,
    CandidateEvidence,
    ProofInput,
    VerificationResult,
    VerificationStatus,
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
# #74: independent hard wall-clock bound for the synchronous partial-fill
# proof; a timeout yields a cached UNKNOWN proof for the same snapshot
# fingerprint and never retries within this process.
LIVE_PROOF_TIME_LIMIT_MS = 1_000
USD_UNITS_PER_DOLLAR = 1_000_000
# #83/#106: shared snapshot freshness for scheduler qualification and the
# episode tick-level quote check.
SNAPSHOT_FRESHNESS = timedelta(seconds=30)


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
    payouts: list[ActionPayout] = []
    for payout in atom.payouts:
        value = payout.payout_lower_bound_per_lot_units
        if value == 0:
            scaled = 0
        elif value == 1:
            scaled = USD_UNITS_PER_DOLLAR
        elif value > 0 and value % (USD_UNITS_PER_DOLLAR // 2) == 0:
            # Supported micro-USDC scales: 0, 500_000 (half dollar), and any
            # whole-dollar multiple. Anything else is an unknown payout scale.
            scaled = value
        else:
            raise ValueError(f"unsupported payout scale: {value}")
        payouts.append(ActionPayout(payout.action_id, scaled))
    return replace(atom, payouts=tuple(payouts))


# Issue #112: per-contract fee states aggregated from the catalog generation
# endpoints (state vocabulary lives in prediction_n_leg_read_model, which also
# owns the fail-closed gate). Anything that is not a proven fee-free market --
# a charging market, a missing/unparseable fee fact, or conflicting facts for
# one contract -- fails closed; the fee gate itself lives in the qualification
# layer, not in the solver cost slice.


def _fee_rate_value(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        rate = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return rate if rate.is_finite() else None


def _endpoint_fee_state(endpoint: object) -> str:
    """Fee state of one catalog endpoint; unknown unless proven fee-free."""
    if not isinstance(endpoint, Mapping):
        return FEE_STATE_UNKNOWN
    fees_enabled = endpoint.get("fees_enabled")
    if fees_enabled is True:
        return FEE_STATE_CHARGING
    if fees_enabled is not False:
        return FEE_STATE_UNKNOWN
    rate_raw = endpoint.get("fee_rate")
    if rate_raw is None:
        return FEE_STATE_FREE
    rate = _fee_rate_value(rate_raw)
    if rate is None:
        return FEE_STATE_UNKNOWN
    if rate > 0:
        return FEE_STATE_CHARGING
    if rate == 0:
        return FEE_STATE_FREE
    return FEE_STATE_UNKNOWN


def _fee_state_by_contract(
    rows: Mapping[str, Mapping[str, object]],
) -> dict[str, str]:
    """Contract -> fee state over one generation batch, conflicting -> unknown."""
    states: dict[str, str] = {}
    for row in rows.values():
        endpoints = row.get("endpoints")
        if not isinstance(endpoints, Sequence):
            continue
        for endpoint in endpoints:
            if not isinstance(endpoint, Mapping):
                continue
            contract_id = str(endpoint.get("contract_id") or "")
            if not contract_id:
                continue
            state = _endpoint_fee_state(endpoint)
            existing = states.setdefault(contract_id, state)
            if existing != state:
                states[contract_id] = FEE_STATE_UNKNOWN
    return states


# Issue #114: per-contract YES/NO CLOB token maps aggregated from the catalog
# generation endpoints.  The live CLOB channel (order books, subscription,
# orders) is keyed by clob token id while threshold/negRisk relations key
# their actions by condition id, so the resolver resolves each action's book
# read by its direction.  Contract -> {"yes_token_id": ..., "no_token_id": ...}
# over one generation batch; conflicting facts for one contract drop the
# mapping (the #112 fee-unknown pattern), never guess a direction.


def _leg_token_by_contract(
    rows: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, str]]:
    """Contract -> YES/NO token pair over one generation batch, conflicting -> dropped."""
    tokens: dict[str, dict[str, str]] = {}
    for row in rows.values():
        endpoints = row.get("endpoints")
        if not isinstance(endpoints, Sequence):
            continue
        for endpoint in endpoints:
            if not isinstance(endpoint, Mapping):
                continue
            contract_id = str(endpoint.get("contract_id") or "")
            if not contract_id:
                continue
            entry = {
                name: str(endpoint[name])
                for name in ("yes_token_id", "no_token_id")
                if isinstance(endpoint.get(name), str) and str(endpoint[name]).strip()
            }
            if not entry:
                continue
            existing = tokens.get(contract_id)
            if existing is None:
                tokens[contract_id] = entry
            elif existing != entry:
                tokens[contract_id] = {}
    return tokens


def resolve_leg_token(action: object, leg_token_map: Mapping[str, Mapping[str, object]]) -> str:
    """The CLOB token to read for one action: the direction token when the
    contract is mapped, else the market_contract_id itself (the mechanical
    contract-is-token invariant; a legacy IMPLIES condition id then finds no
    book and the snapshot fails closed)."""
    contract_id = str(getattr(action, "market_contract_id"))
    entry = leg_token_map.get(contract_id) if isinstance(leg_token_map, Mapping) else None
    if isinstance(entry, Mapping):
        name = "yes_token_id" if getattr(action, "side") == ActionSide.BUY_YES else "no_token_id"
        token = entry.get(name)
        if isinstance(token, str) and token.strip():
            return token
    return contract_id


def _fee_block(
    selected: SelectedComponent | None,
    fee_by_contract: Mapping[str, str],
) -> dict[str, object]:
    """Per-solution fee summary: the worst state over the component contracts.

    A contract missing from the generation map (or a component with no
    contracts at all) counts as unknown, never as fee-free.
    """
    contracts = sorted(selected.contract_ids) if selected is not None else []
    charging: list[str] = []
    unknown: list[str] = []
    for contract_id in contracts:
        state = fee_by_contract.get(contract_id, FEE_STATE_UNKNOWN)
        if state == FEE_STATE_CHARGING:
            charging.append(contract_id)
        elif state != FEE_STATE_FREE:
            unknown.append(contract_id)
    if unknown:
        status: str = FEE_STATE_UNKNOWN
    elif charging:
        status = FEE_STATE_CHARGING
    elif contracts:
        status = FEE_STATE_FREE
    else:
        status = FEE_STATE_UNKNOWN
    return {
        "status": status,
        "charging_contracts": charging,
        "unknown_contracts": unknown,
    }


class _OutcomeTrackingServer:
    """Forward scheduler submits and expose only completed worker outcomes."""

    def __init__(self, solver_server: object) -> None:
        self._server = solver_server
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[WorkerRequest, Future[WorkerOutcome]]] = {}
        self._ready: list[tuple[WorkerRequest, WorkerOutcome | None]] = []
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

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

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
        selection_lock: threading.RLock | None = None,
        store: object,
        execution: object,
        poll_interval: float = 0.25,
        account_freshness_seconds: float = 60.0,
        code_version: str = "issue-52",
        # Seam: the issue-71 validation harness must exercise this chain with
        # its N>=3 budget (8 joint states) instead of the 2-leg LIVE_BUDGET.
        budget: OracleBudget = LIVE_BUDGET,
        limits: BenchmarkLimits = LIVE_LIMITS,
        proof_time_limit_ms: int = LIVE_PROOF_TIME_LIMIT_MS,
        # #106: optional opportunity-episode tracker (pure state machine) and
        # an injectable clock so tests can drive episode timing.
        episode_tracker: EpisodeTracker | None = None,
        now_fn: Callable[[], datetime] | None = None,
        # Issue #114: optional injected contract -> YES/NO token pairs.  The
        # map extracted from the catalog generation rows always wins on a
        # conflicting contract; the injection only fills gaps (legacy rows).
        leg_token_map: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if (
            isinstance(account_freshness_seconds, bool)
            or not isinstance(account_freshness_seconds, (int, float))
            or account_freshness_seconds <= 0
        ):
            raise ValueError("account_freshness_seconds must be positive")
        if not isinstance(budget, OracleBudget):
            raise ValueError("budget must be an OracleBudget")
        if not isinstance(limits, BenchmarkLimits):
            raise ValueError("limits must be BenchmarkLimits")
        if type(proof_time_limit_ms) is not int or proof_time_limit_ms <= 0:
            raise ValueError("proof_time_limit_ms must be a positive integer")
        self._data_dir = Path(data_dir)
        self._relation_catalog = relation_catalog
        self._monitor = monitor
        self._execution = execution
        self._store = store
        self._selection_store = selection_store
        self._selection_lock = selection_lock or threading.RLock()
        self._code_version = str(code_version)
        self._poll_interval = float(poll_interval)
        self._account_freshness = timedelta(seconds=account_freshness_seconds)
        self._budget = budget
        self._limits = limits
        self._proof_time_limit_ms = proof_time_limit_ms
        self._episode_tracker = episode_tracker
        self._now_fn: Callable[[], datetime] = now_fn or (
            lambda: datetime.now(UTC)
        )
        self._lineage_by_component: dict[str, str] = {}
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
            freshness=SNAPSHOT_FRESHNESS,
        )
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._problem_map: dict[str, ArbitrageProblem] = {}
        # Issue #112: contract -> fee state from the last reconciled batch.
        self._fee_by_contract: dict[str, str] = {}
        # Issue #114: contract -> {"yes_token_id", "no_token_id"} used to key
        # book reads by action direction (generation rows win over injection).
        self._injected_leg_tokens: dict[str, dict[str, object]] = {
            str(contract): dict(entry)
            for contract, entry in (leg_token_map or {}).items()
            if isinstance(entry, Mapping)
        }
        self._leg_tokens: dict[str, dict[str, object]] = {}
        self._selection: dict[str, SelectedComponent] = {}
        self._solutions: dict[
            str, tuple[MarketSolution, ExecutionSolution | None]
        ] = {}
        self._resolutions: dict[str, ComponentResolution] = {}
        self._verifications: dict[str, VerificationResult] = {}
        self._request_components: dict[str, tuple[str, str]] = {}
        # #74: synchronous partial-fill proofs, keyed by the stable adversary
        # fingerprint (fixed execution solution + cap config). UNKNOWN results
        # are cached too, so a timed-out snapshot fingerprint is never retried.
        self._fill_proofs: dict[
            str, tuple[PartialFillProofRecord, dict[str, object] | None]
        ] = {}
        self._fill_proof_components: dict[str, str] = {}
        self._applied_generation: tuple[int, str] | None = None
        self._account_view_cache: AccountView | None = None
        self._account_view_cached_at: datetime | None = None
        # #106: qualification policy version for episode records, cached at
        # the same cadence as the controls-reading account view so episode
        # reporting adds no per-tick store query.
        self._policy_version_cache: tuple[str | None, datetime] | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop_event.clear()
            try:
                self._reconcile()
            except Exception:
                # #74 hotfix: a conflicting catalog generation (e.g. the same
                # action compiled under two inconsistent relations) must not
                # take the whole prediction service down at startup. The
                # live tick keeps retrying reconcile whenever the generation
                # changes and self-heals once the conflict is resolved.
                logger.exception(
                    "prediction_live_resolver startup reconcile failed; "
                    "the live tick will retry on the next generation change"
                )
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

    def is_idle(self) -> bool:
        return self._tracking.pending_count() == 0

    def solutions(self) -> list[dict[str, object]]:
        with self._lock:
            selection = dict(self._selection)
            solutions = dict(self._solutions)
            fee_by_contract = dict(self._fee_by_contract)
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
                "fee": _fee_block(
                    selection.get(component_id), fee_by_contract
                ),
            }
            for component_id in ordered
            for market, execution in (solutions[component_id],)
        ]

    def latest_resolution(self, component_id: str) -> ComponentResolution | None:
        with self._lock:
            return self._resolutions.get(component_id)

    def latest_verification(self, component_id: str) -> VerificationResult | None:
        with self._lock:
            return self._verifications.get(component_id)

    def latest_execution(self, component_id: str) -> ExecutionSolution | None:
        with self._lock:
            entry = self._solutions.get(component_id)
            return entry[1] if entry is not None else None

    def latest_partial_fill_proof(
        self, component_id: str
    ) -> Mapping[str, object] | None:
        with self._lock:
            proof_fingerprint = self._fill_proof_components.get(component_id)
            if proof_fingerprint is None:
                return None
            cached = self._fill_proofs.get(proof_fingerprint)
            return cached[0].to_payload() if cached is not None else None

    def n_leg_episodes(self) -> dict[str, dict[str, object]]:
        """#106 read seam: episode projections keyed by component id."""
        tracker = self._episode_tracker
        return {} if tracker is None else tracker.project()

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
        self._observe_open_episodes()

    def _observe_open_episodes(self) -> None:
        """#106 tick pass over open episodes: quote freshness plus the
        shared would-submit predicate on the current solution state."""
        tracker = self._episode_tracker
        if tracker is None:
            return
        for component_id in tracker.open_component_ids():
            now = self._now_fn()
            if not self._episode_quote_fresh(component_id, now):
                tracker.mark_quote_stale(component_id, now=now)
            with self._lock:
                entry = self._solutions.get(component_id)
                verification = self._verifications.get(component_id)
            execution = None if entry is None else entry[1]
            tracker.observe_would_submit(
                component_id,
                would_submit=would_submit_predicate(
                    None if execution is None else execution.reason,
                    None if verification is None else str(verification.status),
                ),
                now=now,
            )

    def _reconcile(self) -> None:
        rows = dict(self._relation_catalog.current_generation())
        problem, components = relation_generation_problem(rows)
        problem_map: dict[str, ArbitrageProblem] = {}
        raw_problems: dict[str, ArbitrageProblem] = {}
        # #106: map oracle component ids ("component:<contract>:...") onto the
        # runtime graph's episode lineage so episode rows carry real lineage.
        lineage_map: dict[str, str] = {}
        for component in self._graph.components().values():
            raw_contracts = sorted(
                contract.split(":", 1)[1] if ":" in contract else contract
                for contract in component.contract_ids
            )
            lineage_map[f"component:{':'.join(raw_contracts)}"] = (
                component.lineage_id
            )
        self._lineage_by_component = lineage_map
        # Issue #112: the fee map is built from the same generation batch as
        # the problem compilation and stored under the solutions lock.
        fee_by_contract = _fee_state_by_contract(rows)
        # Issue #114: the direction token map comes from the same batch; the
        # injected map only fills gaps because generation rows win per key.
        leg_tokens: dict[str, dict[str, object]] = {
            **self._injected_leg_tokens,
            **_leg_token_by_contract(rows),
        }
        for component in components:
            raw = problem_for_component(problem, component)
            raw_problems[component.component_id] = raw
            problem_map[component.component_id] = normalize_problem(raw)
        with self._selection_lock:
            _, persisted = self._selection_store.load()
            kept = {
                component_id: selected
                for component_id, selected in persisted.items()
                if (
                    component_id in raw_problems
                    and fingerprint(
                        {
                            "constraint_model": raw_problems[
                                component_id
                            ].constraint_model
                        }
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
            # #106: pruned components retire their open episode immediately
            # with its own close cause.
            if self._episode_tracker is not None:
                now = self._now_fn()
                for component_id in sorted(set(persisted) - set(kept)):
                    self._episode_tracker.component_retired(
                        component_id, now=now
                    )
            with self._lock:
                self._fee_by_contract = fee_by_contract
                self._leg_tokens = leg_tokens
            self._problem_map = problem_map
            self._selection = kept
            self._solutions = {
                component_id: solution
                for component_id, solution in self._solutions.items()
                if component_id in kept
            }
            self._resolutions = {
                component_id: resolution
                for component_id, resolution in self._resolutions.items()
                if component_id in kept
            }
            self._verifications = {
                component_id: verification
                for component_id, verification in self._verifications.items()
                if component_id in kept
            }
            self._request_components = {
                request_id: entry
                for request_id, entry in self._request_components.items()
                if entry[0] in kept
            }
            self._fill_proof_components = {
                component_id: proof_fingerprint
                for component_id, proof_fingerprint in (
                    self._fill_proof_components.items()
                )
                if component_id in kept
            }
            referenced = set(self._fill_proof_components.values())
            self._fill_proofs = {
                proof_fingerprint: cached
                for proof_fingerprint, cached in self._fill_proofs.items()
                if proof_fingerprint in referenced
            }
            if set(kept) != set(persisted):
                self._selection_store.save(kept)
        # Issue #114 (P1 fix): the resolver owns the N-leg-dedicated share of
        # its monitor's cross-venue subscription (`set_n_leg_tokens`) and
        # re-seeds it from the full resolved token set at the end of every
        # reconcile.  In production the resolver and the cross-venue engine
        # share one monitor instance, so this share runs in parallel with the
        # engine's `set_cross_venue_tokens` share: the two take effect as a
        # union at every consumer and neither writer ever evicts the other.
        # Monitors without the seam (validation adapters) are skipped; an
        # empty generation correctly clears only this share, never the
        # engine's.  Set changes still trigger the monitor's own REST
        # snapshot + resubscribe.
        if hasattr(self._monitor, "set_n_leg_tokens"):
            resolved = {
                resolve_leg_token(action, leg_tokens)
                for problem in problem_map.values()
                for action in problem.actions
            }
            self._monitor.set_n_leg_tokens(sorted(resolved))

    def _snapshot_for(self, selected: SelectedComponent) -> ComponentSnapshot | None:
        problem = self._problem_map.get(selected.component_id)
        if problem is None:
            return None
        leg_tokens = self._leg_tokens
        legs: list[SnapshotLeg] = []
        for action in problem.actions:
            if action.venue_id != "polymarket":
                return None
            token = resolve_leg_token(action, leg_tokens)
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
        # ponytail: fee stays 0 in the cost slice (#117 owns fee-aware
        # economics); since #112 charging/unknown fees are gated at the
        # qualification layer, and tick/haircut policy remains #74/#85.
        request = build_solve_request(
            problem,
            snapshot,
            budget=self._budget,
            limits=self._limits,
            price_units_per_quote_unit=USD_UNITS_PER_DOLLAR,
        )
        with self._lock:
            self._request_components[request.request_id] = (
                selected.component_id,
                model_fingerprint(problem),
            )
        return request

    def _handle_outcome(
        self, request: WorkerRequest, outcome: WorkerOutcome | None
    ) -> None:
        entry = self._request_components.pop(request.request_id, None)
        if entry is None:
            return
        component_id, dispatched_fingerprint = entry
        problem = request.request.problem
        if (
            outcome is None
            or outcome.status != "OK"
            or not outcome.cleanup_proven
            or outcome.response is None
            or outcome.response.evidence is None
        ):
            self._drop_solution(component_id)
            return
        with self._lock:
            current_problem = self._problem_map.get(component_id)
            selected = component_id in self._selection
        if not selected or current_problem is None:
            self._drop_solution(component_id)
            return
        if dispatched_fingerprint != model_fingerprint(current_problem):
            self._drop_solution(component_id)
            return
        verification = None
        try:
            solver_evidence = solver_evidence_from_payload(outcome.response.evidence)
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
                (
                    None
                    if solver_evidence.candidate is None
                    else fingerprint(
                        {"quantities": solver_evidence.candidate.quantities}
                    )
                ),
                solver_evidence,
            )
            if solver_evidence.candidate is None:
                verification = verification_result_from_payload(
                    verify(canonical_payload(proof_input)), source=proof_input
                )
            else:
                verification = verification_result_from_payload(
                    verify(canonical_payload(evidence)), source=evidence
                )
            resolution = resolution_from_verification(
                component_id, problem, evidence, verification,
                code_version=self._code_version,
            )
        except (TypeError, ValueError, OverflowError):
            resolution = None
        if resolution is None:
            self._drop_solution(component_id)
            return
        with self._lock:
            self._resolutions[component_id] = resolution
            self._verifications[component_id] = verification
        market = resolution.market_solution
        if market is None:
            with self._lock:
                self._solutions.pop(component_id, None)
            # #106: negative/unknown outcomes carry no market solution but
            # still drive the episode state machine.
            self._report_episode_outcome(component_id, resolution, verification)
            return
        execution = self._execution_solution(component_id, market, problem)
        with self._lock:
            self._solutions[component_id] = (market, execution)
        self._report_episode_outcome(
            component_id, resolution, verification, execution=execution
        )

    def _episode_quote_fresh(self, component_id: str, now: datetime) -> bool:
        """#106 freshness gate: the component's current book must be fresh."""
        selected = self._selection.get(component_id)
        if selected is None:
            return False
        snapshot = self._snapshot_for(selected)
        if snapshot is None:
            return False
        return order_ready(
            snapshot, now=now, freshness=SNAPSHOT_FRESHNESS
        )

    def _episode_gap_seconds(self) -> float:
        """The caller-supplied rearm gap, read from the safety config."""
        try:
            latest = self._store.n_leg_safety_config_latest() or {}
            config = latest.get("config") or {}
            return float(
                config.get(
                    "episode_rearm_gap_seconds",
                    DEFAULT_SAFETY_CONFIG["episode_rearm_gap_seconds"],
                )
            )
        except (TypeError, ValueError, RuntimeError, AttributeError):
            return float(DEFAULT_SAFETY_CONFIG["episode_rearm_gap_seconds"])

    def _episode_policy_version(self) -> str | None:
        """#106 qualification policy version, cached at controls cadence.

        Reads the ``qualification_policy_version`` of the same ``n_leg_control``
        row the hot path already consults (the account view), refreshed at the
        account-freshness cadence instead of once per episode event.
        """
        now = self._now_fn()
        with self._lock:
            cached = self._policy_version_cache
            if cached is not None and now - cached[1] <= self._account_freshness:
                return cached[0]
        try:
            raw = self._store.n_leg_control().get("qualification_policy_version")
        except (TypeError, ValueError, RuntimeError, AttributeError):
            return None
        version = None if raw is None else str(raw)
        with self._lock:
            self._policy_version_cache = (version, now)
        return version

    def _report_episode_outcome(
        self,
        component_id: str,
        resolution: ComponentResolution,
        verification: VerificationResult,
        *,
        execution: ExecutionSolution | None = None,
    ) -> None:
        """Map one verification outcome onto the #106 episode tracker.

        The tracker stays a pure state machine: this adapter turns the
        VerificationResult plus current quote freshness into tracker events.
        """
        tracker = self._episode_tracker
        if tracker is None:
            return
        now = self._now_fn()
        status = resolution.status
        if status is VerificationStatus.NO_QUALIFIED_OPPORTUNITY:
            # resolution_from_verification only yields this status when
            # negative_proof_matches bound the proof exactly, so the tracker
            # acceptance check reduces to quote freshness.
            proof = verification.negative_proof
            tracker.observe_negative(
                component_id,
                proof_fingerprint=fingerprint(canonical_payload(proof)),
                generation=int(verification.current_generation),
                model_fingerprint=str(verification.model_fingerprint),
                quote_fingerprint=str(verification.quote_fingerprint),
                qualification_fingerprint=(
                    getattr(proof, "qualification_fingerprint", None)
                ),
                binding_matches=True,
                quote_fresh=self._episode_quote_fresh(component_id, now),
                gap_seconds=self._episode_gap_seconds(),
                now=now,
                qualification_policy_version=self._episode_policy_version(),
            )
            return
        if (
            status is VerificationStatus.UNKNOWN
            and resolution.reason == "NEGATIVE_PROOF_MISMATCH"
        ):
            # A negative arrived but binds to another model/quote/generation:
            # a reset event, never a no-arbitrage signal.
            tracker.observe_negative(
                component_id,
                proof_fingerprint="",
                generation=int(verification.current_generation),
                model_fingerprint=str(verification.model_fingerprint),
                quote_fingerprint=str(verification.quote_fingerprint),
                qualification_fingerprint=(
                    getattr(
                        getattr(verification, "negative_proof", None),
                        "qualification_fingerprint",
                        None,
                    )
                ),
                binding_matches=False,
                quote_fresh=self._episode_quote_fresh(component_id, now),
                gap_seconds=self._episode_gap_seconds(),
                now=now,
                qualification_policy_version=self._episode_policy_version(),
            )
            return
        if status is not VerificationStatus.QUALIFIED_VERIFIED:
            tracker.observe_unknown(component_id, now=now)
            return
        market = resolution.market_solution
        if market is None:
            return
        solution_proof = getattr(market, "payout_proof", None)
        fingerprints = {
            "component_generation": int(verification.current_generation),
            "model_fingerprint": str(verification.model_fingerprint),
            "quote_fingerprint": str(verification.quote_fingerprint),
            "qualification_fingerprint": (
                getattr(solution_proof, "qualification_fingerprint", None)
            ),
            "qualification_policy_version": self._episode_policy_version(),
        }
        profit = Decimal(market.guaranteed_profit_units).scaleb(-6)
        would_submit = would_submit_predicate(
            None if execution is None else execution.reason,
            str(status),
        )
        tracker.observe_qualified(
            component_id,
            self._lineage_by_component.get(component_id, component_id),
            profit,
            would_submit,
            None,
            fingerprints,
            self._now_fn(),
        )

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
            safety = self._store.n_leg_safety_config_latest() or {}
            config = safety.get("config") or {}
            max_unsettled = int(
                config.get("max_total_unsettled_capital_units", 0)
            )
            execution = execution_solution_from_market(
                market,
                problem,
                account,
                max_total_unsettled_capital=max_unsettled,
            )
        except (TypeError, ValueError, RuntimeError):
            return None
        if execution is None or execution.reason != EXECUTABLE_REASON:
            return execution
        # #74: synchronous partial-fill proof on the live hot path. Structural
        # failures (bad caps, unknown action, solver unavailable) fail closed
        # to UNKNOWN status; timed-out proofs are cached UNKNOWN records for
        # the same snapshot fingerprint and are never retried.
        try:
            record, _counterexample = self._prove_fixed(
                component_id,
                execution,
                market,
                problem,
                config,
                int(safety.get("version", 1)),
            )
            return replace(execution, partial_fill_proof=record.status)
        except (TypeError, ValueError, RuntimeError, OverflowError):
            return replace(
                execution, partial_fill_proof=PARTIAL_FILL_UNKNOWN
            )

    def _prove_fixed(
        self,
        component_id: str,
        execution: ExecutionSolution,
        market: MarketSolution,
        problem: ArbitrageProblem,
        config: Mapping[str, object],
        config_version: int,
    ) -> tuple[PartialFillProofRecord, dict[str, object] | None]:
        """Run (or replay) the #74 proof for this fixed solution, cache by
        adversary fingerprint, and persist best-effort to the store."""
        adversary = fill_adversary_problem_from_market_solution(
            execution,
            problem,
            cap_config_version=f"caps-v{config_version or 1}",
            max_partial_fill_loss=int(
                config.get("max_partial_fill_loss_units", 0)
            ),
            max_auto_repair_loss=int(
                config.get("max_auto_repair_loss_units", 0)
            ),
        )
        proof_fingerprint = adversary.fingerprint
        with self._lock:
            cached = self._fill_proofs.get(proof_fingerprint)
            if cached is not None:
                self._fill_proof_components[component_id] = proof_fingerprint
                return cached
        try:
            stored = self._store.partial_fill_proof(proof_fingerprint)
        except (TypeError, ValueError, RuntimeError, AttributeError, sqlite3.Error):
            stored = None
        if stored is not None:
            try:
                record = partial_fill_proof_from_payload(stored.get("proof"))
                counterexample = stored.get("unsafe_counterexample")
                with self._lock:
                    self._fill_proofs[proof_fingerprint] = (
                        record,
                        counterexample,
                    )
                    self._fill_proof_components[
                        component_id
                    ] = proof_fingerprint
                return record, counterexample
            except (TypeError, ValueError):
                pass
        record, counterexample = prove_partial_fill(
            adversary, time_limit_ms=self._proof_time_limit_ms
        )
        try:
            # Persist under the stable adversary fingerprint so a later
            # process can replay the proof before re-solving.
            self._store.partial_fill_proof_save(
                record,
                counterexample,
                proof_fingerprint=proof_fingerprint,
            )
        except (TypeError, ValueError, RuntimeError, AttributeError, sqlite3.Error):
            pass
        with self._lock:
            self._fill_proofs[proof_fingerprint] = (record, counterexample)
            self._fill_proof_components[component_id] = proof_fingerprint
        return record, counterexample

    def _account_view(self) -> AccountView | None:
        now = datetime.now(UTC)
        with self._lock:
            cached = self._account_view_cache
            cached_at = self._account_view_cached_at
            if (
                cached_at is not None
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
            self._resolutions.pop(component_id, None)
            self._verifications.pop(component_id, None)
            proof_fingerprint = self._fill_proof_components.pop(
                component_id, None
            )
            if proof_fingerprint is not None and proof_fingerprint not in (
                self._fill_proof_components.values()
            ):
                self._fill_proofs.pop(proof_fingerprint, None)


__all__ = [
    "LIVE_BUDGET",
    "LIVE_LIMITS",
    "LIVE_PROOF_TIME_LIMIT_MS",
    "USD_UNITS_PER_DOLLAR",
    "PredictionLiveResolver",
    "normalize_problem",
]
