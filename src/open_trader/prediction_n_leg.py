from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ActionSide(StrEnum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"


class TerminalKind(StrEnum):
    NORMAL_YES = "NORMAL_YES"
    NORMAL_NO = "NORMAL_NO"
    VOID = "VOID"
    REFUND = "REFUND"
    SPLIT = "SPLIT"


class RelationKind(StrEnum):
    IMPLIES = "IMPLIES"
    MUTUALLY_EXCLUSIVE = "MUTUALLY_EXCLUSIVE"
    EXACTLY_ONE = "EXACTLY_ONE"


class QualificationMetric(StrEnum):
    GUARANTEED_PROFIT_UNITS = "GUARANTEED_PROFIT_UNITS"
    NET_MARGIN_PPM = "NET_MARGIN_PPM"
    ANNUALIZED_RETURN_PPM = "ANNUALIZED_RETURN_PPM"
    MAX_CAPITAL_RELEASE_DELAY_SECONDS = "MAX_CAPITAL_RELEASE_DELAY_SECONDS"


class Comparison(StrEnum):
    GREATER_THAN_OR_EQUAL = "GREATER_THAN_OR_EQUAL"
    LESS_THAN_OR_EQUAL = "LESS_THAN_OR_EQUAL"


class SearchMode(StrEnum):
    ADMISSION = "ADMISSION"
    OPTIMIZATION = "OPTIMIZATION"
    RAW_ARBITRAGE_DIAGNOSTIC = "RAW_ARBITRAGE_DIAGNOSTIC"


class SolveStatus(StrEnum):
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"


class ProofStatus(StrEnum):
    PROVEN = "PROVEN"
    UNKNOWN = "UNKNOWN"


class BusinessStatus(StrEnum):
    QUALIFIED_FEASIBLE = "QUALIFIED_FEASIBLE"
    NO_QUALIFIED_OPPORTUNITY = "NO_QUALIFIED_OPPORTUNITY"
    NO_ARBITRAGE = "NO_ARBITRAGE"
    UNKNOWN = "UNKNOWN"


class OptimalityStatus(StrEnum):
    OPTIMAL = "OPTIMAL"
    NOT_PROVEN = "NOT_PROVEN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class UnknownReason(StrEnum):
    INVALID_MODEL = "INVALID_MODEL"
    UNKNOWN_TERMINAL_DATA = "UNKNOWN_TERMINAL_DATA"
    UNKNOWN_VALUATION = "UNKNOWN_VALUATION"
    NUMERIC_OVERFLOW = "NUMERIC_OVERFLOW"
    CONTRADICTORY_CONSTRAINT_MODEL = "CONTRADICTORY_CONSTRAINT_MODEL"
    ORACLE_DECISION_LIMIT_EXCEEDED = "ORACLE_DECISION_LIMIT_EXCEEDED"
    ORACLE_STATE_LIMIT_EXCEEDED = "ORACLE_STATE_LIMIT_EXCEEDED"
    ORACLE_SUPPORT_LIMIT_EXCEEDED = "ORACLE_SUPPORT_LIMIT_EXCEEDED"


@dataclass(frozen=True, slots=True)
class SettlementObservationKey:
    schema_version: str
    oracle_id: str
    indicator_id: str
    observation_start: datetime
    observation_end: datetime
    timezone: str
    rule_version: str


@dataclass(frozen=True, slots=True)
class ExecutableCostSlice:
    first_lot: int
    last_lot: int
    incremental_cost_upper_bound_units: int


@dataclass(frozen=True, slots=True)
class CandidateAction:
    action_id: str
    market_contract_id: str
    settlement_observation_key: SettlementObservationKey
    side: ActionSide
    lot_step_units: int
    quantity_scale: int
    settlement_asset_id: str
    valuation_unit_id: str
    asset_valuation_rule_id: str
    cost_slices: tuple[ExecutableCostSlice, ...]


@dataclass(frozen=True, slots=True)
class ActionPayout:
    action_id: str
    payout_lower_bound_per_lot_units: int


@dataclass(frozen=True, slots=True)
class TerminalAtom:
    atom_id: str
    kind: TerminalKind
    rule_version: str
    payouts: tuple[ActionPayout, ...]
    capital_release_at: datetime


@dataclass(frozen=True, slots=True)
class TerminalStateSet:
    market_contract_id: str
    settlement_observation_key: SettlementObservationKey
    rule_version: str
    atoms: tuple[TerminalAtom, ...]


@dataclass(frozen=True, slots=True)
class RelationConstraint:
    constraint_id: str
    kind: RelationKind
    contract_ids: tuple[str, ...]
    rule_version: str


@dataclass(frozen=True, slots=True)
class ForbiddenAtomCombination:
    constraint_id: str
    atom_ids: tuple[str, ...]
    rule_version: str


@dataclass(frozen=True, slots=True)
class ConstraintModel:
    relations: tuple[RelationConstraint, ...]
    forbidden_atom_combinations: tuple[ForbiddenAtomCombination, ...]


@dataclass(frozen=True, slots=True)
class QualificationConstraint:
    constraint_id: str
    rule_version: str
    metric: QualificationMetric
    comparison: Comparison
    threshold_numerator: int
    threshold_denominator: int


@dataclass(frozen=True, slots=True)
class ArbitrageProblem:
    schema_version: str
    problem_id: str
    as_of: datetime
    valuation_unit_id: str
    actions: tuple[CandidateAction, ...]
    terminal_state_sets: tuple[TerminalStateSet, ...]
    constraint_model: ConstraintModel
    qualification_constraints: tuple[QualificationConstraint, ...]


@dataclass(frozen=True, slots=True)
class ActionQuantity:
    action_id: str
    quantity_lots: int


@dataclass(frozen=True, slots=True)
class PortfolioCandidate:
    quantities: tuple[ActionQuantity, ...]
    claimed_guaranteed_profit_units: int


@dataclass(frozen=True, slots=True)
class SelectedAtom:
    market_contract_id: str
    atom_id: str


@dataclass(frozen=True, slots=True)
class SettlementScenario:
    atoms: tuple[SelectedAtom, ...]


@dataclass(frozen=True, slots=True)
class WorstStateCut:
    cut_id: str
    scenario: SettlementScenario
    payout_per_lot: tuple[ActionPayout, ...]


@dataclass(frozen=True, slots=True)
class SelectedSupportGraph:
    action_ids: tuple[str, ...]
    contract_ids: tuple[str, ...]
    constraint_ids: tuple[str, ...]
    hyperedges: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class PayoutProof:
    problem_fingerprint: str
    portfolio_fingerprint: str
    worst_scenario: SettlementScenario
    worst_state_cut: WorstStateCut
    payout_lower_bound_units: int
    cost_upper_bound_units: int
    guaranteed_profit_units: int
    conservative_capital_release_at: datetime
    selected_support_graph: SelectedSupportGraph


@dataclass(frozen=True, slots=True)
class PortfolioSolution:
    quantities: tuple[ActionQuantity, ...]
    payout_proof: PayoutProof


@dataclass(frozen=True, slots=True)
class ObjectiveBounds:
    lower_bound_units: int | None
    upper_bound_units: int | None
    gap_units: int | None
    closed: bool


@dataclass(frozen=True, slots=True)
class OracleBudget:
    max_quantity_vectors: int
    max_joint_states: int
    max_support_rechecks: int


@dataclass(frozen=True, slots=True)
class OracleRequest:
    schema_version: str
    mode: SearchMode
    problem: ArbitrageProblem
    budget: OracleBudget


@dataclass(frozen=True, slots=True)
class ExhaustiveSearchProof:
    proof_method: str
    conclusion: BusinessStatus
    request_fingerprint: str
    problem_fingerprint: str
    source_problem_fingerprint: str | None
    qualification_fingerprint: str
    quantity_vectors_total: int
    quantity_vectors_examined: int
    joint_states_per_vector: int
    rejection_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class OracleResult:
    solve_status: SolveStatus
    proof_status: ProofStatus
    business_status: BusinessStatus
    optimality_status: OptimalityStatus
    objective_bounds: ObjectiveBounds
    solution: PortfolioSolution | None
    negative_proof: ExhaustiveSearchProof | None
    unknown_reason: UnknownReason | None
