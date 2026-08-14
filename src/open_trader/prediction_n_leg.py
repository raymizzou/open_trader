from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum


REQUEST_SCHEMA_V1 = "open_trader.prediction_n_leg.request.v1"
PROBLEM_SCHEMA_V1 = "open_trader.prediction_n_leg.problem.v1"
OBSERVATION_SCHEMA_V1 = "open_trader.prediction_n_leg.observation.v1"
PAYOUT_PROOF_SCHEMA_V1 = "open_trader.prediction_n_leg.payout_proof.v1"


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


class ProofResultKind(StrEnum):
    PORTFOLIO = "PORTFOLIO"
    NO_QUALIFIED_OPPORTUNITY = "NO_QUALIFIED_OPPORTUNITY"
    NO_ARBITRAGE = "NO_ARBITRAGE"


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
    venue_id: str
    account_id: str
    chain_id: str
    market_contract_id: str
    settlement_observation_key: SettlementObservationKey
    side: ActionSide
    lot_step_units: int
    quantity_scale: int
    min_quantity_lots: int
    max_quantity_lots: int
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
    rule_version: str | None
    payouts: tuple[ActionPayout, ...]
    capital_release_at: datetime | None


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
    schema_version: str
    result_kind: ProofResultKind
    problem_fingerprint: str
    portfolio_fingerprint: str | None
    worst_scenario: SettlementScenario | None
    worst_state_cut: WorstStateCut | None
    payout_lower_bound_units: int | None
    cost_upper_bound_units: int | None
    guaranteed_profit_units: int | None
    conservative_capital_release_at: datetime | None
    selected_support_graph: SelectedSupportGraph | None
    proof_method: str
    request_fingerprint: str | None
    source_problem_fingerprint: str | None
    qualification_fingerprint: str | None
    quantity_vectors_total: int | None
    quantity_vectors_examined: int | None
    joint_states_per_vector: int | None
    rejection_counts: tuple[tuple[str, int], ...]


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
class OracleResult:
    solve_status: SolveStatus
    proof_status: ProofStatus
    business_status: BusinessStatus
    optimality_status: OptimalityStatus
    objective_bounds: ObjectiveBounds
    solution: PortfolioSolution | None
    negative_proof: PayoutProof | None
    unknown_reason: UnknownReason | None


@dataclass(frozen=True, slots=True)
class ModelIssue:
    code: str
    path: str
    message: str


class ModelDecodeError(ValueError):
    pass


_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("canonical datetimes must be UTC-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sort_values(owner: object, name: str, values: list[object]) -> list[object]:
    if isinstance(owner, RelationConstraint) and name == "contract_ids":
        return values if owner.kind == RelationKind.IMPLIES else sorted(values)
    if isinstance(owner, SettlementScenario) and name == "atoms":
        return sorted(values, key=lambda value: value["market_contract_id"])
    if name in {"action_ids", "contract_ids", "constraint_ids", "atom_ids"}:
        return sorted(values)
    stable_ids = {
        "actions": "action_id",
        "terminal_state_sets": "market_contract_id",
        "relations": "constraint_id",
        "forbidden_atom_combinations": "constraint_id",
        "qualification_constraints": "constraint_id",
        "atoms": "atom_id",
        "payouts": "action_id",
        "quantities": "action_id",
        "payout_per_lot": "action_id",
        "rejection_counts": None,
        "hyperedges": None,
    }
    key = stable_ids.get(name, ...)
    if key is ...:
        return values
    if key is None:
        return sorted(values, key=lambda value: value[0])
    return sorted(values, key=lambda value: value[key])


def _canonical_value(value: object, owner: object | None = None, name: str = "") -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name), value, field.name)
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical mappings require string keys")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        values = [_canonical_value(item) for item in value]
        return _sort_values(owner, name, values) if owner is not None else values
    if isinstance(value, float):
        raise ValueError("canonical models do not permit floats")
    if value is None or isinstance(value, str | int | bool):
        return value
    raise ValueError(f"unsupported canonical value: {type(value).__name__}")


def canonical_payload(value: object) -> dict[str, object]:
    payload = _canonical_value(value)
    if not isinstance(payload, dict):
        raise ValueError("canonical payload must be an object")
    return payload


def canonical_json(value: object) -> str:
    return json.dumps(canonical_payload(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode()).hexdigest()}"


def _issue(issues: list[ModelIssue], code: str, path: str, message: str) -> None:
    issues.append(ModelIssue(code, path, message))


def _validate_int(issues: list[ModelIssue], value: object, path: str) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        _issue(issues, "INVALID_INTEGER", path, "must be a signed 64-bit integer")
        return False
    if not _INT64_MIN <= value <= _INT64_MAX:
        _issue(issues, "INTEGER_OUT_OF_RANGE", path, "must fit signed 64-bit range")
        return False
    return True


def _validate_product(issues: list[ModelIssue], left: int, right: int, path: str) -> None:
    if not _INT64_MIN <= left * right <= _INT64_MAX:
        _issue(issues, "DERIVED_INTEGER_OUT_OF_RANGE", path, "derived product must fit signed 64-bit range")


def _validate_datetime(issues: list[ModelIssue], value: object, path: str) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _issue(issues, "NAIVE_DATETIME", path, "must be UTC-aware")
        return False
    if value.utcoffset() != UTC.utcoffset(value):
        _issue(issues, "NON_UTC_DATETIME", path, "must be UTC")
        return False
    return True


def _validate_enum(issues: list[ModelIssue], value: object, enum_type: type[StrEnum], code: str, path: str) -> bool:
    if not isinstance(value, enum_type):
        _issue(issues, code, path, f"must be a {enum_type.__name__}")
        return False
    return True


def _nodes(issues: list[ModelIssue], value: object, code: str, path: str) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        _issue(issues, code, path, "must be a tuple")
        return ()
    return value


def _identifier(issues: list[ModelIssue], value: object, path: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        _issue(issues, "INVALID_IDENTIFIER", path, "must be a non-empty string")
        return None
    return value


def _validate_observation_key(issues: list[ModelIssue], value: object, path: str) -> bool:
    if not isinstance(value, SettlementObservationKey):
        _issue(issues, "INVALID_SETTLEMENT_OBSERVATION_KEY", path, "must be a SettlementObservationKey")
        return False
    for field in ("schema_version", "oracle_id", "indicator_id", "timezone", "rule_version"):
        _identifier(issues, getattr(value, field), f"{path}.{field}")
    if value.schema_version != OBSERVATION_SCHEMA_V1:
        _issue(issues, "INVALID_SCHEMA_VERSION", f"{path}.schema_version", f"must equal {OBSERVATION_SCHEMA_V1}")
    return True


def validate_problem(problem: ArbitrageProblem) -> tuple[ModelIssue, ...]:
    issues: list[ModelIssue] = []
    if not isinstance(problem, ArbitrageProblem):
        return (ModelIssue("INVALID_PROBLEM", "$", "must be an ArbitrageProblem"),)
    as_of_valid = _validate_datetime(issues, problem.as_of, "as_of")
    _identifier(issues, problem.schema_version, "schema_version")
    if problem.schema_version != PROBLEM_SCHEMA_V1:
        _issue(issues, "INVALID_SCHEMA_VERSION", "schema_version", f"must equal {PROBLEM_SCHEMA_V1}")
    _identifier(issues, problem.problem_id, "problem_id")
    valuation_unit_id = _identifier(issues, problem.valuation_unit_id, "valuation_unit_id")
    action_ids: set[str] = set()
    actions_by_contract: dict[str, list[CandidateAction]] = {}
    for action_index, action in enumerate(_nodes(issues, problem.actions, "INVALID_ACTION_CONTAINER", "actions")):
        prefix = f"actions[{action_index}]"
        if not isinstance(action, CandidateAction):
            _issue(issues, "INVALID_ACTION", prefix, "must be a CandidateAction")
            continue
        action_id = _identifier(issues, action.action_id, f"{prefix}.action_id")
        for name in ("venue_id", "account_id", "chain_id"):
            _identifier(issues, getattr(action, name), f"{prefix}.{name}")
        contract_id = _identifier(issues, action.market_contract_id, f"{prefix}.market_contract_id")
        if action_id is not None and action_id in action_ids:
            _issue(issues, "DUPLICATE_ID", f"{prefix}.action_id", "action_id must be unique")
        if action_id is not None:
            action_ids.add(action_id)
        if action_id is not None and contract_id is not None:
            actions_by_contract.setdefault(contract_id, []).append(action)
        _identifier(issues, action.settlement_asset_id, f"{prefix}.settlement_asset_id")
        action_valuation_unit_id = _identifier(issues, action.valuation_unit_id, f"{prefix}.valuation_unit_id")
        if action_valuation_unit_id != valuation_unit_id:
            _issue(issues, "VALUATION_UNIT_MISMATCH", f"{prefix}.valuation_unit_id", "must equal problem valuation_unit_id")
        _identifier(issues, action.asset_valuation_rule_id, f"{prefix}.asset_valuation_rule_id")
        _validate_enum(issues, action.side, ActionSide, "INVALID_ACTION_SIDE", f"{prefix}.side")
        for name in ("lot_step_units", "quantity_scale", "min_quantity_lots", "max_quantity_lots"):
            _validate_int(issues, getattr(action, name), f"{prefix}.{name}")
        if isinstance(action.lot_step_units, int) and not isinstance(action.lot_step_units, bool) and action.lot_step_units <= 0:
            _issue(issues, "NON_POSITIVE_LOT_STEP", f"{prefix}.lot_step_units", "must be positive")
        if isinstance(action.quantity_scale, int) and not isinstance(action.quantity_scale, bool) and action.quantity_scale <= 0:
            _issue(issues, "NON_POSITIVE_QUANTITY_SCALE", f"{prefix}.quantity_scale", "must be positive")
        if isinstance(action.lot_step_units, int) and isinstance(action.quantity_scale, int) and not isinstance(action.lot_step_units, bool) and not isinstance(action.quantity_scale, bool):
            _validate_product(issues, action.lot_step_units, action.quantity_scale, f"{prefix}.lot_step_units*quantity_scale")
        if action.settlement_asset_id != problem.valuation_unit_id and (not isinstance(action.asset_valuation_rule_id, str) or not action.asset_valuation_rule_id.strip()):
            _issue(issues, "MISSING_ASSET_VALUATION_RULE", f"{prefix}.asset_valuation_rule_id", "non-native settlement assets require a versioned valuation rule")
        if _validate_observation_key(issues, action.settlement_observation_key, f"{prefix}.settlement_observation_key"):
            observation_start_valid = _validate_datetime(issues, action.settlement_observation_key.observation_start, f"{prefix}.settlement_observation_key.observation_start")
            observation_end_valid = _validate_datetime(issues, action.settlement_observation_key.observation_end, f"{prefix}.settlement_observation_key.observation_end")
            if observation_start_valid and observation_end_valid and action.settlement_observation_key.observation_start > action.settlement_observation_key.observation_end:
                _issue(issues, "INVALID_OBSERVATION_WINDOW", f"{prefix}.settlement_observation_key", "observation_start must not be after observation_end")
        previous_last = 0
        cost_slices = _nodes(issues, action.cost_slices, "INVALID_COST_SLICE_CONTAINER", f"{prefix}.cost_slices")
        if not cost_slices:
            _issue(issues, "MISSING_COST_SLICES", f"{prefix}.cost_slices", "must contain at least one executable cost slice")
        valid_quantity_bounds = all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (action.min_quantity_lots, action.max_quantity_lots)
        )
        if valid_quantity_bounds and not 0 < action.min_quantity_lots <= action.max_quantity_lots:
            _issue(issues, "INVALID_QUANTITY_BOUNDS", f"{prefix}.min_quantity_lots", "must satisfy 0 < min_quantity_lots <= max_quantity_lots")
        if (
            valid_quantity_bounds
            and cost_slices
            and isinstance(cost_slices[-1], ExecutableCostSlice)
            and isinstance(cost_slices[-1].last_lot, int)
            and not isinstance(cost_slices[-1].last_lot, bool)
            and action.max_quantity_lots > cost_slices[-1].last_lot
        ):
            _issue(issues, "QUANTITY_BOUNDS_EXCEED_COST_SLICES", f"{prefix}.max_quantity_lots", "must not exceed the final executable cost slice")
        for slice_index, cost_slice in enumerate(cost_slices):
            slice_path = f"{prefix}.cost_slices[{slice_index}]"
            if not isinstance(cost_slice, ExecutableCostSlice):
                _issue(issues, "INVALID_COST_SLICE", slice_path, "must be an ExecutableCostSlice")
                continue
            for name in ("first_lot", "last_lot", "incremental_cost_upper_bound_units"):
                _validate_int(issues, getattr(cost_slice, name), f"{slice_path}.{name}")
            if all(isinstance(value, int) and not isinstance(value, bool) for value in (cost_slice.first_lot, cost_slice.last_lot)) and (cost_slice.first_lot != previous_last + 1 or cost_slice.last_lot < cost_slice.first_lot):
                _issue(issues, "NON_CONTIGUOUS_COST_SLICES", slice_path, "cost slices must start at one and be contiguous")
            if isinstance(cost_slice.last_lot, int) and not isinstance(cost_slice.last_lot, bool):
                previous_last = cost_slice.last_lot
            if isinstance(cost_slice.last_lot, int) and isinstance(cost_slice.incremental_cost_upper_bound_units, int) and not isinstance(cost_slice.last_lot, bool) and not isinstance(cost_slice.incremental_cost_upper_bound_units, bool):
                _validate_product(issues, cost_slice.last_lot, cost_slice.incremental_cost_upper_bound_units, f"{slice_path}.total_cost")
    contract_ids: set[str] = set()
    atom_ids: set[str] = set()
    for state_index, state_set in enumerate(_nodes(issues, problem.terminal_state_sets, "INVALID_TERMINAL_STATE_SET_CONTAINER", "terminal_state_sets")):
        prefix = f"terminal_state_sets[{state_index}]"
        if not isinstance(state_set, TerminalStateSet):
            _issue(issues, "INVALID_TERMINAL_STATE_SET", prefix, "must be a TerminalStateSet")
            continue
        state_contract_id = _identifier(issues, state_set.market_contract_id, f"{prefix}.market_contract_id")
        if state_contract_id is not None and state_contract_id in contract_ids:
            _issue(issues, "DUPLICATE_ID", f"{prefix}.market_contract_id", "market_contract_id must be unique")
        if state_contract_id is not None:
            contract_ids.add(state_contract_id)
        if state_contract_id is not None and state_contract_id not in actions_by_contract:
            _issue(issues, "UNKNOWN_CONTRACT_REFERENCE", f"{prefix}.market_contract_id", "must reference an action contract")
        _validate_observation_key(issues, state_set.settlement_observation_key, f"{prefix}.settlement_observation_key")
        _identifier(issues, state_set.rule_version, f"{prefix}.rule_version")
        atoms = _nodes(issues, state_set.atoms, "INVALID_TERMINAL_ATOM_CONTAINER", f"{prefix}.atoms")
        if not atoms:
            _issue(issues, "MISSING_TERMINAL_ATOMS", f"{prefix}.atoms", "must contain at least one terminal atom")
        state_actions = actions_by_contract.get(state_contract_id, ()) if state_contract_id is not None else ()
        for action in state_actions:
            if action.settlement_observation_key != state_set.settlement_observation_key:
                _issue(issues, "OBSERVATION_KEY_MISMATCH", f"{prefix}.settlement_observation_key", "must match each action for this contract")
        for atom_index, atom in enumerate(atoms):
            atom_path = f"{prefix}.atoms[{atom_index}]"
            if not isinstance(atom, TerminalAtom):
                _issue(issues, "INVALID_TERMINAL_ATOM", atom_path, "must be a TerminalAtom")
                continue
            atom_id = _identifier(issues, atom.atom_id, f"{atom_path}.atom_id")
            if atom_id is not None and atom_id in atom_ids:
                _issue(issues, "DUPLICATE_ID", f"{atom_path}.atom_id", "atom_id must be globally unique")
            if atom_id is not None:
                atom_ids.add(atom_id)
            _validate_enum(issues, atom.kind, TerminalKind, "INVALID_TERMINAL_KIND", f"{atom_path}.kind")
            if atom.rule_version is None or (
                isinstance(atom.rule_version, str) and not atom.rule_version.strip()
            ):
                _issue(issues, "MISSING_TERMINAL_RULE_IDENTITY", f"{atom_path}.rule_version", "must identify the terminal rule")
            else:
                _identifier(issues, atom.rule_version, f"{atom_path}.rule_version")
                if atom.rule_version != state_set.rule_version:
                    _issue(issues, "TERMINAL_RULE_VERSION_MISMATCH", f"{atom_path}.rule_version", "must equal terminal state-set rule_version")
            if atom.capital_release_at is None:
                _issue(issues, "MISSING_CAPITAL_RELEASE_AT", f"{atom_path}.capital_release_at", "must identify capital release time")
                release_at_valid = False
            else:
                release_at_valid = _validate_datetime(issues, atom.capital_release_at, f"{atom_path}.capital_release_at")
            if as_of_valid and release_at_valid and atom.capital_release_at < problem.as_of:
                _issue(issues, "STALE_CAPITAL_RELEASE_AT", f"{atom_path}.capital_release_at", "must be after problem as_of")
            payouts = _nodes(issues, atom.payouts, "INVALID_ACTION_PAYOUT_CONTAINER", f"{atom_path}.payouts")
            valid_payouts = tuple(payout for payout in payouts if isinstance(payout, ActionPayout))
            payout_ids = {
                action_id
                for payout in valid_payouts
                if (action_id := _identifier(issues, payout.action_id, f"{atom_path}.payouts.action_id")) is not None
            }
            required = {action.action_id for action in state_actions if isinstance(action.action_id, str) and action.action_id.strip()}
            if not required.issubset(payout_ids):
                _issue(issues, "MISSING_ACTION_PAYOUT", f"{atom_path}.payouts", "must include every action for this contract")
            if len(payout_ids) != len(valid_payouts):
                _issue(issues, "DUPLICATE_ID", f"{atom_path}.payouts", "action payouts must be unique")
            for payout_index, payout in enumerate(payouts):
                if not isinstance(payout, ActionPayout):
                    _issue(issues, "INVALID_ACTION_PAYOUT", f"{atom_path}.payouts[{payout_index}]", "must be an ActionPayout")
                    continue
                _validate_int(issues, payout.payout_lower_bound_per_lot_units, f"{atom_path}.payouts[{payout_index}].payout_lower_bound_per_lot_units")
                payout_action_id = _identifier(issues, payout.action_id, f"{atom_path}.payouts[{payout_index}].action_id")
                if payout_action_id is None:
                    continue
                if payout_action_id not in action_ids:
                    _issue(issues, "UNKNOWN_ACTION_REFERENCE", f"{atom_path}.payouts[{payout_index}].action_id", "must reference an action")
                elif payout_action_id not in required:
                    _issue(issues, "UNKNOWN_ACTION_REFERENCE", f"{atom_path}.payouts[{payout_index}].action_id", "must reference an action on this contract")
    for contract_id in actions_by_contract:
        if contract_id not in contract_ids:
            _issue(issues, "MISSING_TERMINAL_STATE_SET", "terminal_state_sets", f"missing terminal states for {contract_id}")
    constraint_ids: set[str] = set()
    if not isinstance(problem.constraint_model, ConstraintModel):
        _issue(issues, "INVALID_CONSTRAINT_MODEL", "constraint_model", "must be a ConstraintModel")
        relations: tuple[object, ...] = ()
        forbidden_combinations: tuple[object, ...] = ()
    else:
        relations = _nodes(issues, problem.constraint_model.relations, "INVALID_RELATION_CONTAINER", "constraint_model.relations")
        forbidden_combinations = _nodes(issues, problem.constraint_model.forbidden_atom_combinations, "INVALID_FORBIDDEN_ATOM_COMBINATION_CONTAINER", "constraint_model.forbidden_atom_combinations")
    for relation_index, relation in enumerate(relations):
        path = f"constraint_model.relations[{relation_index}]"
        if not isinstance(relation, RelationConstraint):
            _issue(issues, "INVALID_RELATION", path, "must be a RelationConstraint")
            continue
        constraint_id = _identifier(issues, relation.constraint_id, f"{path}.constraint_id")
        if constraint_id is not None and constraint_id in constraint_ids:
            _issue(issues, "DUPLICATE_ID", f"{path}.constraint_id", "constraint_id must be unique")
        if constraint_id is not None:
            constraint_ids.add(constraint_id)
        _validate_enum(issues, relation.kind, RelationKind, "INVALID_RELATION_KIND", f"{path}.kind")
        _identifier(issues, relation.rule_version, f"{path}.rule_version")
        contract_references = _nodes(issues, relation.contract_ids, "INVALID_CONTRACT_REFERENCE_CONTAINER", f"{path}.contract_ids")
        if relation.kind == RelationKind.IMPLIES and len(contract_references) != 2:
            _issue(issues, "INVALID_RELATION_ARITY", f"{path}.contract_ids", "IMPLIES requires ordered antecedent and consequent")
        if relation.kind != RelationKind.IMPLIES and len(contract_references) < 2:
            _issue(issues, "INVALID_RELATION_ARITY", f"{path}.contract_ids", "relation requires at least two contracts")
        for reference_index, contract_id in enumerate(contract_references):
            contract_id = _identifier(issues, contract_id, f"{path}.contract_ids[{reference_index}]")
            if contract_id is not None and contract_id not in contract_ids:
                _issue(issues, "UNKNOWN_CONTRACT_REFERENCE", f"{path}.contract_ids", "must reference a terminal contract")
    for forbidden_index, forbidden in enumerate(forbidden_combinations):
        path = f"constraint_model.forbidden_atom_combinations[{forbidden_index}]"
        if not isinstance(forbidden, ForbiddenAtomCombination):
            _issue(issues, "INVALID_FORBIDDEN_ATOM_COMBINATION", path, "must be a ForbiddenAtomCombination")
            continue
        constraint_id = _identifier(issues, forbidden.constraint_id, f"{path}.constraint_id")
        if constraint_id is not None and constraint_id in constraint_ids:
            _issue(issues, "DUPLICATE_ID", f"{path}.constraint_id", "constraint_id must be unique")
        if constraint_id is not None:
            constraint_ids.add(constraint_id)
        _identifier(issues, forbidden.rule_version, f"{path}.rule_version")
        for atom_index, atom_id in enumerate(_nodes(issues, forbidden.atom_ids, "INVALID_ATOM_REFERENCE_CONTAINER", f"{path}.atom_ids")):
            atom_id = _identifier(issues, atom_id, f"{path}.atom_ids[{atom_index}]")
            if atom_id is not None and atom_id not in atom_ids:
                _issue(issues, "UNKNOWN_ATOM_REFERENCE", f"{path}.atom_ids", "must reference a terminal atom")
    qualification_ids: set[str] = set()
    for qualification_index, qualification in enumerate(_nodes(issues, problem.qualification_constraints, "INVALID_QUALIFICATION_CONSTRAINT_CONTAINER", "qualification_constraints")):
        path = f"qualification_constraints[{qualification_index}]"
        if not isinstance(qualification, QualificationConstraint):
            _issue(issues, "INVALID_QUALIFICATION_CONSTRAINT", path, "must be a QualificationConstraint")
            continue
        qualification_id = _identifier(issues, qualification.constraint_id, f"{path}.constraint_id")
        if qualification_id is not None and qualification_id in qualification_ids:
            _issue(issues, "DUPLICATE_ID", f"{path}.constraint_id", "qualification constraint_id must be unique")
        if qualification_id is not None:
            qualification_ids.add(qualification_id)
        _identifier(issues, qualification.rule_version, f"{path}.rule_version")
        _validate_enum(issues, qualification.metric, QualificationMetric, "INVALID_QUALIFICATION_METRIC", f"{path}.metric")
        _validate_enum(issues, qualification.comparison, Comparison, "INVALID_COMPARISON", f"{path}.comparison")
        _validate_int(issues, qualification.threshold_numerator, f"{path}.threshold_numerator")
        denominator_valid = _validate_int(issues, qualification.threshold_denominator, f"{path}.threshold_denominator")
        if denominator_valid and qualification.threshold_denominator <= 0:
            _issue(issues, "NON_POSITIVE_DENOMINATOR", f"{path}.threshold_denominator", "must be positive")
    return tuple(issues)


def _object(payload: object, name: str, required: set[str]) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or set(payload) != required or not all(isinstance(key, str) for key in payload):
        raise ModelDecodeError(f"{name} must contain exactly {sorted(required)}")
    return payload


def _array(payload: object, name: str) -> list[object]:
    if not isinstance(payload, list):
        raise ModelDecodeError(f"{name} must be a JSON array")
    return payload


def _string(payload: object, name: str) -> str:
    if not isinstance(payload, str) or not payload.strip():
        raise ModelDecodeError(f"{name} must be a non-empty string")
    return payload


def _text(payload: object, name: str) -> str:
    if not isinstance(payload, str):
        raise ModelDecodeError(f"{name} must be a string")
    return payload


def _integer(payload: object, name: str) -> int:
    if isinstance(payload, bool) or not isinstance(payload, int) or not _INT64_MIN <= payload <= _INT64_MAX:
        raise ModelDecodeError(f"{name} must be a signed 64-bit integer")
    return payload


def _optional_integer(payload: object, name: str) -> int | None:
    return None if payload is None else _integer(payload, name)


def _boolean(payload: object, name: str) -> bool:
    if not isinstance(payload, bool):
        raise ModelDecodeError(f"{name} must be a boolean")
    return payload


def _datetime_from_payload(payload: object, name: str) -> datetime:
    value = _string(payload, name)
    if not value.endswith("Z"):
        raise ModelDecodeError(f"{name} must be an RFC3339 UTC datetime ending in Z")
    try:
        decoded = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise ModelDecodeError(f"{name} must be an RFC3339 UTC datetime ending in Z") from error
    if decoded.tzinfo is None or decoded.utcoffset() != UTC.utcoffset(decoded):
        raise ModelDecodeError(f"{name} must be UTC")
    return decoded.astimezone(UTC)


def _enum(enum_type: type[StrEnum], payload: object, name: str) -> StrEnum:
    try:
        return enum_type(_string(payload, name))
    except ValueError as error:
        raise ModelDecodeError(f"{name} is not a valid {enum_type.__name__}") from error


def _observation_from_payload(payload: object) -> SettlementObservationKey:
    value = _object(payload, "settlement_observation_key", {"schema_version", "oracle_id", "indicator_id", "observation_start", "observation_end", "timezone", "rule_version"})
    schema_version = _string(value["schema_version"], "schema_version")
    if schema_version != OBSERVATION_SCHEMA_V1:
        raise ModelDecodeError(f"schema_version must equal {OBSERVATION_SCHEMA_V1}")
    return SettlementObservationKey(
        schema_version, _string(value["oracle_id"], "oracle_id"),
        _string(value["indicator_id"], "indicator_id"), _datetime_from_payload(value["observation_start"], "observation_start"),
        _datetime_from_payload(value["observation_end"], "observation_end"), _string(value["timezone"], "timezone"),
        _string(value["rule_version"], "rule_version"),
    )


def _cost_slice_from_payload(payload: object) -> ExecutableCostSlice:
    value = _object(payload, "cost_slice", {"first_lot", "last_lot", "incremental_cost_upper_bound_units"})
    return ExecutableCostSlice(_integer(value["first_lot"], "first_lot"), _integer(value["last_lot"], "last_lot"), _integer(value["incremental_cost_upper_bound_units"], "incremental_cost_upper_bound_units"))


def _action_from_payload(payload: object) -> CandidateAction:
    value = _object(payload, "action", {"action_id", "venue_id", "account_id", "chain_id", "market_contract_id", "settlement_observation_key", "side", "lot_step_units", "quantity_scale", "min_quantity_lots", "max_quantity_lots", "settlement_asset_id", "valuation_unit_id", "asset_valuation_rule_id", "cost_slices"})
    return CandidateAction(
        _string(value["action_id"], "action_id"), _string(value["venue_id"], "venue_id"),
        _string(value["account_id"], "account_id"), _string(value["chain_id"], "chain_id"),
        _string(value["market_contract_id"], "market_contract_id"),
        _observation_from_payload(value["settlement_observation_key"]), _enum(ActionSide, value["side"], "side"),
        _integer(value["lot_step_units"], "lot_step_units"), _integer(value["quantity_scale"], "quantity_scale"),
        _integer(value["min_quantity_lots"], "min_quantity_lots"), _integer(value["max_quantity_lots"], "max_quantity_lots"),
        _string(value["settlement_asset_id"], "settlement_asset_id"), _string(value["valuation_unit_id"], "valuation_unit_id"),
        _text(value["asset_valuation_rule_id"], "asset_valuation_rule_id"), tuple(_cost_slice_from_payload(item) for item in _array(value["cost_slices"], "cost_slices")),
    )


def _payout_from_payload(payload: object) -> ActionPayout:
    value = _object(payload, "action_payout", {"action_id", "payout_lower_bound_per_lot_units"})
    return ActionPayout(_string(value["action_id"], "action_id"), _integer(value["payout_lower_bound_per_lot_units"], "payout_lower_bound_per_lot_units"))


def _atom_from_payload(payload: object) -> TerminalAtom:
    required = {"atom_id", "kind", "payouts"}
    allowed = required | {"rule_version", "capital_release_at"}
    if not isinstance(payload, Mapping) or not required <= set(payload) <= allowed or not all(isinstance(key, str) for key in payload):
        raise ModelDecodeError(f"terminal_atom must contain {sorted(required)} and only optional rule_version/capital_release_at")
    rule_version = payload.get("rule_version")
    capital_release_at = payload.get("capital_release_at")
    return TerminalAtom(
        _string(payload["atom_id"], "atom_id"),
        _enum(TerminalKind, payload["kind"], "kind"),
        None if rule_version is None else _text(rule_version, "rule_version"),
        tuple(_payout_from_payload(item) for item in _array(payload["payouts"], "payouts")),
        None if capital_release_at is None else _datetime_from_payload(capital_release_at, "capital_release_at"),
    )


def _state_set_from_payload(payload: object) -> TerminalStateSet:
    value = _object(payload, "terminal_state_set", {"market_contract_id", "settlement_observation_key", "rule_version", "atoms"})
    return TerminalStateSet(_string(value["market_contract_id"], "market_contract_id"), _observation_from_payload(value["settlement_observation_key"]), _string(value["rule_version"], "rule_version"), tuple(_atom_from_payload(item) for item in _array(value["atoms"], "atoms")))


def _relation_from_payload(payload: object) -> RelationConstraint:
    value = _object(payload, "relation", {"constraint_id", "kind", "contract_ids", "rule_version"})
    return RelationConstraint(_string(value["constraint_id"], "constraint_id"), _enum(RelationKind, value["kind"], "kind"), tuple(_string(item, "contract_ids[]") for item in _array(value["contract_ids"], "contract_ids")), _string(value["rule_version"], "rule_version"))


def _forbidden_from_payload(payload: object) -> ForbiddenAtomCombination:
    value = _object(payload, "forbidden_atom_combination", {"constraint_id", "atom_ids", "rule_version"})
    return ForbiddenAtomCombination(_string(value["constraint_id"], "constraint_id"), tuple(_string(item, "atom_ids[]") for item in _array(value["atom_ids"], "atom_ids")), _string(value["rule_version"], "rule_version"))


def _qualification_from_payload(payload: object) -> QualificationConstraint:
    value = _object(payload, "qualification_constraint", {"constraint_id", "rule_version", "metric", "comparison", "threshold_numerator", "threshold_denominator"})
    return QualificationConstraint(_string(value["constraint_id"], "constraint_id"), _string(value["rule_version"], "rule_version"), _enum(QualificationMetric, value["metric"], "metric"), _enum(Comparison, value["comparison"], "comparison"), _integer(value["threshold_numerator"], "threshold_numerator"), _integer(value["threshold_denominator"], "threshold_denominator"))


def _sorted_problem(problem: ArbitrageProblem) -> ArbitrageProblem:
    actions = tuple(sorted(problem.actions, key=lambda action: action.action_id))
    state_sets = tuple(
        sorted(
            (
                replace(
                    state_set,
                    atoms=tuple(
                        sorted(
                            (
                                replace(atom, payouts=tuple(sorted(atom.payouts, key=lambda payout: payout.action_id)))
                                for atom in state_set.atoms
                            ),
                            key=lambda atom: atom.atom_id,
                        )
                    ),
                )
                for state_set in problem.terminal_state_sets
            ),
            key=lambda state_set: state_set.market_contract_id,
        )
    )
    relations = tuple(sorted((replace(relation, contract_ids=relation.contract_ids if relation.kind == RelationKind.IMPLIES else tuple(sorted(relation.contract_ids))) for relation in problem.constraint_model.relations), key=lambda relation: relation.constraint_id))
    forbidden = tuple(sorted((replace(item, atom_ids=tuple(sorted(item.atom_ids))) for item in problem.constraint_model.forbidden_atom_combinations), key=lambda item: item.constraint_id))
    return replace(problem, actions=actions, terminal_state_sets=state_sets, constraint_model=ConstraintModel(relations, forbidden), qualification_constraints=tuple(sorted(problem.qualification_constraints, key=lambda item: item.constraint_id)))


def problem_from_payload(payload: Mapping[str, object], *, allow_unknown_data: bool = False) -> ArbitrageProblem:
    value = _object(payload, "problem", {"schema_version", "problem_id", "as_of", "valuation_unit_id", "actions", "terminal_state_sets", "constraint_model", "qualification_constraints"})
    schema_version = _string(value["schema_version"], "schema_version")
    if schema_version != PROBLEM_SCHEMA_V1:
        raise ModelDecodeError(f"schema_version must equal {PROBLEM_SCHEMA_V1}")
    constraint_model = _object(value["constraint_model"], "constraint_model", {"relations", "forbidden_atom_combinations"})
    problem = ArbitrageProblem(
        schema_version, _string(value["problem_id"], "problem_id"), _datetime_from_payload(value["as_of"], "as_of"), _string(value["valuation_unit_id"], "valuation_unit_id"),
        tuple(_action_from_payload(item) for item in _array(value["actions"], "actions")),
        tuple(_state_set_from_payload(item) for item in _array(value["terminal_state_sets"], "terminal_state_sets")),
        ConstraintModel(tuple(_relation_from_payload(item) for item in _array(constraint_model["relations"], "relations")), tuple(_forbidden_from_payload(item) for item in _array(constraint_model["forbidden_atom_combinations"], "forbidden_atom_combinations"))),
        tuple(_qualification_from_payload(item) for item in _array(value["qualification_constraints"], "qualification_constraints")),
    )
    issues = validate_problem(problem)
    if issues and not (
        allow_unknown_data
        and all(issue.code in {"MISSING_ACTION_PAYOUT", "MISSING_TERMINAL_RULE_IDENTITY", "MISSING_CAPITAL_RELEASE_AT", "MISSING_ASSET_VALUATION_RULE"} for issue in issues)
    ):
        raise ModelDecodeError("invalid problem: " + "; ".join(f"{issue.path}: {issue.code}" for issue in issues))
    return _sorted_problem(problem)


def _budget_from_payload(payload: object) -> OracleBudget:
    value = _object(payload, "budget", {"max_quantity_vectors", "max_joint_states", "max_support_rechecks"})
    budget = OracleBudget(_integer(value["max_quantity_vectors"], "max_quantity_vectors"), _integer(value["max_joint_states"], "max_joint_states"), _integer(value["max_support_rechecks"], "max_support_rechecks"))
    if any(item <= 0 for item in (budget.max_quantity_vectors, budget.max_joint_states, budget.max_support_rechecks)):
        raise ModelDecodeError("budget limits must be positive")
    return budget


def request_from_payload(payload: Mapping[str, object]) -> OracleRequest:
    value = _object(payload, "request", {"schema_version", "mode", "problem", "budget"})
    schema_version = _string(value["schema_version"], "schema_version")
    if schema_version != REQUEST_SCHEMA_V1:
        raise ModelDecodeError(f"schema_version must equal {REQUEST_SCHEMA_V1}")
    return OracleRequest(schema_version, _enum(SearchMode, value["mode"], "mode"), problem_from_payload(_object(value["problem"], "problem", {"schema_version", "problem_id", "as_of", "valuation_unit_id", "actions", "terminal_state_sets", "constraint_model", "qualification_constraints"}), allow_unknown_data=True), _budget_from_payload(value["budget"]))


def _quantity_from_payload(payload: object) -> ActionQuantity:
    value = _object(payload, "action_quantity", {"action_id", "quantity_lots"})
    quantity_lots = _integer(value["quantity_lots"], "quantity_lots")
    if quantity_lots < 0:
        raise ModelDecodeError("quantity_lots must be non-negative")
    return ActionQuantity(_string(value["action_id"], "action_id"), quantity_lots)


def _selected_atom_from_payload(payload: object) -> SelectedAtom:
    value = _object(payload, "selected_atom", {"market_contract_id", "atom_id"})
    return SelectedAtom(_string(value["market_contract_id"], "market_contract_id"), _string(value["atom_id"], "atom_id"))


def _scenario_from_payload(payload: object) -> SettlementScenario:
    value = _object(payload, "scenario", {"atoms"})
    return SettlementScenario(tuple(_selected_atom_from_payload(item) for item in _array(value["atoms"], "atoms")))


def _cut_from_payload(payload: object) -> WorstStateCut:
    value = _object(payload, "worst_state_cut", {"cut_id", "scenario", "payout_per_lot"})
    return WorstStateCut(_string(value["cut_id"], "cut_id"), _scenario_from_payload(value["scenario"]), tuple(_payout_from_payload(item) for item in _array(value["payout_per_lot"], "payout_per_lot")))


def _support_graph_from_payload(payload: object) -> SelectedSupportGraph:
    value = _object(payload, "selected_support_graph", {"action_ids", "contract_ids", "constraint_ids", "hyperedges"})
    hyperedges = []
    for item in _array(value["hyperedges"], "hyperedges"):
        edge = _array(item, "hyperedge")
        if len(edge) != 2:
            raise ModelDecodeError("hyperedge must contain a constraint ID and contract IDs")
        hyperedges.append((_string(edge[0], "hyperedge.constraint_id"), tuple(_string(contract, "hyperedge.contract_ids[]") for contract in _array(edge[1], "hyperedge.contract_ids"))))
    return SelectedSupportGraph(tuple(_string(item, "action_ids[]") for item in _array(value["action_ids"], "action_ids")), tuple(_string(item, "contract_ids[]") for item in _array(value["contract_ids"], "contract_ids")), tuple(_string(item, "constraint_ids[]") for item in _array(value["constraint_ids"], "constraint_ids")), tuple(hyperedges))


def _payout_proof_from_payload(payload: object) -> PayoutProof:
    value = _object(payload, "payout_proof", {"schema_version", "result_kind", "problem_fingerprint", "portfolio_fingerprint", "worst_scenario", "worst_state_cut", "payout_lower_bound_units", "cost_upper_bound_units", "guaranteed_profit_units", "conservative_capital_release_at", "selected_support_graph", "proof_method", "request_fingerprint", "source_problem_fingerprint", "qualification_fingerprint", "quantity_vectors_total", "quantity_vectors_examined", "joint_states_per_vector", "rejection_counts"})
    proof = PayoutProof(
        schema_version=_string(value["schema_version"], "schema_version"),
        result_kind=_enum(ProofResultKind, value["result_kind"], "result_kind"),
        problem_fingerprint=_string(value["problem_fingerprint"], "problem_fingerprint"),
        portfolio_fingerprint=None if value["portfolio_fingerprint"] is None else _string(value["portfolio_fingerprint"], "portfolio_fingerprint"),
        worst_scenario=None if value["worst_scenario"] is None else _scenario_from_payload(value["worst_scenario"]),
        worst_state_cut=None if value["worst_state_cut"] is None else _cut_from_payload(value["worst_state_cut"]),
        payout_lower_bound_units=_optional_integer(value["payout_lower_bound_units"], "payout_lower_bound_units"),
        cost_upper_bound_units=_optional_integer(value["cost_upper_bound_units"], "cost_upper_bound_units"),
        guaranteed_profit_units=_optional_integer(value["guaranteed_profit_units"], "guaranteed_profit_units"),
        conservative_capital_release_at=None if value["conservative_capital_release_at"] is None else _datetime_from_payload(value["conservative_capital_release_at"], "conservative_capital_release_at"),
        selected_support_graph=None if value["selected_support_graph"] is None else _support_graph_from_payload(value["selected_support_graph"]),
        proof_method=_string(value["proof_method"], "proof_method"),
        request_fingerprint=None if value["request_fingerprint"] is None else _string(value["request_fingerprint"], "request_fingerprint"),
        source_problem_fingerprint=None if value["source_problem_fingerprint"] is None else _string(value["source_problem_fingerprint"], "source_problem_fingerprint"),
        qualification_fingerprint=None if value["qualification_fingerprint"] is None else _string(value["qualification_fingerprint"], "qualification_fingerprint"),
        quantity_vectors_total=_optional_integer(value["quantity_vectors_total"], "quantity_vectors_total"),
        quantity_vectors_examined=_optional_integer(value["quantity_vectors_examined"], "quantity_vectors_examined"),
        joint_states_per_vector=_optional_integer(value["joint_states_per_vector"], "joint_states_per_vector"),
        rejection_counts=_rejection_counts_from_payload(value["rejection_counts"]),
    )
    if proof.schema_version != PAYOUT_PROOF_SCHEMA_V1:
        raise ModelDecodeError(f"schema_version must equal {PAYOUT_PROOF_SCHEMA_V1}")
    if proof.result_kind == ProofResultKind.PORTFOLIO and (
        proof.proof_method != "BOUNDED_EXACT_ORACLE_V1"
        or any(item is None for item in (proof.portfolio_fingerprint, proof.worst_scenario, proof.worst_state_cut, proof.payout_lower_bound_units, proof.cost_upper_bound_units, proof.guaranteed_profit_units, proof.conservative_capital_release_at, proof.selected_support_graph, proof.qualification_fingerprint))
        or any(item is not None for item in (proof.request_fingerprint, proof.source_problem_fingerprint, proof.quantity_vectors_total, proof.quantity_vectors_examined, proof.joint_states_per_vector))
        or proof.rejection_counts
    ):
        raise ModelDecodeError("PORTFOLIO payout proof contains invalid branch fields")
    if proof.result_kind != ProofResultKind.PORTFOLIO and (
        proof.proof_method != "EXHAUSTIVE_ORACLE_V1"
        or any(item is not None for item in (proof.portfolio_fingerprint, proof.worst_scenario, proof.worst_state_cut, proof.payout_lower_bound_units, proof.cost_upper_bound_units, proof.guaranteed_profit_units, proof.conservative_capital_release_at, proof.selected_support_graph))
        or any(item is None for item in (proof.request_fingerprint, proof.qualification_fingerprint, proof.quantity_vectors_total, proof.quantity_vectors_examined, proof.joint_states_per_vector))
        or proof.quantity_vectors_total != proof.quantity_vectors_examined
        or proof.quantity_vectors_total <= 0
        or proof.joint_states_per_vector <= 0
        or any(count > proof.quantity_vectors_examined for _, count in proof.rejection_counts)
        or (proof.result_kind == ProofResultKind.NO_QUALIFIED_OPPORTUNITY) != (proof.source_problem_fingerprint is None)
    ):
        raise ModelDecodeError("negative payout proof contains invalid branch fields")
    return proof


def _rejection_counts_from_payload(payload: object) -> tuple[tuple[str, int], ...]:
    rejection_counts = []
    rejection_ids: set[str] = set()
    for item in _array(payload, "rejection_counts"):
        pair = _array(item, "rejection_count")
        if len(pair) != 2:
            raise ModelDecodeError("rejection_count must contain an ID and count")
        rejection_id = _string(pair[0], "rejection_count.id")
        if rejection_id in rejection_ids:
            raise ModelDecodeError("rejection_count IDs must be unique")
        rejection_ids.add(rejection_id)
        count = _integer(pair[1], "rejection_count.count")
        if count < 0:
            raise ModelDecodeError("rejection_count.count must be non-negative")
        rejection_counts.append((rejection_id, count))
    return tuple(rejection_counts)


def _solution_from_payload(payload: object) -> PortfolioSolution:
    value = _object(payload, "solution", {"quantities", "payout_proof"})
    proof = _payout_proof_from_payload(value["payout_proof"])
    if proof.result_kind != ProofResultKind.PORTFOLIO:
        raise ModelDecodeError("solution requires a PORTFOLIO payout proof")
    return PortfolioSolution(tuple(_quantity_from_payload(item) for item in _array(value["quantities"], "quantities")), proof)


def portfolio_solution_from_payload(payload: object) -> PortfolioSolution:
    """Decode one canonical portfolio proof without wrapping it in an Oracle result."""
    return _solution_from_payload(payload)


def payout_proof_from_payload(payload: object) -> PayoutProof:
    """Decode one canonical payout proof without wrapping it in an Oracle result."""
    return _payout_proof_from_payload(payload)


def _bounds_from_payload(payload: object) -> ObjectiveBounds:
    value = _object(payload, "objective_bounds", {"lower_bound_units", "upper_bound_units", "gap_units", "closed"})
    return ObjectiveBounds(_optional_integer(value["lower_bound_units"], "lower_bound_units"), _optional_integer(value["upper_bound_units"], "upper_bound_units"), _optional_integer(value["gap_units"], "gap_units"), _boolean(value["closed"], "closed"))


def _validate_result(result: OracleResult) -> None:
    feasible = result.solve_status == SolveStatus.FEASIBLE
    negative = result.business_status in {BusinessStatus.NO_QUALIFIED_OPPORTUNITY, BusinessStatus.NO_ARBITRAGE}
    if feasible:
        if result.solution is None or result.proof_status != ProofStatus.PROVEN or result.business_status != BusinessStatus.QUALIFIED_FEASIBLE or result.negative_proof is not None or result.unknown_reason is not None:
            raise ModelDecodeError("FEASIBLE requires a proved qualified solution and no negative or unknown state")
        if result.optimality_status == OptimalityStatus.NOT_PROVEN and (result.objective_bounds.closed or result.objective_bounds.lower_bound_units is None or result.objective_bounds.upper_bound_units is not None or result.objective_bounds.gap_units is not None):
            raise ModelDecodeError("non-optimal feasible results require only an open lower bound")
        if result.optimality_status not in {OptimalityStatus.NOT_PROVEN, OptimalityStatus.OPTIMAL}:
            raise ModelDecodeError("FEASIBLE requires an optimal or not-proven optimality status")
    if result.optimality_status == OptimalityStatus.OPTIMAL:
        bounds = result.objective_bounds
        if not feasible or result.solution is None or result.proof_status != ProofStatus.PROVEN or not bounds.closed or bounds.lower_bound_units is None or bounds.lower_bound_units != bounds.upper_bound_units or bounds.gap_units != 0:
            raise ModelDecodeError("OPTIMAL requires a proved solution and equal closed objective bounds")
    if negative:
        if result.solve_status != SolveStatus.INFEASIBLE or result.proof_status != ProofStatus.PROVEN or result.optimality_status != OptimalityStatus.NOT_APPLICABLE or result.solution is not None or result.negative_proof is None or result.negative_proof.result_kind.value != result.business_status.value or result.negative_proof.quantity_vectors_total <= 0 or result.negative_proof.quantity_vectors_examined != result.negative_proof.quantity_vectors_total or result.negative_proof.joint_states_per_vector <= 0 or result.unknown_reason is not None:
            raise ModelDecodeError("negative conclusions require a matching exhaustive proof")
    if result.business_status == BusinessStatus.NO_ARBITRAGE:
        bounds = result.objective_bounds
        proof = result.negative_proof
        if not bounds.closed or bounds.lower_bound_units is None or bounds.upper_bound_units is None or bounds.lower_bound_units != bounds.upper_bound_units or bounds.lower_bound_units > 0 or bounds.gap_units != 0 or proof is None or proof.source_problem_fingerprint is None:
            raise ModelDecodeError("NO_ARBITRAGE requires closed diagnostic bounds and a source proof")
    if result.solve_status == SolveStatus.UNKNOWN:
        if result.business_status != BusinessStatus.UNKNOWN or result.proof_status != ProofStatus.UNKNOWN or result.optimality_status != OptimalityStatus.NOT_APPLICABLE or result.solution is not None or result.negative_proof is not None or result.unknown_reason is None:
            raise ModelDecodeError("UNKNOWN requires only a matching unknown reason")
    if result.solve_status == SolveStatus.INFEASIBLE and not negative:
        raise ModelDecodeError("INFEASIBLE requires an exhaustive negative conclusion")


def result_from_payload(payload: Mapping[str, object]) -> OracleResult:
    value = _object(payload, "result", {"solve_status", "proof_status", "business_status", "optimality_status", "objective_bounds", "solution", "negative_proof", "unknown_reason"})
    solution = None if value["solution"] is None else _solution_from_payload(value["solution"])
    negative_proof = None if value["negative_proof"] is None else _payout_proof_from_payload(value["negative_proof"])
    unknown_reason = None if value["unknown_reason"] is None else _enum(UnknownReason, value["unknown_reason"], "unknown_reason")
    result = OracleResult(_enum(SolveStatus, value["solve_status"], "solve_status"), _enum(ProofStatus, value["proof_status"], "proof_status"), _enum(BusinessStatus, value["business_status"], "business_status"), _enum(OptimalityStatus, value["optimality_status"], "optimality_status"), _bounds_from_payload(value["objective_bounds"]), solution, negative_proof, unknown_reason)
    _validate_result(result)
    return result
