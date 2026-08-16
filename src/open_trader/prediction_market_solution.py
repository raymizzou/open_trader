"""Issue #84: MarketSolution / ExecutionSolution interpretation.

The monitor path turns each selected component's newest snapshot into a
``WorkerRequest`` whose cost slices are rebuilt from the live book (#84),
then interprets the #50 solve + verify outcome as a component-level
``MarketSolution`` and, with an injectable account seam, at most one
``ExecutionSolution``.

Scope: no orders, no ORDER_READY, no partial-fill proofs (#85/#74 are later);
the payout proof comes from the #50 solve/verify seam only, and an unchanged
structure reuses the fixed portfolio by re-verifying it against current cost
slices without a new raw solve.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from open_trader.prediction_n_leg import (
    PROBLEM_SCHEMA_V1,
    REQUEST_SCHEMA_V1,
    ActionQuantity,
    ActionSide,
    ArbitrageProblem,
    CandidateAction,
    ExecutableCostSlice,
    OracleBudget,
    OracleRequest,
    PortfolioCandidate,
    SearchMode,
    canonical_payload,
    fingerprint,
)
from open_trader.prediction_n_leg_oracle import cost_upper_bound, evaluate_fixed_portfolio
from open_trader.prediction_snapshot_scheduler import ComponentSnapshot, economic_fingerprint
from open_trader.prediction_solver import (
    BenchmarkLimits,
    ObjectiveBounds,
    SolverBackend,
    SolverEvidence,
)
from open_trader.prediction_solver_verified import (
    CANDIDATE_EVIDENCE_SCHEMA_V1,
    PROOF_REQUEST_SCHEMA_V1,
    CandidateEvidence,
    ProofInput,
    VerificationResult,
    VerificationStatus,
    candidate_evidence_from_payload,
    model_fingerprint,
    quote_fingerprint,
    solve,
    verification_result_from_payload,
    verify,
)
from open_trader.prediction_solver_worker import WorkerRequest


_PPM = 1_000_000

EXECUTABLE_REASON = "EXECUTABLE"
INSUFFICIENT_FUNDS_REASON = "INSUFFICIENT_FUNDS"
UNSETTLED_CAP_EXCEEDED_REASON = "UNSETTLED_CAP_EXCEEDED"
INSUFFICIENT_DEPTH_REASON = "INSUFFICIENT_DEPTH"


@dataclass(frozen=True, slots=True)
class MarketSolution:
    """A verified fixed-portfolio market opportunity for one component."""

    component_id: str
    structure_fingerprint: str
    quote_fingerprint: str
    quantities: tuple[ActionQuantity, ...]
    guaranteed_profit_units: int
    bounded_cost_units: int
    bounded_payout_units: int
    capital_release_at: datetime | None
    global_search_closed: bool
    verification_fingerprint: str


@dataclass(frozen=True, slots=True)
class AccountView:
    """Injectable Predict account seam for execution funding checks."""

    available_units: int
    allowance_units: int
    unsettled_capital_units: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionSolution:
    """Funding interpretation of a MarketSolution; never order-ready here."""

    market_solution_fingerprint: str
    quantities: tuple[ActionQuantity, ...]
    capital_use_units: int
    reason: str
    order_ready: bool
    partial_fill_proof: str


@dataclass(frozen=True, slots=True)
class ComponentResolution:
    status: VerificationStatus
    market_solution: MarketSolution | None
    reason: str | None


def cost_slices_from_book(
    action: CandidateAction,
    book: object,
    *,
    fee_ppm: int = 0,
    haircut_ppm: int = 0,
    tick_units: int = 0,
    price_units_per_quote_unit: int = 1,
    common_units_per_dollar: int | None = None,
) -> tuple[ExecutableCostSlice, ...]:
    """Build executable cost slices from a live book (口径沿用 #51).

    Buy legs walk the asks best-first; NO legs walk the bids best-first.
    Each lot's upper bound is price x lot_step plus fee plus slippage/safety
    margin (tick + haircut), rounded the same way as the #51 quote pipeline.
    Consecutive levels with the same unit cost merge into one slice; depth
    only covers executable lots, so an exhausted book truncates.
    """
    if not isinstance(action, CandidateAction):
        raise ValueError("action must be a CandidateAction")
    levels = getattr(book, "bids" if action.side == ActionSide.BUY_NO else "asks", ())
    if common_units_per_dollar is None:
        common_units_per_dollar = price_units_per_quote_unit
    slices: list[ExecutableCostSlice] = []
    first = 1
    for level in levels:
        price = getattr(level, "price", None)
        size = getattr(level, "size", None)
        if (
            not isinstance(price, Decimal)
            or not isinstance(size, Decimal)
            or price <= 0
            or size <= 0
            or not price.is_finite()
            or not size.is_finite()
        ):
            raise ValueError("malformed book level")
        lots = int(
            (size * action.quantity_scale / action.lot_step_units).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        if lots <= 0:
            continue
        protected = int(
            (price * price_units_per_quote_unit).to_integral_value(rounding=ROUND_CEILING)
        ) + tick_units
        venue_cost = _ceil_div(protected * action.lot_step_units, action.quantity_scale)
        common_cost = _ceil_div(venue_cost * common_units_per_dollar, price_units_per_quote_unit)
        fee = _ceil_div(common_cost * fee_ppm, _PPM)
        haircut = _ceil_div((common_cost + fee) * haircut_ppm, _PPM)
        unit_cost = common_cost + fee + haircut
        last = min(action.max_quantity_lots, first + lots - 1)
        if (
            slices
            and slices[-1].incremental_cost_upper_bound_units == unit_cost
            and slices[-1].last_lot + 1 == first
        ):
            slices[-1] = ExecutableCostSlice(slices[-1].first_lot, last, unit_cost)
        else:
            slices.append(ExecutableCostSlice(first, last, unit_cost))
        first = last + 1
        if last == action.max_quantity_lots:
            break
    return tuple(slices)


def structure_fingerprint(
    problem: ArbitrageProblem,
    quantities: tuple[ActionQuantity, ...],
) -> str:
    """Structural identity: relations, actions (sides/domains), terminal states, quantities.

    Quote prices/costs/fees and solve time never enter, so an unchanged
    structure can reuse the #50 fixed-portfolio proof while only live cost
    and qualification are recomputed.
    """
    return fingerprint(
        {
            "constraint_model": problem.constraint_model,
            "terminal_state_sets": problem.terminal_state_sets,
            "actions": tuple(
                {
                    name: value
                    for name, value in canonical_payload(action).items()
                    if name != "cost_slices"
                }
                for action in problem.actions
            ),
            "quantities": quantities,
        }
    )


def build_solve_request(
    problem: ArbitrageProblem,
    snapshot: ComponentSnapshot,
    *,
    budget: OracleBudget,
    limits: BenchmarkLimits,
    fee_ppm: int = 0,
    haircut_ppm: int = 0,
    tick_units: int = 0,
    price_units_per_quote_unit: int = 1,
) -> WorkerRequest:
    """Build the real #83 worker request: compiled model + snapshot books."""
    if not isinstance(snapshot, ComponentSnapshot):
        raise ValueError("snapshot must be a ComponentSnapshot")
    legs = {leg.leg_id: leg for leg in snapshot.legs}
    if set(legs) != {action.action_id for action in problem.actions}:
        raise ValueError("snapshot legs must exactly cover problem actions")
    converted: list[CandidateAction] = []
    for action in problem.actions:
        cost_slices = cost_slices_from_book(
            action,
            legs[action.action_id].book,
            fee_ppm=fee_ppm,
            haircut_ppm=haircut_ppm,
            tick_units=tick_units,
            price_units_per_quote_unit=price_units_per_quote_unit,
        )
        if not cost_slices:
            raise ValueError(f"no executable depth for action {action.action_id}")
        converted.append(
            replace(
                action,
                max_quantity_lots=min(action.max_quantity_lots, cost_slices[-1].last_lot),
                cost_slices=cost_slices,
            )
        )
    request = OracleRequest(
        REQUEST_SCHEMA_V1,
        SearchMode.ADMISSION,
        replace(problem, actions=tuple(converted)),
        budget,
    )
    request_id = f"{snapshot.component_id}:{economic_fingerprint(snapshot)}"
    return WorkerRequest(request_id, "cp_sat", request, limits)


def market_solution_from_verification(
    component_id: str,
    problem: ArbitrageProblem,
    evidence: CandidateEvidence,
    verification: VerificationResult,
) -> MarketSolution | None:
    """Interpret one qualified #50 verification as a component MarketSolution."""
    if (
        verification.status != VerificationStatus.QUALIFIED_VERIFIED
        or verification.solution is None
        or verification.model_fingerprint != model_fingerprint(problem)
        or verification.quote_fingerprint != quote_fingerprint(problem)
    ):
        return None
    proof = verification.solution.payout_proof
    return MarketSolution(
        component_id=component_id,
        structure_fingerprint=structure_fingerprint(problem, verification.solution.quantities),
        quote_fingerprint=verification.quote_fingerprint,
        quantities=verification.solution.quantities,
        guaranteed_profit_units=proof.guaranteed_profit_units,
        bounded_cost_units=proof.cost_upper_bound_units,
        bounded_payout_units=proof.payout_lower_bound_units,
        capital_release_at=proof.conservative_capital_release_at,
        global_search_closed=evidence.solver_evidence.global_search_closed,
        verification_fingerprint=fingerprint(canonical_payload(proof)),
    )


def execution_solution_from_market(
    market: MarketSolution,
    problem: ArbitrageProblem,
    account: AccountView,
    *,
    max_total_unsettled_capital: int,
) -> ExecutionSolution:
    """Interpret a MarketSolution against the account seam (#84 decision 3).

    Depth, available capital/allowance, and the unsettled-capital cap are the
    minimum checks; a failure produces a clear non-executable reason without
    changing MarketSolution qualification.
    """
    actions = {action.action_id: action for action in problem.actions}
    for quantity in market.quantities:
        action = actions.get(quantity.action_id)
        if action is None or not action.cost_slices:
            raise ValueError(f"market solution references unknown action {quantity.action_id}")
        if quantity.quantity_lots > action.cost_slices[-1].last_lot:
            return ExecutionSolution(
                fingerprint(market),
                market.quantities,
                0,
                INSUFFICIENT_DEPTH_REASON,
                False,
                "UNKNOWN",
            )
    capital_use = sum(cost_upper_bound(problem, (quantity,)) for quantity in market.quantities)
    if capital_use > account.available_units or capital_use > account.allowance_units:
        reason = INSUFFICIENT_FUNDS_REASON
    elif account.unsettled_capital_units + capital_use > max_total_unsettled_capital:
        reason = UNSETTLED_CAP_EXCEEDED_REASON
    else:
        reason = EXECUTABLE_REASON
    return ExecutionSolution(
        fingerprint(market), market.quantities, capital_use, reason, False, "UNKNOWN"
    )


def negative_proof_matches(
    evidence: CandidateEvidence,
    verification: VerificationResult,
    problem: ArbitrageProblem,
    *,
    code_version: str,
) -> bool:
    """Consume a component NO_QUALIFIED_OPPORTUNITY proof only when it binds exactly."""
    proof = verification.negative_proof
    return bool(
        proof is not None
        and proof.result_kind.value == "NO_QUALIFIED_OPPORTUNITY"
        and proof.source_problem_fingerprint is None
        and evidence.proof_input.code_version == code_version
        and evidence.proof_input.current_generation == verification.current_generation
        and verification.model_fingerprint == model_fingerprint(problem)
        and verification.quote_fingerprint == quote_fingerprint(problem)
        and proof.problem_fingerprint == fingerprint(problem)
        and proof.qualification_fingerprint == _qualification_fingerprint(problem)
    )


def resolution_from_verification(
    component_id: str,
    problem: ArbitrageProblem,
    evidence: CandidateEvidence,
    verification: VerificationResult,
    *,
    code_version: str,
) -> ComponentResolution:
    """Map one verification outcome; mismatched negatives and non-proofs fail closed."""
    if verification.status == VerificationStatus.QUALIFIED_VERIFIED:
        market_solution = market_solution_from_verification(
            component_id, problem, evidence, verification
        )
        if market_solution is None:
            return ComponentResolution(
                VerificationStatus.UNKNOWN, None, "INVALID_QUALIFIED_VERIFICATION"
            )
        return ComponentResolution(VerificationStatus.QUALIFIED_VERIFIED, market_solution, None)
    if verification.status == VerificationStatus.NO_QUALIFIED_OPPORTUNITY:
        if negative_proof_matches(evidence, verification, problem, code_version=code_version):
            return ComponentResolution(VerificationStatus.NO_QUALIFIED_OPPORTUNITY, None, None)
        return ComponentResolution(VerificationStatus.UNKNOWN, None, "NEGATIVE_PROOF_MISMATCH")
    return ComponentResolution(verification.status, None, verification.unknown_reason)


def resolve_market_solution(
    component_id: str,
    problem: ArbitrageProblem,
    *,
    budget: OracleBudget,
    limits: BenchmarkLimits,
    backend: SolverBackend | None = None,
    generation: int = 0,
    code_version: str = "issue-84",
    prior: MarketSolution | None = None,
) -> ComponentResolution:
    """Run the #50 seam for one component snapshot and interpret the result.

    An unchanged structure (same relations/terminal states/quantities) reuses
    the fixed portfolio: no new raw solve, only exact re-verification against
    the current cost slices. Any solver/verifier timeout or crash maps to
    UNKNOWN.
    """
    proof_input = ProofInput(
        PROOF_REQUEST_SCHEMA_V1,
        OracleRequest(REQUEST_SCHEMA_V1, SearchMode.ADMISSION, problem, budget),
        limits,
        quote_fingerprint(problem),
        generation,
        code_version,
    )
    if prior is not None and prior.structure_fingerprint == structure_fingerprint(
        problem, prior.quantities
    ):
        try:
            evidence, verification = _verify_fixed_quantities(
                problem, prior.quantities, proof_input
            )
        except Exception:
            return ComponentResolution(VerificationStatus.UNKNOWN, None, "VERIFIER_UNKNOWN")
        return resolution_from_verification(
            component_id, problem, evidence, verification, code_version=code_version
        )
    try:
        evidence = candidate_evidence_from_payload(
            solve(canonical_payload(proof_input), backend=backend)
        )
        verification = verification_result_from_payload(
            verify(canonical_payload(evidence)), source=evidence
        )
    except Exception:
        return ComponentResolution(VerificationStatus.UNKNOWN, None, "SOLVER_OR_VERIFIER_UNKNOWN")
    return resolution_from_verification(
        component_id, problem, evidence, verification, code_version=code_version
    )


def _verify_fixed_quantities(
    problem: ArbitrageProblem,
    quantities: tuple[ActionQuantity, ...],
    proof_input: ProofInput,
) -> tuple[CandidateEvidence, VerificationResult]:
    """Re-run the #50 verifier on a fixed portfolio without any raw solve.

    The exact oracle evaluation supplies the complete fixed-portfolio facts
    the canonical evidence envelope requires; ``verify`` then independently
    re-evaluates the same fixed quantities against the current cost slices.
    """
    evaluation = evaluate_fixed_portfolio(problem, quantities, proof_input.request.budget)
    solver_evidence = SolverEvidence(
        native_status="FEASIBLE",
        candidate=PortfolioCandidate(quantities, evaluation.guaranteed_profit_units),
        objective_bounds=ObjectiveBounds(
            evaluation.guaranteed_profit_units, None, None, False
        ),
        worst_scenario=evaluation.worst_scenario,
        payout_lower_bound_units=evaluation.payout_lower_bound_units,
        cost_upper_bound_units=evaluation.cost_upper_bound_units,
        guaranteed_profit_units=evaluation.guaranteed_profit_units,
        conservative_capital_release_at=evaluation.conservative_capital_release_at,
        fixed_portfolio_closed=True,
        global_search_closed=False,
        master_rounds=0,
        adversary_rounds=0,
        cuts=(evaluation.worst_state_cut,),
        certificate=None,
    )
    evidence = CandidateEvidence(
        CANDIDATE_EVIDENCE_SCHEMA_V1,
        proof_input,
        "fixed-portfolio-reuse",
        "reuse",
        model_fingerprint(problem),
        fingerprint({"quantities": quantities}),
        solver_evidence,
    )
    verification = verification_result_from_payload(
        verify(canonical_payload(evidence)), source=evidence
    )
    return evidence, verification


def _qualification_fingerprint(problem: ArbitrageProblem) -> str:
    constraints = tuple(
        sorted(problem.qualification_constraints, key=lambda item: item.constraint_id)
    )
    return fingerprint(
        {"schema_version": PROBLEM_SCHEMA_V1, "qualification_constraints": constraints}
    )


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor
