"""Issue #77: relation generation -> canonical components -> background resolution.

Only ACTIVE and model-complete generation rows may enter monitoring selection
(#77 acceptance 1); UNKNOWN, candidate, unapproved or model-incomplete rows are
never admitted. Components are built by reusing the canonical component builder
``build_relation_components`` on one N-leg ``ArbitrageProblem`` compiled from
the admissible rows (#77 acceptance 2-3), so overlapping relations merge into a
single non-overlapping component set.

The compile seam is the deferred threshold-enrichment boundary: each COMPLETE
row must carry a compiled canonical problem payload under ``model["problem"]``
(``open_trader.prediction_n_leg.problem.v1``). Until enrichment produces such
rows, no row is admitted and this bridge returns the empty set, which is the
current production state.

Background resolution (#77 acceptance 4-6) consumes one compiled component and
fixes its ``initial_verified_profit`` to the #50 verifier's worst-payout-minus-
cost proof; the #51 fee/tick/slippage and safety margin are already baked into
each action's ``cost_slices``. ``OPTIMAL``/``NOT_PROVEN`` is recorded separately
from the verified profit, and the whole pass is gated by ``idle_capacity()`` so
it never competes with the #52 real-time window.

Selection and persistence (#77 acceptance 7-10): the ranked selected-monitor set
keeps at most 10 non-overlapping components (disjoint by construction, since
components are connected components of one merged problem) ordered by fixed
``initial_verified_profit``. Component identity, the fixed admission score, the
verified portfolio and its fingerprints are persisted in the shared prediction
SQLite; in-flight solves, pending snapshots and quote freshness are never
persisted here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from open_trader.prediction_n_leg import (
    PROBLEM_SCHEMA_V1,
    REQUEST_SCHEMA_V1,
    ActionQuantity,
    CandidateAction,
    ForbiddenAtomCombination,
    OptimalityStatus,
    OracleBudget,
    OracleRequest,
    PortfolioSolution,
    QualificationConstraint,
    RelationConstraint,
    SearchMode,
    TerminalStateSet,
    ArbitrageProblem,
    ConstraintModel,
    canonical_payload,
    fingerprint,
    problem_from_payload,
)
from open_trader.prediction_n_leg_oracle import (
    RelationComponent,
    build_relation_components,
)
from open_trader.prediction_solver import BenchmarkLimits, SolverBackend
from open_trader.prediction_solver_verified import (
    PROOF_REQUEST_SCHEMA_V1,
    ProofInput,
    VerificationStatus,
    candidate_evidence_from_payload,
    quote_fingerprint,
    solve,
    verification_result_from_payload,
    verify,
)


def relation_generation_components(
    generation: Mapping[str, Mapping[str, object]],
) -> tuple[RelationComponent, ...]:
    """Build the canonical N-leg components of the current relation generation.

    ``generation`` is the ``RelationCatalog.current_generation()`` mapping of
    identity -> row. Returns the empty tuple while no ACTIVE, model-complete row
    exists; COMPLETE rows without a compiled problem fail closed instead of
    being silently admitted.
    """
    rows = tuple(
        row
        for row in generation.values()
        if row.get("activation") == "ACTIVE" and _model_complete(row)
    )
    if not rows:
        return ()
    return build_relation_components(_compile(rows))


def _model_complete(row: Mapping[str, object]) -> bool:
    model = row.get("model")
    if not isinstance(model, Mapping):
        return False
    return all(
        model.get(name) not in (None, "", [])
        for name in ("terminal_states", "payouts", "capital_release")
    )


def _compile(rows: tuple[Mapping[str, object], ...]) -> ArbitrageProblem:
    """Compile admissible rows into one N-leg problem for component building."""
    problems: list[ArbitrageProblem] = []
    for row in rows:
        model = row.get("model")
        payload = model.get("problem") if isinstance(model, Mapping) else None
        if not isinstance(payload, Mapping):
            raise ValueError(
                f"COMPLETE relation {row.get('identity', '?')} has no compiled "
                "problem payload; threshold enrichment must attach model.problem"
            )
        problems.append(problem_from_payload(payload))
    return _merge(problems)


def _merge(problems: list[ArbitrageProblem]) -> ArbitrageProblem:
    valuation_unit_id = problems[0].valuation_unit_id
    actions: dict[str, CandidateAction] = {}
    states: dict[str, TerminalStateSet] = {}
    relations: dict[str, RelationConstraint] = {}
    forbidden: dict[str, ForbiddenAtomCombination] = {}
    qualifications: dict[str, QualificationConstraint] = {}
    for problem in problems[1:]:
        if problem.schema_version != PROBLEM_SCHEMA_V1:
            raise ValueError("compiled problems must use the canonical schema version")
        if problem.valuation_unit_id != valuation_unit_id:
            raise ValueError("compiled problems must share one valuation unit")
    for problem in problems:
        for action in problem.actions:
            _merge_one(actions, action.action_id, action, "action")
        for state in problem.terminal_state_sets:
            _merge_one(states, state.market_contract_id, state, "terminal state set")
        for relation in problem.constraint_model.relations:
            _merge_one(relations, relation.constraint_id, relation, "relation")
        for item in problem.constraint_model.forbidden_atom_combinations:
            _merge_one(forbidden, item.constraint_id, item, "forbidden atom combination")
        for item in problem.qualification_constraints:
            _merge_one(qualifications, item.constraint_id, item, "qualification constraint")
    return ArbitrageProblem(
        PROBLEM_SCHEMA_V1,
        "relation-generation-components",
        max(problem.as_of for problem in problems),
        valuation_unit_id,
        tuple(actions.values()),
        tuple(states.values()),
        ConstraintModel(tuple(relations.values()), tuple(forbidden.values())),
        tuple(qualifications.values()),
    )


def _merge_one(
    index: dict[str, object], key: str, value: object, label: str
) -> None:
    existing = index.get(key)
    if existing is not None and canonical_payload(existing) != canonical_payload(value):
        raise ValueError(f"{label} {key!r} conflicts across compiled relations")
    index.setdefault(key, value)


@dataclass(frozen=True, slots=True)
class BackgroundResolution:
    """The fixed first-resolution outcome of one candidate component."""

    status: VerificationStatus
    initial_verified_profit: int | None
    optimality: OptimalityStatus
    solution: PortfolioSolution | None


def idle_capacity() -> bool:
    """Return True when the #52 real-time worker has no pending snapshot.

    Default seam: background discovery may only consume idle capacity so it never
    competes with the real-time window. The #52 integration injects the live check.
    """
    return True


def resolve_background_candidate(
    problem: ArbitrageProblem,
    *,
    budget: OracleBudget,
    limits: BenchmarkLimits,
    backend: SolverBackend | None = None,
    generation: int = 0,
    code_version: str = "issue-77",
) -> BackgroundResolution | None:
    """Resolve one compiled component once within a fixed background budget.

    Returns ``None`` while the background path is not idle. Otherwise runs the
    #50 solve + verify seam on the compiled problem and fixes
    ``initial_verified_profit`` to the verifier's worst-payout-minus-cost proof,
    never the unverified solver objective or bound.
    """
    if not idle_capacity():
        return None
    proof_input = ProofInput(
        PROOF_REQUEST_SCHEMA_V1,
        OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, budget),
        limits,
        quote_fingerprint(problem),
        generation,
        code_version,
    )
    try:
        evidence = candidate_evidence_from_payload(solve(canonical_payload(proof_input), backend=backend))
        verification = verification_result_from_payload(verify(canonical_payload(evidence)), source=evidence)
    except (TypeError, ValueError):
        return BackgroundResolution(VerificationStatus.UNKNOWN, None, OptimalityStatus.NOT_APPLICABLE, None)
    if verification.status != VerificationStatus.QUALIFIED_VERIFIED or verification.solution is None:
        return BackgroundResolution(verification.status, None, OptimalityStatus.NOT_APPLICABLE, None)
    optimality = (
        OptimalityStatus.OPTIMAL
        if evidence.solver_evidence.global_search_closed
        else OptimalityStatus.NOT_PROVEN
    )
    return BackgroundResolution(
        VerificationStatus.QUALIFIED_VERIFIED,
        verification.solution.payout_proof.guaranteed_profit_units,
        optimality,
        verification.solution,
    )


def _problem_for_component(
    problem: ArbitrageProblem, component: RelationComponent
) -> ArbitrageProblem:
    """Restrict one merged problem to a single canonical relation component."""
    action_ids = set(component.action_ids)
    contract_ids = set(component.contract_ids)
    constraint_ids = set(component.constraint_ids)
    actions = tuple(
        action for action in problem.actions if action.action_id in action_ids
    )
    states = tuple(
        state
        for state in problem.terminal_state_sets
        if state.market_contract_id in contract_ids
    )
    relations = tuple(
        relation
        for relation in problem.constraint_model.relations
        if relation.constraint_id in constraint_ids
    )
    forbidden = tuple(
        combination
        for combination in problem.constraint_model.forbidden_atom_combinations
        if combination.constraint_id in constraint_ids
    )
    return ArbitrageProblem(
        problem.schema_version,
        problem.problem_id,
        problem.as_of,
        problem.valuation_unit_id,
        actions,
        states,
        ConstraintModel(relations, forbidden),
        problem.qualification_constraints,
    )


def run_discovery(
    problem: ArbitrageProblem,
    components: tuple[RelationComponent, ...],
    *,
    budget: OracleBudget,
    limits: BenchmarkLimits,
    backend: SolverBackend | None = None,
    generation: int = 0,
    code_version: str = "issue-77",
    max_components: int | None = None,
) -> tuple[BackgroundResolution, ...]:
    """Resolve candidates one at a time within the background budget.

    Returns the empty tuple when the background path is not idle. Components are
    processed in order; ``max_components`` bounds the pass and each component is
    resolved at most once.
    """
    if not idle_capacity():
        return ()
    selected = components if max_components is None else components[:max_components]
    results: list[BackgroundResolution] = []
    for component in selected:
        resolution = resolve_background_candidate(
            _problem_for_component(problem, component),
            budget=budget,
            limits=limits,
            backend=backend,
            generation=generation,
            code_version=code_version,
        )
        if resolution is not None:
            results.append(resolution)
    return tuple(results)


@dataclass(frozen=True, slots=True)
class SelectedComponent:
    """One persisted monitor slot: component identity plus fixed admission facts."""

    component_id: str
    contract_ids: tuple[str, ...]
    constraint_ids: tuple[str, ...]
    action_ids: tuple[str, ...]
    admission_score: int
    portfolio: tuple[ActionQuantity, ...]
    relation_fingerprint: str
    terminal_fingerprint: str
    portfolio_fingerprint: str
    status: str


def select_monitor_components(
    candidates: Mapping[str, BackgroundResolution],
    current: Mapping[str, SelectedComponent],
    *,
    problem: ArbitrageProblem,
    components: Mapping[str, RelationComponent],
    max_slots: int = 10,
) -> dict[str, SelectedComponent]:
    """Select at most ``max_slots`` monitor components by fixed admission score.

    Only ``QUALIFIED_VERIFIED`` candidates with ``initial_verified_profit > 0``
    enter; ``current`` entries persist unless replaced by a same-id candidate.
    The result is ranked by admission score descending, ties by component id.
    Components of one generation are connected components of one merged problem,
    so they are disjoint by construction and need no runtime overlap check.
    Structural invalidation is the caller's job: pass a ``current`` that already
    excludes invalidated components.
    """
    if max_slots < 0:
        raise ValueError("max_slots must be non-negative")
    selected: dict[str, SelectedComponent] = dict(current)
    for component_id, resolution in candidates.items():
        if resolution.status != VerificationStatus.QUALIFIED_VERIFIED:
            continue
        profit = resolution.initial_verified_profit
        if resolution.solution is None or profit is None or profit <= 0:
            continue
        component = components.get(component_id)
        if component is None or component.component_id != component_id:
            raise ValueError(
                f"candidate {component_id!r} has no matching component structure"
            )
        selected[component_id] = _selected_component(
            component_id, component, resolution, problem
        )
    ranked = sorted(
        selected.values(), key=lambda item: (-item.admission_score, item.component_id)
    )
    return {item.component_id: item for item in ranked[:max_slots]}


def _selected_component(
    component_id: str,
    component: RelationComponent,
    resolution: BackgroundResolution,
    problem: ArbitrageProblem,
) -> SelectedComponent:
    sub = _problem_for_component(problem, component)
    solution = resolution.solution
    assert solution is not None
    return SelectedComponent(
        component_id=component_id,
        contract_ids=component.contract_ids,
        constraint_ids=component.constraint_ids,
        action_ids=component.action_ids,
        admission_score=resolution.initial_verified_profit or 0,
        portfolio=solution.quantities,
        relation_fingerprint=fingerprint({"constraint_model": sub.constraint_model}),
        terminal_fingerprint=fingerprint({"terminal_state_sets": sub.terminal_state_sets}),
        portfolio_fingerprint=fingerprint({"quantities": solution.quantities}),
        status="ACTIVE",
    )


def selection_fingerprint(selection: Mapping[str, SelectedComponent]) -> str:
    """Canonical sha256 of the whole selection ordered by component id."""
    ordered = [selection[component_id] for component_id in sorted(selection)]
    return fingerprint({"selection": ordered})


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


_MONITOR_SELECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_selection_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    selection_fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_selection_components (
    component_id TEXT PRIMARY KEY,
    contract_ids TEXT NOT NULL,
    constraint_ids TEXT NOT NULL,
    action_ids TEXT NOT NULL,
    admission_score INTEGER NOT NULL,
    portfolio TEXT NOT NULL,
    relation_fingerprint TEXT NOT NULL,
    terminal_fingerprint TEXT NOT NULL,
    portfolio_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _encode_portfolio(quantities: tuple[ActionQuantity, ...]) -> str:
    return json.dumps(
        [
            {"action_id": quantity.action_id, "quantity_lots": quantity.quantity_lots}
            for quantity in quantities
        ],
        sort_keys=True,
    )


def _decode_portfolio(raw: str) -> tuple[ActionQuantity, ...]:
    return tuple(
        ActionQuantity(str(item["action_id"]), int(item["quantity_lots"]))
        for item in json.loads(raw)
    )


def _decode_ids(raw: str) -> tuple[str, ...]:
    return tuple(str(item) for item in json.loads(raw))


class MonitorSelectionStore:
    """Persist the selected-monitor set in the shared prediction SQLite (WAL).

    The selection is rewritten atomically as one transaction; in-flight solves,
    pending snapshots and quote freshness are never stored here.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.path = (
            Path(data_dir)
            / "prediction_arbitrage"
            / "prediction_arbitrage.sqlite3"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_MONITOR_SELECTION_SCHEMA)

    def save(self, selection: Mapping[str, SelectedComponent]) -> str:
        fingerprint_value = selection_fingerprint(selection)
        now = _utc_now()
        rows = [
            (
                item.component_id,
                json.dumps(list(item.contract_ids)),
                json.dumps(list(item.constraint_ids)),
                json.dumps(list(item.action_ids)),
                item.admission_score,
                _encode_portfolio(item.portfolio),
                item.relation_fingerprint,
                item.terminal_fingerprint,
                item.portfolio_fingerprint,
                item.status,
                now,
                now,
            )
            for item in sorted(selection.values(), key=lambda item: item.component_id)
        ]
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM monitor_selection_components")
            connection.execute(
                """
                INSERT INTO monitor_selection_meta(
                    singleton, selection_fingerprint, updated_at
                ) VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    selection_fingerprint=excluded.selection_fingerprint,
                    updated_at=excluded.updated_at
                """,
                (fingerprint_value, now),
            )
            connection.executemany(
                """
                INSERT INTO monitor_selection_components(
                    component_id, contract_ids, constraint_ids, action_ids,
                    admission_score, portfolio, relation_fingerprint,
                    terminal_fingerprint, portfolio_fingerprint, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return fingerprint_value

    def load(self) -> tuple[str, dict[str, SelectedComponent]]:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT selection_fingerprint FROM monitor_selection_meta"
                " WHERE singleton=1"
            ).fetchone()
            if row is None:
                return "", {}
            rows = connection.execute(
                """
                SELECT component_id, contract_ids, constraint_ids, action_ids,
                       admission_score, portfolio, relation_fingerprint,
                       terminal_fingerprint, portfolio_fingerprint, status
                FROM monitor_selection_components
                """
            ).fetchall()
        return str(row[0]), {
            str(component_id): SelectedComponent(
                component_id=str(component_id),
                contract_ids=_decode_ids(contract_ids),
                constraint_ids=_decode_ids(constraint_ids),
                action_ids=_decode_ids(action_ids),
                admission_score=int(admission_score),
                portfolio=_decode_portfolio(portfolio),
                relation_fingerprint=str(relation_fingerprint),
                terminal_fingerprint=str(terminal_fingerprint),
                portfolio_fingerprint=str(portfolio_fingerprint),
                status=str(status),
            )
            for component_id, contract_ids, constraint_ids, action_ids,
                admission_score, portfolio, relation_fingerprint,
                terminal_fingerprint, portfolio_fingerprint, status in rows
        }
