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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from open_trader.prediction_n_leg import (
    PROBLEM_SCHEMA_V1,
    REQUEST_SCHEMA_V1,
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
