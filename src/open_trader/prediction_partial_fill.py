"""Issue #74: worst-case partial-fill loss proof (fill adversary).

A fixed #52 ``ExecutionSolution`` is turned into an independent adversarial
problem: the adversary chooses how many lots of each leg actually fill (an
atomic leg fills ``{0, q}``, a partial leg fills ``0..q`` in whole lots) and
which joint terminal atoms settle, and maximizes the conservative loss

    sum_i f_i * ceil(max_cost_i / q_i) + f_i * ceil(max_fee_i / q_i)
        - sum_{i,a} f_i * payout_i(atom(a))

in pure fixed-point integer arithmetic (costs/fees round up, payouts are
already lower bounds, no floats).  Unfilled legs contribute 0 and the all-zero
fill vector has loss 0, so the adversary optimum is always >= 0.

The problem payload is canonical and fingerprinted; the solver evidence and
the verifier recomputation are separated exactly like
``prediction_solver_verified``.  ``prove_partial_fill`` assembles the existing
``PartialFillProofRecord`` (schema v1, field names fixed) and returns one of
``PARTIAL_FILL_SAFE`` / ``PARTIAL_FILL_UNSAFE`` / ``UNKNOWN``:

- SAFE   : the adversary problem closed to OPTIMAL and the independent
           verifier recomputation agreed, and the worst loss <= cap.
- UNSAFE : same closure, worst loss > cap, and the counterexample
           (fill vector + joint atoms) was re-verified in fixed-point.
- UNKNOWN: unknown order semantics, incomplete input, timeout, unclosed
           solve, or verifier disagreement.  Never "safe by absence".

Production never enumerates the prod(q_i + 1) fill vectors and never samples;
exhaustive enumeration only happens inside the bounded Oracle entry.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import StrEnum
from itertools import product
from typing import Mapping

from open_trader.prediction_executable_cost import ExecutionSolution as _HeavyExecutionSolution
from open_trader.prediction_market_solution import ExecutionSolution as _MarketExecutionSolution
from open_trader.prediction_n_leg import (
    ArbitrageProblem,
    ActionQuantity,
    OracleBudget,
    SelectedAtom,
    SettlementScenario,
    _sorted_problem,
    canonical_payload,
    fingerprint,
    problem_from_payload,
)
from open_trader.prediction_n_leg_execution import (
    PARTIAL_FILL_PROOF_SCHEMA_V1,
    PartialFillProofRecord,
    execution_solution_binding,
)
from open_trader.prediction_n_leg_oracle import (
    UnknownReason,
    cost_upper_bound,
    enumerate_allowed_scenarios,
)
from open_trader.prediction_solver import (
    BackendResult,
    IntVariable,
    LinearConstraint,
    LinearModel,
    LinearObjective,
    NativeSolveStatus,
    SolverBackend,
    UnsafeSolverResult,
    compile_terminal_model,
    validate_backend_result,
    validate_linear_model,
)

FILL_ADVERSARY_SCHEMA_V1 = "open_trader.prediction_partial_fill.fill_adversary.v1"
FILL_ADVERSARY_EVIDENCE_SCHEMA_V1 = (
    "open_trader.prediction_partial_fill.fill_adversary_evidence.v1"
)
FILL_ADVERSARY_VERIFICATION_SCHEMA_V1 = (
    "open_trader.prediction_partial_fill.fill_adversary_verification.v1"
)
ORDER_SEMANTICS_SCHEMA_V1 = "open_trader.prediction_partial_fill.order_semantics.v1"

FILL_ADVERSARY_PROBLEM_KIND = "FILL_ADVERSARY"

PARTIAL_FILL_SAFE = "PARTIAL_FILL_SAFE"
PARTIAL_FILL_UNSAFE = "PARTIAL_FILL_UNSAFE"
PARTIAL_FILL_UNKNOWN = "UNKNOWN"

VERIFIER_QUALIFIED = "QUALIFIED_VERIFIED"
VERIFIER_NOT_APPLICABLE = "NOT_APPLICABLE"

#: Backend termination reported when the adversary closed to OPTIMAL.
TERMINATION_CLOSED = "CLOSED"


class FillSemantics(StrEnum):
    ATOMIC = "ATOMIC"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


#: Versioned order-semantics table v1.  The whole contract (default order
#: types plus this table) is versioned under ORDER_SEMANTICS_SCHEMA_V1; any
#: bump changes the order_semantics_fingerprint and invalidates proof caches.
ORDER_SEMANTICS_TABLE_V1: tuple[tuple[str, str, FillSemantics], ...] = (
    ("polymarket", "FOK", FillSemantics.ATOMIC),
    ("predict.fun", "LIMIT", FillSemantics.PARTIAL),
)

#: Default order type per venue when the caller only knows the venue.
DEFAULT_ORDER_TYPES_V1: tuple[tuple[str, str], ...] = (
    ("polymarket", "FOK"),
    ("predict.fun", "LIMIT"),
)


def order_semantics_lookup(venue_id: str, order_type: str) -> FillSemantics:
    """The versioned semantics assertion for one (venue, order type) pair."""
    for venue, order, semantics in ORDER_SEMANTICS_TABLE_V1:
        if venue == venue_id and order == order_type:
            return semantics
    return FillSemantics.UNKNOWN


def default_order_type(venue_id: str) -> str | None:
    """Default order type for a venue, or None when the venue is unknown."""
    for venue, order_type in DEFAULT_ORDER_TYPES_V1:
        if venue == venue_id:
            return order_type
    return None


def order_semantics_fingerprint_for(
    legs: tuple[tuple[str, str, str, str], ...],
) -> str:
    """Stable semantics fingerprint: table version plus per-leg assertions."""
    return fingerprint(
        {
            "order_semantics_version": ORDER_SEMANTICS_SCHEMA_V1,
            "legs": legs,
        }
    )


@dataclass(frozen=True, slots=True)
class FillLeg:
    """One frozen execution leg plus its asserted fill semantics."""

    action_id: str
    venue_id: str
    quantity_lots: int
    order_type: str
    semantics: str
    max_cost_units: int
    max_fee_units: int

    def __post_init__(self) -> None:
        for name in ("action_id", "venue_id", "order_type", "semantics"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"fill leg {name} must be a non-empty string")
        if self.semantics not in tuple(FillSemantics):
            raise ValueError(f"unknown fill semantics: {self.semantics}")
        # Lot space is discrete in 1-lot units: every fill between 0 and
        # quantity_lots is reachable.  The venue's lot_step_units is only a
        # units-per-lot conversion factor (size * quantity_scale /
        # lot_step_units -> lots) and never restricts the fill domain, so it
        # is not part of the fill leg at all.
        if type(self.quantity_lots) is not int or self.quantity_lots <= 0:
            raise ValueError(f"fill leg quantity_lots must be a positive integer")
        for name in ("max_cost_units", "max_fee_units"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"fill leg {name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class FillAdversaryProblem:
    """The canonical, fingerprinted fill-adversary input (#74)."""

    schema_version: str
    problem_kind: str
    execution_solution_fingerprint: str
    execution_solution_payload_fingerprint: str
    model_fingerprint: str
    quote_fingerprint: str
    cost_fingerprint: str
    order_semantics_fingerprint: str
    order_semantics_version: str
    cap_config_version: str
    max_partial_fill_loss: int
    max_auto_repair_loss: int
    legs: tuple[FillLeg, ...]
    source_problem: ArbitrageProblem
    fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != FILL_ADVERSARY_SCHEMA_V1:
            raise ValueError("unsupported fill-adversary schema")
        if self.problem_kind != FILL_ADVERSARY_PROBLEM_KIND:
            raise ValueError("unexpected fill-adversary problem kind")
        if self.order_semantics_version != ORDER_SEMANTICS_SCHEMA_V1:
            raise ValueError("unsupported order-semantics schema")
        if not isinstance(self.source_problem, ArbitrageProblem):
            raise ValueError("source_problem must be an ArbitrageProblem")
        if not isinstance(self.legs, tuple) or not self.legs:
            raise ValueError("fill adversary requires at least one leg")
        for name in (
            "execution_solution_fingerprint",
            "execution_solution_payload_fingerprint",
            "model_fingerprint",
            "quote_fingerprint",
            "cost_fingerprint",
            "order_semantics_fingerprint",
            "cap_config_version",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("max_partial_fill_loss", "max_auto_repair_loss"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        actions = {
            action.action_id: action for action in self.source_problem.actions
        }
        seen: set[str] = set()
        for leg in self.legs:
            if leg.action_id in seen:
                raise ValueError(f"duplicate fill leg: {leg.action_id}")
            seen.add(leg.action_id)
            action = actions.get(leg.action_id)
            if action is None:
                raise ValueError(
                    f"fill leg references unknown action: {leg.action_id}"
                )
            if leg.venue_id != action.venue_id:
                raise ValueError(
                    f"fill leg venue does not match action venue: {leg.action_id}"
                )
            if leg.semantics != order_semantics_lookup(
                leg.venue_id, leg.order_type
            ).value:
                raise ValueError(
                    "fill leg semantics contradicts the versioned semantics "
                    f"table: {leg.action_id}"
                )
        expected = fingerprint(
            {
                key: value
                for key, value in asdict(self).items()
                if key != "fingerprint"
            }
        )
        if self.fingerprint != expected:
            raise ValueError("fill-adversary problem fingerprint mismatch")

    def to_payload(self) -> dict[str, object]:
        return canonical_payload(asdict(self))


def _problem_fingerprint(fields: Mapping[str, object]) -> str:
    # Canonicalize the payload form (dataclasses -> asdict), exactly the
    # structure the record's __post_init__ recomputes over and the form the
    # verifier rebuilds from; never the raw dataclass form, whose nested
    # tuple ordering canonicalizes differently.
    values = {
        key: (
            asdict(value)
            if is_dataclass(value) and not isinstance(value, type)
            else value
        )
        for key, value in fields.items()
        if key != "fingerprint"
    }
    return fingerprint(values)


def fill_adversary_problem(
    *,
    execution_solution_fingerprint: str,
    execution_solution_payload_fingerprint: str,
    model_fingerprint: str,
    quote_fingerprint: str,
    cost_fingerprint: str,
    order_semantics_fingerprint: str,
    cap_config_version: str,
    max_partial_fill_loss: int,
    max_auto_repair_loss: int,
    legs: tuple[FillLeg, ...],
    source_problem: ArbitrageProblem,
) -> FillAdversaryProblem:
    """Construct a canonical fill-adversary problem with its fingerprint.

    The embedded source problem is normalized to the same sorted form
    ``problem_from_payload`` produces, so the fingerprint is stable across
    payload roundtrips and the verifier's independent rebuild.
    """
    canonical_problem = _sorted_problem(source_problem)
    fields: dict[str, object] = {
        "schema_version": FILL_ADVERSARY_SCHEMA_V1,
        "problem_kind": FILL_ADVERSARY_PROBLEM_KIND,
        "execution_solution_fingerprint": execution_solution_fingerprint,
        "execution_solution_payload_fingerprint": (
            execution_solution_payload_fingerprint
        ),
        "model_fingerprint": model_fingerprint,
        "quote_fingerprint": quote_fingerprint,
        "cost_fingerprint": cost_fingerprint,
        "order_semantics_fingerprint": order_semantics_fingerprint,
        "order_semantics_version": ORDER_SEMANTICS_SCHEMA_V1,
        "cap_config_version": cap_config_version,
        "max_partial_fill_loss": max_partial_fill_loss,
        "max_auto_repair_loss": max_auto_repair_loss,
        "legs": legs,
        "source_problem": canonical_problem,
    }
    return FillAdversaryProblem(
        **fields, fingerprint=_problem_fingerprint(fields)
    )  # type: ignore[arg-type]


def fill_adversary_problem_from_payload(payload: object) -> FillAdversaryProblem:
    keys = set(FillAdversaryProblem.__dataclass_fields__)
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError("unexpected FillAdversaryProblem fields")
    value = dict(payload)
    value["legs"] = tuple(FillLeg(**leg) for leg in value["legs"])
    value["source_problem"] = problem_from_payload(value["source_problem"])
    return FillAdversaryProblem(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CompiledFillAdversary:
    """Compiled adversary model plus the fill-variable mapping."""

    model: LinearModel
    legs: tuple[FillLeg, ...]
    fill_variables: tuple[tuple[str, str], ...]


def _ceil_div(numerator: int, denominator: int) -> int:
    if (
        type(numerator) is not int
        or type(denominator) is not int
        or denominator <= 0
    ):
        raise ValueError("ceil division requires an integer over a positive integer")
    return -(-numerator // denominator)


def compile_fill_adversary(problem: FillAdversaryProblem) -> CompiledFillAdversary:
    """Compile the fill-adversary LinearModel (terminal model reused #48).

    Fill integer variables: ATOMIC legs fill ``{0, q}`` (binary selection),
    PARTIAL legs fill ``0..q`` in 1-lot steps (lot space is discrete in whole
    lots).  The payout term
    ``f_i * payout_i(atom)`` is bilinear in the fill variable and the terminal
    atom binary, linearized with big-M helper variables ``y_{i,a} = f_i * z_a``:
    ``y <= f``, ``y <= q*z``, ``y - f - q*z >= -q``.  The adversary maximizes
    ``sum (ceil(max_cost/q) + ceil(max_fee/q)) * f_i - sum y_{i,a} * payout``.
    """
    if not isinstance(problem, FillAdversaryProblem):
        raise ValueError("fill adversary requires a FillAdversaryProblem")
    terminal = compile_terminal_model(problem.source_problem)
    variables = list(terminal.variables)
    constraints = list(terminal.constraints)
    fill_names: list[tuple[str, str]] = []
    objective_terms: list[tuple[str, int]] = []
    for leg in problem.legs:
        if leg.semantics == FillSemantics.UNKNOWN.value:
            raise ValueError(
                "cannot compile fill adversary with unknown order semantics"
            )
        if leg.semantics == FillSemantics.ATOMIC.value:
            selector = IntVariable(f"fill-b:{leg.action_id}", 0, 1)
            fill = IntVariable(f"fill:{leg.action_id}", 0, leg.quantity_lots)
            variables.extend((selector, fill))
            constraints.append(
                LinearConstraint(
                    f"fill:atomic:{leg.action_id}",
                    (
                        (f"fill:{leg.action_id}", 1),
                        (f"fill-b:{leg.action_id}", -leg.quantity_lots),
                    ),
                    0,
                    0,
                )
            )
        else:
            fill = IntVariable(f"fill:{leg.action_id}", 0, leg.quantity_lots)
            variables.append(fill)
        unit_cost = _ceil_div(leg.max_cost_units, leg.quantity_lots)
        unit_fee = _ceil_div(leg.max_fee_units, leg.quantity_lots)
        objective_terms.append((f"fill:{leg.action_id}", unit_cost + unit_fee))
        fill_names.append((leg.action_id, f"fill:{leg.action_id}"))

    atoms_by_contract = {
        state.market_contract_id: state.atoms
        for state in problem.source_problem.terminal_state_sets
    }
    contracts_by_action = {
        action.action_id: action.market_contract_id
        for action in problem.source_problem.actions
    }
    for leg in problem.legs:
        contract_id = contracts_by_action[leg.action_id]
        quantity = leg.quantity_lots
        for atom in atoms_by_contract[contract_id]:
            payout = next(
                (
                    payout.payout_lower_bound_per_lot_units
                    for payout in atom.payouts
                    if payout.action_id == leg.action_id
                ),
                None,
            )
            if payout is None:
                raise ValueError(
                    f"leg {leg.action_id} has no payout in atom {atom.atom_id}"
                )
            name = f"fill-payout:{leg.action_id}:{atom.atom_id}"
            y = IntVariable(name, 0, quantity)
            variables.append(y)
            z = f"z:{atom.atom_id}"
            constraints.extend(
                (
                    LinearConstraint(
                        f"payout:{leg.action_id}:{atom.atom_id}:upper-fill",
                        ((name, 1), (f"fill:{leg.action_id}", -1)),
                        None,
                        0,
                    ),
                    LinearConstraint(
                        f"payout:{leg.action_id}:{atom.atom_id}:upper-atom",
                        ((name, 1), (z, -quantity)),
                        None,
                        0,
                    ),
                    LinearConstraint(
                        f"payout:{leg.action_id}:{atom.atom_id}:lower",
                        ((name, 1), (f"fill:{leg.action_id}", -1), (z, -quantity)),
                        -quantity,
                        None,
                    ),
                )
            )
            if payout > 0:
                objective_terms.append((name, -payout))

    model = LinearModel(
        tuple(variables),
        tuple(constraints),
        LinearObjective("MAX", tuple(objective_terms)),
    )
    validate_linear_model(model)
    return CompiledFillAdversary(model, problem.legs, tuple(fill_names))


def counterexample_loss_units(
    problem: FillAdversaryProblem,
    fill_quantities: tuple[ActionQuantity, ...],
    scenario: SettlementScenario,
) -> int:
    """Fixed-point recomputation of the loss for one (fills, atoms) outcome.

    Pure integer arithmetic: costs/fees round up per lot, payouts are the
    per-lot lower bounds, unfilled legs contribute 0.  This is the independent
    check used to confirm an UNSAFE counterexample without any solver.
    """
    if not isinstance(problem, FillAdversaryProblem) or not isinstance(
        scenario, SettlementScenario
    ):
        raise ValueError("invalid fill adversary counterexample input")
    fills = {quantity.action_id: quantity.quantity_lots for quantity in fill_quantities}
    scenario_atoms = {
        selected.market_contract_id: selected.atom_id for selected in scenario.atoms
    }
    if len(scenario_atoms) != len(scenario.atoms):
        raise ValueError("scenario selects a contract more than once")
    actions = {
        action.action_id: action for action in problem.source_problem.actions
    }
    atoms_by_contract = {
        state.market_contract_id: {
            atom.atom_id: atom for atom in state.atoms
        }
        for state in problem.source_problem.terminal_state_sets
    }
    total = 0
    for leg in problem.legs:
        filled = fills.get(leg.action_id, 0)
        if filled == 0:
            continue
        if type(filled) is not int or not 0 < filled <= leg.quantity_lots:
            raise ValueError(f"invalid fill quantity for {leg.action_id}")
        action = actions.get(leg.action_id)
        if action is None:
            raise ValueError(f"unknown action in counterexample: {leg.action_id}")
        atom = atoms_by_contract.get(action.market_contract_id, {}).get(
            scenario_atoms.get(action.market_contract_id, "")
        )
        if atom is None:
            raise ValueError("scenario is missing the atom for a filled leg")
        payout = next(
            (
                payout.payout_lower_bound_per_lot_units
                for payout in atom.payouts
                if payout.action_id == leg.action_id
            ),
            None,
        )
        if payout is None:
            raise ValueError(
                f"atom {atom.atom_id} has no payout for {leg.action_id}"
            )
        unit_cost = _ceil_div(leg.max_cost_units, leg.quantity_lots)
        unit_fee = _ceil_div(leg.max_fee_units, leg.quantity_lots)
        total += filled * (unit_cost + unit_fee) - filled * payout
    return total


@dataclass(frozen=True, slots=True)
class FillAdversaryEvidence:
    """Self-contained solver evidence (embeds the canonical problem).

    Deliberately carries no timing: the payload is fully deterministic so the
    proof record fingerprint is stable for the same fixed solution + cap and
    can serve as the proof cache key.
    """

    schema_version: str
    problem: FillAdversaryProblem
    solver_name: str
    solver_version: str
    termination: str
    worst_loss_units: int | None
    worst_fill_quantities: tuple[ActionQuantity, ...]
    worst_scenario: SettlementScenario | None

    def __post_init__(self) -> None:
        if (
            self.schema_version != FILL_ADVERSARY_EVIDENCE_SCHEMA_V1
            or not isinstance(self.problem, FillAdversaryProblem)
        ):
            raise ValueError("invalid fill adversary evidence")
        for name in ("solver_name", "solver_version", "termination"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"evidence {name} must be a non-empty string")
        if self.termination == TERMINATION_CLOSED:
            if (
                self.worst_loss_units is None
                or not self.worst_fill_quantities
                or self.worst_scenario is None
            ):
                raise ValueError("closed evidence requires worst loss, fills, atoms")
        elif (
            self.worst_loss_units is not None
            or self.worst_fill_quantities
            or self.worst_scenario is not None
        ):
            raise ValueError("unclosed evidence must not carry worst values")

    def to_payload(self) -> dict[str, object]:
        return canonical_payload(asdict(self))


def fill_adversary_evidence_from_payload(payload: object) -> FillAdversaryEvidence:
    keys = set(FillAdversaryEvidence.__dataclass_fields__)
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError("unexpected FillAdversaryEvidence fields")
    value = dict(payload)
    value["problem"] = fill_adversary_problem_from_payload(value["problem"])
    value["worst_fill_quantities"] = tuple(
        ActionQuantity(**quantity) for quantity in value["worst_fill_quantities"]
    )
    if value["worst_scenario"] is not None:
        value["worst_scenario"] = SettlementScenario(
            tuple(
                SelectedAtom(**atom) for atom in value["worst_scenario"]["atoms"]
            )
        )
    return FillAdversaryEvidence(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FillAdversaryVerification:
    """Verifier recomputation result (independent re-solve plus fixed point)."""

    schema_version: str
    status: str
    problem_fingerprint: str
    solver_termination: str
    solver_worst_loss_units: int | None
    verifier_termination: str
    verifier_worst_loss_units: int | None
    counterexample_loss_units: int | None
    worst_fill_quantities: tuple[ActionQuantity, ...]
    worst_scenario: SettlementScenario | None
    reason: str | None

    def __post_init__(self) -> None:
        if (
            self.schema_version != FILL_ADVERSARY_VERIFICATION_SCHEMA_V1
            or self.status
            not in {
                VERIFIER_QUALIFIED,
                "MISMATCH",
                "INVALID",
                VERIFIER_NOT_APPLICABLE,
            }
        ):
            raise ValueError("invalid fill adversary verification")


def fill_adversary_verification_from_payload(
    payload: object,
) -> FillAdversaryVerification:
    keys = set(FillAdversaryVerification.__dataclass_fields__)
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError("unexpected FillAdversaryVerification fields")
    value = dict(payload)
    value["worst_fill_quantities"] = tuple(
        ActionQuantity(**quantity) for quantity in value["worst_fill_quantities"]
    )
    if value["worst_scenario"] is not None:
        value["worst_scenario"] = SettlementScenario(
            tuple(
                SelectedAtom(**atom) for atom in value["worst_scenario"]["atoms"]
            )
        )
    return FillAdversaryVerification(**value)  # type: ignore[arg-type]


def _default_backend() -> SolverBackend:
    from open_trader.prediction_solver_backends import CpSatBackend

    return CpSatBackend()


def _checked_backend_solve(
    model: LinearModel, backend: SolverBackend, time_limit_ms: int
) -> BackendResult:
    result = backend.solve(model, time_limit_ms=time_limit_ms)
    validate_backend_result(model, result)
    return result


def _scenario_from_values(
    problem: FillAdversaryProblem, values: Mapping[str, int]
) -> SettlementScenario:
    selected: list[SelectedAtom] = []
    for state in problem.source_problem.terminal_state_sets:
        for atom in state.atoms:
            if values[f"z:{atom.atom_id}"] == 1:
                selected.append(SelectedAtom(state.market_contract_id, atom.atom_id))
    return SettlementScenario(tuple(selected))


def solve_fill_adversary(
    payload: object,
    *,
    backend: SolverBackend | None = None,
    time_limit_ms: int,
) -> dict[str, object]:
    """Solve one canonical fill-adversary problem into canonical evidence."""
    if type(time_limit_ms) is not int or time_limit_ms <= 0:
        raise ValueError("time_limit_ms must be a positive integer")
    problem = fill_adversary_problem_from_payload(payload)
    compiled = compile_fill_adversary(problem)
    if backend is None:
        backend = _default_backend()
    result = _checked_backend_solve(compiled.model, backend, time_limit_ms)
    if result.status == NativeSolveStatus.OPTIMAL:
        values = dict(result.values)
        evidence = FillAdversaryEvidence(
            FILL_ADVERSARY_EVIDENCE_SCHEMA_V1,
            problem,
            backend.name,
            backend.version,
            TERMINATION_CLOSED,
            result.objective_value,
            tuple(
                ActionQuantity(action_id, values[name])
                for action_id, name in compiled.fill_variables
            ),
            _scenario_from_values(problem, values),
        )
    else:
        evidence = FillAdversaryEvidence(
            FILL_ADVERSARY_EVIDENCE_SCHEMA_V1,
            problem,
            backend.name,
            backend.version,
            f"UNCLOSED:{result.status.value}",
            None,
            (),
            None,
        )
    return evidence.to_payload()


def verify_fill_adversary(
    payload: object,
    *,
    backend: SolverBackend | None = None,
    time_limit_ms: int,
) -> dict[str, object]:
    """Independently rebuild the adversary from the evidence and recompute.

    Re-solves from the canonical problem payload, cross-checks the optimal
    value and termination against the solver evidence, and recomputes the
    solver's counterexample loss in pure fixed-point arithmetic.
    """
    try:
        evidence = fill_adversary_evidence_from_payload(payload)
        problem = evidence.problem
        compiled = compile_fill_adversary(problem)
        if backend is None:
            backend = _default_backend()
        result = _checked_backend_solve(compiled.model, backend, time_limit_ms)
        verifier_termination = (
            TERMINATION_CLOSED
            if result.status == NativeSolveStatus.OPTIMAL
            else f"UNCLOSED:{result.status.value}"
        )
        verifier_worst = (
            result.objective_value
            if result.status == NativeSolveStatus.OPTIMAL
            else None
        )
        if evidence.termination == TERMINATION_CLOSED:
            counterexample_loss = counterexample_loss_units(
                problem,
                evidence.worst_fill_quantities,
                evidence.worst_scenario,
            )
        else:
            counterexample_loss = None
        if (
            evidence.termination != verifier_termination
            or evidence.worst_loss_units != verifier_worst
        ):
            status = "MISMATCH"
            reason = "verifier and solver disagree on the worst loss"
        elif evidence.termination != TERMINATION_CLOSED:
            status = "QUALIFIED_VERIFIED"
            reason = None
        elif counterexample_loss != evidence.worst_loss_units:
            status = "MISMATCH"
            reason = "counterexample fixed-point recomputation disagrees"
        else:
            status = VERIFIER_QUALIFIED
            reason = None
        return canonical_payload(
            FillAdversaryVerification(
                FILL_ADVERSARY_VERIFICATION_SCHEMA_V1,
                status,
                problem.fingerprint,
                evidence.termination,
                evidence.worst_loss_units,
                verifier_termination,
                verifier_worst,
                counterexample_loss,
                evidence.worst_fill_quantities,
                evidence.worst_scenario,
                reason,
            )
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return canonical_payload(
            FillAdversaryVerification(
                FILL_ADVERSARY_VERIFICATION_SCHEMA_V1,
                "INVALID",
                "",
                "",
                None,
                "",
                None,
                None,
                (),
                None,
                str(exc),
            )
        )


def _proof_record(fields: Mapping[str, object]) -> PartialFillProofRecord:
    """Assemble a PartialFillProofRecord with its self-verifying fingerprint."""
    values = dict(fields)
    values["schema_version"] = PARTIAL_FILL_PROOF_SCHEMA_V1
    values["fingerprint"] = fingerprint(values)
    return PartialFillProofRecord(**values)  # type: ignore[arg-type]


def _unknown_record(
    problem: FillAdversaryProblem, reason: str
) -> PartialFillProofRecord:
    evidence = FillAdversaryEvidence(
        FILL_ADVERSARY_EVIDENCE_SCHEMA_V1,
        problem,
        "cp_sat",
        "unavailable",
        f"UNKNOWN:{reason}",
        None,
        (),
        None,
    )
    verification = FillAdversaryVerification(
        FILL_ADVERSARY_VERIFICATION_SCHEMA_V1,
        "NOT_APPLICABLE",
        problem.fingerprint,
        f"UNKNOWN:{reason}",
        None,
        "",
        None,
        None,
        (),
        None,
        reason,
    )
    evidence_payload = evidence.to_payload()
    verification_payload = canonical_payload(verification)
    return _proof_record(
        {
            "execution_solution_fingerprint": problem.execution_solution_fingerprint,
            "execution_solution_payload_fingerprint": (
                problem.execution_solution_payload_fingerprint
            ),
            "model_fingerprint": problem.model_fingerprint,
            "quote_fingerprint": problem.quote_fingerprint,
            "cost_fingerprint": problem.cost_fingerprint,
            "order_semantics_fingerprint": problem.order_semantics_fingerprint,
            "cap_config_version": problem.cap_config_version,
            "max_partial_fill_loss": problem.max_partial_fill_loss,
            "max_auto_repair_loss": problem.max_auto_repair_loss,
            "solver_lower_bound": 0,
            "solver_upper_bound": 0,
            "solver_termination": f"UNKNOWN:{reason}",
            "solver_evidence_fingerprint": fingerprint(evidence_payload),
            "verifier_status": VERIFIER_NOT_APPLICABLE,
            "verifier_fingerprint": fingerprint(verification_payload),
            "verifier_evidence_fingerprint": fingerprint(verification_payload),
            "status": PARTIAL_FILL_UNKNOWN,
        }
    )


def prove_partial_fill(
    problem: FillAdversaryProblem | Mapping[str, object],
    *,
    backend: SolverBackend | None = None,
    time_limit_ms: int,
) -> tuple[PartialFillProofRecord, dict[str, object] | None]:
    """The three-state partial-fill proof entry for one fixed solution.

    Returns ``(record, counterexample)``; the counterexample is only present
    for UNSAFE (fill vector + joint atoms + fixed-point verified loss).
    Raises ``ValueError`` only for a structurally invalid problem payload.
    """
    if type(time_limit_ms) is not int or time_limit_ms <= 0:
        raise ValueError("time_limit_ms must be a positive integer")
    if isinstance(problem, FillAdversaryProblem):
        decoded = problem
    else:
        decoded = fill_adversary_problem_from_payload(problem)
    if any(leg.semantics == FillSemantics.UNKNOWN.value for leg in decoded.legs):
        return _unknown_record(decoded, "UNKNOWN_ORDER_SEMANTICS"), None
    try:
        compiled = compile_fill_adversary(decoded)
    except (TypeError, ValueError, OverflowError) as exc:
        return _unknown_record(decoded, f"INVALID_INPUT:{exc}"), None
    if backend is None:
        try:
            backend = _default_backend()
        except ModuleNotFoundError:
            return _unknown_record(decoded, "BACKEND_UNAVAILABLE"), None
    try:
        result = _checked_backend_solve(compiled.model, backend, time_limit_ms)
    except ModuleNotFoundError:
        return _unknown_record(decoded, "BACKEND_UNAVAILABLE"), None
    except (TypeError, ValueError, OverflowError) as exc:
        return _unknown_record(decoded, f"INVALID_INPUT:{exc}"), None
    if result.status != NativeSolveStatus.OPTIMAL:
        reason = (
            "TIMEOUT"
            if result.status == NativeSolveStatus.UNKNOWN
            else "UNCLOSED"
        )
        return _unknown_record(decoded, reason), None
    values = dict(result.values)
    worst_loss = result.objective_value
    evidence = FillAdversaryEvidence(
        FILL_ADVERSARY_EVIDENCE_SCHEMA_V1,
        decoded,
        backend.name,
        backend.version,
        TERMINATION_CLOSED,
        worst_loss,
        tuple(
            ActionQuantity(action_id, values[name])
            for action_id, name in compiled.fill_variables
        ),
        _scenario_from_values(decoded, values),
    )
    evidence_payload = evidence.to_payload()
    try:
        verification = fill_adversary_verification_from_payload(
            verify_fill_adversary(
                evidence_payload, backend=backend, time_limit_ms=time_limit_ms
            )
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return _unknown_record(decoded, f"VERIFIER_MISMATCH:{exc}"), None
    if verification.status != VERIFIER_QUALIFIED:
        return _unknown_record(decoded, "VERIFIER_MISMATCH"), None
    counterexample_loss = verification.counterexample_loss_units
    verification_payload = canonical_payload(verification)
    if worst_loss is None or counterexample_loss is None:
        return _unknown_record(decoded, "VERIFIER_MISMATCH"), None
    if worst_loss <= decoded.max_partial_fill_loss:
        status = PARTIAL_FILL_SAFE
        counterexample = None
    elif counterexample_loss > decoded.max_partial_fill_loss:
        status = PARTIAL_FILL_UNSAFE
        filled = tuple(
            quantity
            for quantity in evidence.worst_fill_quantities
            if quantity.quantity_lots > 0
        )
        scenario_atoms = tuple(
            {
                "market_contract_id": atom.market_contract_id,
                "atom_id": atom.atom_id,
            }
            for atom in evidence.worst_scenario.atoms
        )
        counterexample = {
            "fill_quantities": [
                {"action_id": quantity.action_id, "quantity_lots": quantity.quantity_lots}
                for quantity in filled
            ],
            "scenario": list(scenario_atoms),
            "loss_units": counterexample_loss,
            "cap_units": decoded.max_partial_fill_loss,
            "fingerprint": fingerprint(
                {
                    "fill_quantities": filled,
                    "scenario": evidence.worst_scenario,
                    "loss_units": counterexample_loss,
                    "cap_units": decoded.max_partial_fill_loss,
                }
            ),
        }
    else:
        # The verifier agreed on the optimum but the fixed-point recomputation
        # did not exceed the cap while the solver optimum did: fail closed.
        return _unknown_record(decoded, "VERIFIER_MISMATCH"), None
    record = _proof_record(
        {
            "execution_solution_fingerprint": decoded.execution_solution_fingerprint,
            "execution_solution_payload_fingerprint": (
                decoded.execution_solution_payload_fingerprint
            ),
            "model_fingerprint": decoded.model_fingerprint,
            "quote_fingerprint": decoded.quote_fingerprint,
            "cost_fingerprint": decoded.cost_fingerprint,
            "order_semantics_fingerprint": decoded.order_semantics_fingerprint,
            "cap_config_version": decoded.cap_config_version,
            "max_partial_fill_loss": decoded.max_partial_fill_loss,
            "max_auto_repair_loss": decoded.max_auto_repair_loss,
            "solver_lower_bound": worst_loss,
            "solver_upper_bound": worst_loss,
            "solver_termination": TERMINATION_CLOSED,
            "solver_evidence_fingerprint": fingerprint(evidence_payload),
            "verifier_status": VERIFIER_QUALIFIED,
            "verifier_fingerprint": fingerprint(verification_payload),
            "verifier_evidence_fingerprint": fingerprint(
                {
                    "termination": verification.verifier_termination,
                    "worst_loss_units": verification.verifier_worst_loss_units,
                    "counterexample_loss_units": (
                        verification.counterexample_loss_units
                    ),
                    "fills": verification.worst_fill_quantities,
                    "scenario": verification.worst_scenario,
                }
            ),
            "status": status,
        }
    )
    return record, counterexample


def fill_adversary_problem_from_execution_solution(
    solution: object,
    source_problem: ArbitrageProblem,
    *,
    cap_config_version: str,
    max_partial_fill_loss: int,
    max_auto_repair_loss: int,
) -> FillAdversaryProblem:
    """Build the adversary from a #51 heavy ExecutionSolution handoff.

    The six binding facts come from ``execution_solution_binding()``; per-leg
    max_cost/max_fee come from the ExecutionLegEvidence.
    """
    if not isinstance(solution, _HeavyExecutionSolution):
        raise ValueError("fill adversary requires a #51 execution solution")
    binding = execution_solution_binding(solution)
    actions = {action.action_id: action for action in source_problem.actions}
    legs: list[FillLeg] = []
    for leg in solution.execution_legs:
        action = actions.get(leg.action_id)
        if action is None:
            raise ValueError(
                f"execution leg references unknown action: {leg.action_id}"
            )
        order_type = default_order_type(leg.venue_id)
        if order_type is None:
            order_type = "UNKNOWN"
        legs.append(
            FillLeg(
                leg.action_id,
                leg.venue_id,
                leg.quantity_lots,
                order_type,
                order_semantics_lookup(leg.venue_id, order_type).value,
                leg.max_cost_units,
                leg.max_fee_units,
            )
        )
    if not legs:
        raise ValueError("execution solution has no legs to prove")
    return fill_adversary_problem(
        execution_solution_fingerprint=binding["execution_solution_fingerprint"],
        execution_solution_payload_fingerprint=binding[
            "execution_solution_payload_fingerprint"
        ],
        model_fingerprint=binding["model_fingerprint"],
        quote_fingerprint=binding["quote_fingerprint"],
        cost_fingerprint=binding["cost_fingerprint"],
        order_semantics_fingerprint=order_semantics_fingerprint_for(
            tuple(
                (leg.action_id, leg.venue_id, leg.order_type, leg.semantics)
                for leg in legs
            )
        ),
        cap_config_version=cap_config_version,
        max_partial_fill_loss=max_partial_fill_loss,
        max_auto_repair_loss=max_auto_repair_loss,
        legs=tuple(legs),
        source_problem=source_problem,
    )


def fill_adversary_problem_from_market_solution(
    execution: object,
    source_problem: ArbitrageProblem,
    *,
    cap_config_version: str,
    max_partial_fill_loss: int,
    max_auto_repair_loss: int,
) -> FillAdversaryProblem:
    """Build the adversary from a #84 light MarketSolution interpretation.

    Used by the validation harness and the live resolver, which only have the
    light execution solution.  Per-leg max_cost is the same conservative
    ``cost_upper_bound`` the execution's capital_use was derived from; the
    light pipeline keeps fee/haircut/tick at zero, so max_fee is 0.  The six
    binding facts are recomputed from the solution's stable fields.
    """
    if not isinstance(execution, _MarketExecutionSolution):
        raise ValueError("fill adversary requires a #84 execution solution")
    actions = {action.action_id: action for action in source_problem.actions}
    legs: list[FillLeg] = []
    for quantity in execution.quantities:
        if quantity.quantity_lots <= 0:
            continue
        action = actions.get(quantity.action_id)
        if action is None or not action.cost_slices:
            raise ValueError(
                f"market solution references unknown action {quantity.action_id}"
            )
        order_type = default_order_type(action.venue_id)
        if order_type is None:
            order_type = "UNKNOWN"
        max_cost = cost_upper_bound(source_problem, (quantity,))
        legs.append(
            FillLeg(
                quantity.action_id,
                action.venue_id,
                quantity.quantity_lots,
                order_type,
                order_semantics_lookup(action.venue_id, order_type).value,
                max_cost,
                0,
            )
        )
    if not legs:
        raise ValueError("market solution has no positive legs to prove")
    stable_payload = {
        key: value
        for key, value in canonical_payload(execution).items()
        if key != "partial_fill_proof"
    }
    execution_fingerprint = fingerprint(
        {
            "market": execution.market_solution_fingerprint,
            "quantities": execution.quantities,
            "capital_use_units": execution.capital_use_units,
            "reason": execution.reason,
        }
    )
    return fill_adversary_problem(
        execution_solution_fingerprint=execution_fingerprint,
        execution_solution_payload_fingerprint=fingerprint(stable_payload),
        model_fingerprint=execution.market_solution_fingerprint,
        quote_fingerprint=fingerprint(
            {"sources": tuple(leg.action_id for leg in legs)}
        ),
        cost_fingerprint=fingerprint(
            {
                "capital_use_units": execution.capital_use_units,
                "legs": tuple(
                    (leg.action_id, leg.max_cost_units, leg.max_fee_units)
                    for leg in legs
                ),
            }
        ),
        order_semantics_fingerprint=order_semantics_fingerprint_for(
            tuple(
                (leg.action_id, leg.venue_id, leg.order_type, leg.semantics)
                for leg in legs
            )
        ),
        cap_config_version=cap_config_version,
        max_partial_fill_loss=max_partial_fill_loss,
        max_auto_repair_loss=max_auto_repair_loss,
        legs=tuple(legs),
        source_problem=source_problem,
    )


@dataclass(frozen=True, slots=True)
class FillAdversaryOracleResult:
    """Bounded exact fill-domain oracle result (over budget -> UNKNOWN)."""

    closed: bool
    unknown_reason: str | None
    worst_loss_units: int | None
    worst_fill_quantities: tuple[ActionQuantity, ...] | None
    worst_scenario: SettlementScenario | None


def enumerate_fill_adversary(
    problem: FillAdversaryProblem, budget: OracleBudget
) -> FillAdversaryOracleResult:
    """Exhaustively enumerate fill vectors x joint atoms within the budget.

    Never runs in production; the differential tests use it to confirm that a
    production SAFE proof is not false-safe and that an UNSAFE proof's worst
    loss is reproduced exactly.
    """
    if not isinstance(problem, FillAdversaryProblem) or not isinstance(
        budget, OracleBudget
    ):
        raise ValueError("fill adversary oracle requires problem and budget")
    if any(leg.semantics == FillSemantics.UNKNOWN.value for leg in problem.legs):
        return FillAdversaryOracleResult(
            False, "UNKNOWN_ORDER_SEMANTICS", None, None, None
        )
    enumeration = enumerate_allowed_scenarios(problem.source_problem, budget)
    if enumeration.scenarios is None:
        reason = (
            enumeration.unknown_reason.value
            if enumeration.unknown_reason is not None
            else "SCENARIO_ENUMERATION_FAILED"
        )
        return FillAdversaryOracleResult(False, reason, None, None, None)
    fill_domains: list[tuple[int, ...]] = []
    vector_count = 1
    try:
        for leg in problem.legs:
            if leg.semantics == FillSemantics.ATOMIC.value:
                domain = (0, leg.quantity_lots)
            else:
                domain = tuple(range(0, leg.quantity_lots + 1))
            fill_domains.append(domain)
            vector_count *= len(domain)
            if vector_count > budget.max_quantity_vectors:
                return FillAdversaryOracleResult(
                    False, "ORACLE_FILL_VECTOR_LIMIT_EXCEEDED", None, None, None
                )
    except OverflowError:
        return FillAdversaryOracleResult(
            False, "NUMERIC_OVERFLOW", None, None, None
        )
    best = 0
    best_fills: tuple[ActionQuantity, ...] = ()
    best_scenario: SettlementScenario | None = None
    for fills in product(*fill_domains):
        fill_quantities = tuple(
            ActionQuantity(leg.action_id, filled)
            for leg, filled in zip(problem.legs, fills, strict=True)
        )
        for scenario in enumeration.scenarios:
            loss = counterexample_loss_units(problem, fill_quantities, scenario)
            if loss > best:
                best = loss
                best_fills = fill_quantities
                best_scenario = scenario
    return FillAdversaryOracleResult(
        True, None, best, best_fills, best_scenario
    )


__all__ = [
    "FILL_ADVERSARY_SCHEMA_V1",
    "FILL_ADVERSARY_EVIDENCE_SCHEMA_V1",
    "FILL_ADVERSARY_VERIFICATION_SCHEMA_V1",
    "ORDER_SEMANTICS_SCHEMA_V1",
    "FILL_ADVERSARY_PROBLEM_KIND",
    "PARTIAL_FILL_SAFE",
    "PARTIAL_FILL_UNSAFE",
    "PARTIAL_FILL_UNKNOWN",
    "TERMINATION_CLOSED",
    "VERIFIER_QUALIFIED",
    "FillSemantics",
    "ORDER_SEMANTICS_TABLE_V1",
    "DEFAULT_ORDER_TYPES_V1",
    "FillLeg",
    "FillAdversaryProblem",
    "CompiledFillAdversary",
    "FillAdversaryEvidence",
    "FillAdversaryVerification",
    "FillAdversaryOracleResult",
    "order_semantics_lookup",
    "default_order_type",
    "order_semantics_fingerprint_for",
    "fill_adversary_problem",
    "fill_adversary_problem_from_payload",
    "fill_adversary_evidence_from_payload",
    "fill_adversary_verification_from_payload",
    "compile_fill_adversary",
    "counterexample_loss_units",
    "solve_fill_adversary",
    "verify_fill_adversary",
    "prove_partial_fill",
    "fill_adversary_problem_from_execution_solution",
    "fill_adversary_problem_from_market_solution",
    "enumerate_fill_adversary",
]
