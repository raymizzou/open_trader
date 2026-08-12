from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from open_trader.prediction_n_leg import (
    ActionPayout,
    ArbitrageProblem,
    BusinessStatus,
    CandidateAction,
    Comparison,
    ConstraintModel,
    ExecutableCostSlice,
    ForbiddenAtomCombination,
    OracleBudget,
    OracleRequest,
    OracleResult,
    QualificationConstraint,
    QualificationMetric,
    REQUEST_SCHEMA_V1,
    RelationConstraint,
    RelationKind,
    SearchMode,
    SelectedSupportGraph,
    SettlementObservationKey,
    TerminalAtom,
    TerminalKind,
    TerminalStateSet,
    UnknownReason,
    canonical_payload,
    fingerprint,
    request_from_payload,
    result_from_payload,
    problem_from_payload,
    validate_problem,
)
from open_trader.prediction_n_leg_oracle import (
    PortfolioEvaluation,
    cut_from_scenario,
    derive_selected_support_graph,
    diagnose_raw_arbitrage,
    evaluate_fixed_portfolio,
    find_qualified,
    solve_optimal,
    split_disconnected_solution,
)
from open_trader.prediction_solver import BenchmarkClassification, SolverEvidence


_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_FIXTURE = _ROOT / "tests" / "fixtures" / "prediction_n_leg_v1.json"
_CANONICAL_FIXTURE_SHA256 = "a4680fb2c66dedac9e85db9cd06d0872882ca69b09fba6d9f338d0b97243ecc7"
_SYNTHETIC_SCHEMA_V1 = "open_trader.prediction_solver.synthetic_corpus.v1"
_APPROVED_ENVELOPE_V1 = "open_trader.prediction_solver.approved_envelope.v1"
_APPROVED_CORPUS_V1 = "open_trader.prediction_solver.approved_corpus.v1"
_APPROVED_ENVELOPE_KEYS = {
    "schema_version",
    "source_alias",
    "approval_id",
    "generation_id",
    "approver",
    "approved_at",
    "captured_at",
    "problem",
}
_APPROVAL_PROVENANCE_KEYS = {"approval_id", "generation_id", "approver", "approved_at", "captured_at"}
_INPUT_GAP_CODES = {
    "MISSING_ACTION_PAYOUT": "terminal_payout",
    "MISSING_TERMINAL_RULE_IDENTITY": "terminal_rule",
    "MISSING_CAPITAL_RELEASE_AT": "terminal_release",
    "MISSING_ASSET_VALUATION_RULE": "valuation_identity",
}


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    request: OracleRequest
    expected_result: OracleResult | None
    request_fingerprint: str
    result_fingerprint: str | None
    budget: OracleBudget
    truth_method: str


class CheckFailureReason(StrEnum):
    FALSE_SAFE = "FALSE_SAFE"
    FALSE_NEGATIVE = "FALSE_NEGATIVE"
    FALSE_OPTIMAL = "FALSE_OPTIMAL"
    WRONG_TIE_BREAK = "WRONG_TIE_BREAK"
    WRONG_RELEASE = "WRONG_RELEASE"
    DISCONNECTED_SUPPORT = "DISCONNECTED_SUPPORT"
    CHANGED_PROBLEM_FINGERPRINT = "CHANGED_PROBLEM_FINGERPRINT"
    UNVERIFIED_NEGATIVE = "UNVERIFIED_NEGATIVE"
    CLAIM_MISMATCH = "CLAIM_MISMATCH"


@dataclass(frozen=True, slots=True)
class DifferentialCheck:
    classification: BenchmarkClassification
    canonical_result: OracleResult | None
    hard_failure: bool
    failure_reason: CheckFailureReason | None


def load_canonical_cases() -> tuple[BenchmarkCase, ...]:
    fixture_bytes = _CANONICAL_FIXTURE.read_bytes()
    if hashlib.sha256(fixture_bytes).hexdigest() != _CANONICAL_FIXTURE_SHA256:
        raise ValueError("canonical #48 fixture byte SHA-256 changed")
    payload = json.loads(fixture_bytes)
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or len(cases) != 16:
        raise ValueError("canonical #48 fixture must contain exactly 16 cases")

    decoded = []
    for item in cases:
        if not isinstance(item, dict):
            raise ValueError("canonical #48 case must be an object")
        request = request_from_payload(item["request"])
        result = result_from_payload(item["expected_result"])
        request_fingerprint = item["expected_request_fingerprint"]
        result_fingerprint = item["expected_result_fingerprint"]
        if fingerprint(request) != request_fingerprint or fingerprint(result) != result_fingerprint:
            raise ValueError(f"canonical #48 fingerprints changed for {item.get('case_id', '<unknown>')}")
        decoded.append(
            BenchmarkCase(
                case_id=item["case_id"],
                request=request,
                expected_result=result,
                request_fingerprint=request_fingerprint,
                result_fingerprint=result_fingerprint,
                budget=request.budget,
                truth_method="exact_oracle_v1",
            )
        )
    return tuple(decoded)


def generate_synthetic_corpus(seed: int = 4901) -> bytes:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    rng = random.Random(seed)
    token = f"{rng.getrandbits(48):012x}"
    canonical = {case.case_id: case for case in load_canonical_cases()}
    semantic_sources = (
        ("single_contract_complement", canonical["native-complement-n2"]),
        ("three_leg_exactly_one", canonical["exactly-one-n3"]),
        ("implies_exceptional", _implies_exceptional(canonical["implication-two-contract"], token)),
        ("mutual_exclusion_exceptional", _mutual_exclusion_exceptional(canonical["exactly-one-n3"], token)),
        ("forbidden_atoms", canonical["explicit-exception-link"]),
        ("piecewise_cost", _piecewise_cost(canonical["native-complement-n2"], token)),
        ("quantity_bounds", canonical["quantity-selection-n4"]),
        ("profit_boundary", canonical["rounded-false-edge"]),
        ("margin_boundary", _margin_boundary(canonical["native-complement-n2"], token)),
        ("annualized_round_up", _annualized_round_up(canonical["native-complement-n2"], token, rng.randint(1, 86_399))),
        ("release_delay_boundary", _release_delay_boundary(canonical["native-complement-n2"], token, rng.randint(1, 3_600))),
        ("unreachable_late_release", _unreachable_late_release(canonical["native-complement-n2"], token)),
        ("disconnected_support", canonical["disconnected-double-arbitrage"]),
        ("same_observation_identity", _same_observation_identity(canonical["disconnected-double-arbitrage"], token)),
        ("contradictory_terminal", canonical["unknown-contradictory-model"]),
        ("missing_terminal_data", canonical["unknown-missing-atom-data"]),
        ("missing_valuation", canonical["unknown-cross-asset"]),
        ("raw_no_arbitrage", canonical["no-arbitrage"]),
    )
    cases = [_exact_case(case_id, source) for case_id, source in semantic_sources]
    for size in (8, 16, 32):
        cases.append(_scale_case(size, dense=False, token=token))
        cases.append(_scale_case(size, dense=True, token=token))
    manifest: dict[str, object] = {
        "case_count": len(cases),
        "cases": cases,
        "schema_version": _SYNTHETIC_SCHEMA_V1,
        "seed": seed,
    }
    manifest_hash = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {**manifest, "manifest_sha256": f"sha256:{manifest_hash}"}
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def import_approved_snapshot(inbox_path: str | os.PathLike[str], corpus_path: str | os.PathLike[str]) -> None:
    inbox = _approved_inbox_path(inbox_path)
    corpus = Path(corpus_path)
    corpus_payload = _load_approved_corpus(corpus)
    try:
        envelope = json.loads(inbox.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("approved snapshot must be readable local JSON") from error
    if not isinstance(envelope, dict) or not all(isinstance(key, str) for key in envelope):
        raise ValueError("approved snapshot envelope must be an object")
    unknown = set(envelope) - _APPROVED_ENVELOPE_KEYS
    if unknown:
        raise ValueError(f"approved snapshot contains unknown keys: {sorted(unknown)}")
    missing = _APPROVED_ENVELOPE_KEYS - set(envelope)
    if missing - _APPROVAL_PROVENANCE_KEYS:
        raise ValueError(f"approved snapshot is missing keys: {sorted(missing)}")
    if envelope["schema_version"] != _APPROVED_ENVELOPE_V1:
        raise ValueError("unsupported approved snapshot schema")
    source_alias = envelope["source_alias"]
    if not _nonempty_text(source_alias):
        raise ValueError("source_alias must be a non-empty string")
    if not isinstance(envelope["problem"], Mapping):
        raise ValueError("problem must be an object")
    problem_payload, missing_valuation_identity = _problem_payload_for_gap_validation(envelope["problem"])
    problem = problem_from_payload(problem_payload, allow_unknown_data=True)
    gap_kinds = {"valuation_identity"} if missing_valuation_identity else set()
    issues = validate_problem(problem)
    if issues:
        gap_codes = {_INPUT_GAP_CODES.get(issue.code) for issue in issues}
        if None in gap_codes:
            raise ValueError("approved problem is malformed")
        gap_kinds.update(gap_codes)

    if missing:
        _append_input_gap(corpus, corpus_payload, "approval_provenance", source_alias, envelope["problem"])
        return
    if any(not _nonempty_text(envelope[name]) for name in ("approval_id", "generation_id", "approver")):
        _append_input_gap(corpus, corpus_payload, "approval_provenance", source_alias, envelope["problem"])
        return
    for name in ("approved_at", "captured_at"):
        if not _nonempty_text(envelope[name]):
            _append_input_gap(corpus, corpus_payload, "approval_provenance", source_alias, envelope["problem"])
            return
        _utc_z(envelope[name], name)
    if gap_kinds:
        _append_input_gap(
            corpus,
            corpus_payload,
            sorted(gap_kinds)[0],
            source_alias,
            envelope["problem"],
        )
        return

    source_fingerprint = fingerprint(problem)
    salt = corpus_payload["anonymization_salt"]
    anonymized = _anonymize_problem(problem, salt)
    anonymized_fingerprint = fingerprint(anonymized)
    if any(case.get("anonymized_problem_fingerprint") == anonymized_fingerprint for case in corpus_payload["cases"]):
        return
    token = lambda namespace, value: _anonymous_token(salt, namespace, value)
    corpus_payload["cases"].append(
        {
            "anonymized_problem_fingerprint": anonymized_fingerprint,
            "approval_id": token("approval", envelope["approval_id"]),
            "approved_at": envelope["approved_at"],
            "approver": token("approver", envelope["approver"]),
            "captured_at": envelope["captured_at"],
            "case_id": f"approved:{anonymized_fingerprint.removeprefix('sha256:')[:24]}",
            "generation_id": token("generation", envelope["generation_id"]),
            "problem": canonical_payload(anonymized),
            "source_alias": source_alias,
            "source_problem_fingerprint": source_fingerprint,
        }
    )
    corpus_payload["cases"].sort(key=lambda case: case["case_id"])
    _atomic_write_json(corpus, corpus_payload)


def check_solver_claim(
    request: OracleRequest,
    evidence: SolverEvidence,
    *,
    claimed_problem_fingerprint: str,
    truth_method: str,
) -> DifferentialCheck:
    if claimed_problem_fingerprint != fingerprint(request.problem):
        return _check_failure(CheckFailureReason.CHANGED_PROBLEM_FINGERPRINT)
    if truth_method == "measurement_only":
        if _is_negative_claim(evidence):
            return _check_failure(CheckFailureReason.UNVERIFIED_NEGATIVE)
        return DifferentialCheck(BenchmarkClassification.MEASUREMENT_ONLY, None, False, None)
    if truth_method != "exact_oracle_v1":
        return _check_failure(CheckFailureReason.CLAIM_MISMATCH)

    exact = _oracle_result(request)
    if evidence.candidate is None:
        return _check_nonpositive_claim(evidence, exact)
    return _check_positive_claim(request, evidence, exact)


def _check_positive_claim(
    request: OracleRequest,
    evidence: SolverEvidence,
    exact: OracleResult,
) -> DifferentialCheck:
    assert evidence.candidate is not None
    problem = (
        replace(
            request.problem,
            problem_id=f"{request.problem.problem_id}:raw-arbitrage-diagnostic",
            qualification_constraints=(),
        )
        if request.mode == SearchMode.RAW_ARBITRAGE_DIAGNOSTIC
        else request.problem
    )
    search_budget_exhausted = exact.unknown_reason in {
        UnknownReason.ORACLE_DECISION_LIMIT_EXCEEDED,
        UnknownReason.ORACLE_STATE_LIMIT_EXCEEDED,
    }
    try:
        evaluation = evaluate_fixed_portfolio(problem, evidence.candidate.quantities, request.budget)
    except ValueError as error:
        if search_budget_exhausted and str(error) == UnknownReason.ORACLE_STATE_LIMIT_EXCEEDED.value:
            return DifferentialCheck(BenchmarkClassification.MEASUREMENT_ONLY, None, False, None)
        return _check_failure(CheckFailureReason.FALSE_SAFE)
    except OverflowError:
        return _check_failure(CheckFailureReason.FALSE_SAFE)
    if evidence.candidate.claimed_guaranteed_profit_units > evaluation.guaranteed_profit_units and evaluation.guaranteed_profit_units < 0:
        return _check_failure(CheckFailureReason.FALSE_SAFE)
    if evaluation.failed_qualification_ids:
        return _check_failure(CheckFailureReason.FALSE_SAFE)
    try:
        support = derive_selected_support_graph(problem, evaluation, request.budget)
    except ValueError as error:
        if search_budget_exhausted and str(error) == UnknownReason.ORACLE_STATE_LIMIT_EXCEEDED.value:
            return DifferentialCheck(BenchmarkClassification.MEASUREMENT_ONLY, None, False, None)
        return _check_failure(CheckFailureReason.CLAIM_MISMATCH)
    except OverflowError:
        return _check_failure(CheckFailureReason.CLAIM_MISMATCH)
    if search_budget_exhausted and support == UnknownReason.ORACLE_SUPPORT_LIMIT_EXCEEDED:
        return DifferentialCheck(BenchmarkClassification.MEASUREMENT_ONLY, None, False, None)
    if not isinstance(support, SelectedSupportGraph):
        return _check_failure(CheckFailureReason.CLAIM_MISMATCH)
    if len(split_disconnected_solution(problem, evaluation, support)) != 1:
        return _check_failure(CheckFailureReason.DISCONNECTED_SUPPORT)
    if evidence.conservative_capital_release_at != evaluation.conservative_capital_release_at:
        return _check_failure(CheckFailureReason.WRONG_RELEASE)
    if not _fixed_claim_matches(problem, evidence, evaluation):
        return _check_failure(CheckFailureReason.CLAIM_MISMATCH)
    if search_budget_exhausted:
        return DifferentialCheck(BenchmarkClassification.MEASUREMENT_ONLY, None, False, None)
    if exact.solution is None:
        return _check_failure(CheckFailureReason.FALSE_SAFE)

    exact_solution = exact.solution
    exact_proof = exact_solution.payout_proof
    if request.mode != SearchMode.ADMISSION and evaluation.guaranteed_profit_units != exact_proof.guaranteed_profit_units:
        return _check_failure(CheckFailureReason.FALSE_OPTIMAL)
    if evaluation.quantities != exact_solution.quantities:
        same_tie_dimensions = (
            evaluation.guaranteed_profit_units == exact_proof.guaranteed_profit_units
            and evaluation.cost_upper_bound_units == exact_proof.cost_upper_bound_units
            and len(evaluation.quantities) == len(exact_solution.quantities)
        )
        return _check_failure(
            CheckFailureReason.WRONG_TIE_BREAK
            if request.mode != SearchMode.ADMISSION and same_tie_dimensions
            else CheckFailureReason.FALSE_OPTIMAL
            if request.mode != SearchMode.ADMISSION
            else CheckFailureReason.CLAIM_MISMATCH
        )
    if (
        evidence.objective_bounds != exact.objective_bounds
        or evidence.global_search_closed != exact.objective_bounds.closed
        or evaluation.payout_lower_bound_units != exact_proof.payout_lower_bound_units
        or evaluation.cost_upper_bound_units != exact_proof.cost_upper_bound_units
        or evaluation.guaranteed_profit_units != exact_proof.guaranteed_profit_units
        or evaluation.conservative_capital_release_at != exact_proof.conservative_capital_release_at
    ):
        return _check_failure(
            CheckFailureReason.FALSE_OPTIMAL
            if request.mode != SearchMode.ADMISSION and evidence.objective_bounds != exact.objective_bounds
            else CheckFailureReason.CLAIM_MISMATCH
        )
    return DifferentialCheck(BenchmarkClassification.CHECKED, exact, False, None)


def _fixed_claim_matches(problem: ArbitrageProblem, evidence: SolverEvidence, evaluation: PortfolioEvaluation) -> bool:
    assert evidence.candidate is not None
    if (
        evidence.candidate.quantities != evaluation.quantities
        or evidence.candidate.claimed_guaranteed_profit_units != evaluation.guaranteed_profit_units
        or evidence.payout_lower_bound_units != evaluation.payout_lower_bound_units
        or evidence.cost_upper_bound_units != evaluation.cost_upper_bound_units
        or evidence.guaranteed_profit_units != evaluation.guaranteed_profit_units
        or evidence.worst_scenario is None
        or not evidence.fixed_portfolio_closed
    ):
        return False
    try:
        cut = cut_from_scenario(problem, evidence.worst_scenario)
    except ValueError:
        return False
    quantities = {item.action_id: item.quantity_lots for item in evaluation.quantities}
    payout = sum(
        item.payout_lower_bound_per_lot_units * quantities.get(item.action_id, 0)
        for item in cut.payout_per_lot
    )
    return payout == evaluation.payout_lower_bound_units and cut in evidence.cuts


def _check_nonpositive_claim(evidence: SolverEvidence, exact: OracleResult) -> DifferentialCheck:
    if exact.solution is not None:
        return _check_failure(CheckFailureReason.FALSE_NEGATIVE)
    expected_native_status = (
        exact.business_status.value
        if exact.business_status != BusinessStatus.UNKNOWN
        else exact.unknown_reason.value
    )
    expected_closed = exact.business_status != BusinessStatus.UNKNOWN
    if (
        evidence.native_status != expected_native_status
        or evidence.objective_bounds != exact.objective_bounds
        or evidence.global_search_closed != expected_closed
    ):
        return _check_failure(CheckFailureReason.CLAIM_MISMATCH)
    return DifferentialCheck(BenchmarkClassification.CHECKED, exact, False, None)


def _is_negative_claim(evidence: SolverEvidence) -> bool:
    return (
        evidence.candidate is None
        and evidence.global_search_closed
        and evidence.native_status
        in {
            BusinessStatus.NO_QUALIFIED_OPPORTUNITY.value,
            BusinessStatus.NO_ARBITRAGE.value,
            "INFEASIBLE",
        }
    )


def _check_failure(reason: CheckFailureReason) -> DifferentialCheck:
    return DifferentialCheck(BenchmarkClassification.UNKNOWN, None, True, reason)


def _exact_case(case_id: str, source: BenchmarkCase | OracleRequest) -> dict[str, object]:
    request = source.request if isinstance(source, BenchmarkCase) else source
    result = source.expected_result if isinstance(source, BenchmarkCase) else _oracle_result(request)
    assert result is not None
    return {
        "case_id": case_id,
        "expected_result": canonical_payload(result),
        "request": canonical_payload(request),
        "request_fingerprint": fingerprint(request),
        "result_fingerprint": fingerprint(result),
        "truth_method": "exact_oracle_v1",
    }


def _oracle_result(request: OracleRequest) -> OracleResult:
    if request.mode == SearchMode.ADMISSION:
        return find_qualified(request)
    if request.mode == SearchMode.OPTIMIZATION:
        return solve_optimal(request)
    return diagnose_raw_arbitrage(request.problem, request.budget)


def _implies_exceptional(source: BenchmarkCase, token: str) -> OracleRequest:
    request = source.request
    state = request.problem.terminal_state_sets[0]
    action_id = next(action.action_id for action in request.problem.actions if action.market_contract_id == state.market_contract_id)
    exceptional = TerminalAtom(
        f"exception-{token}",
        TerminalKind.VOID,
        "v1",
        (ActionPayout(action_id, 100),),
        request.problem.as_of,
    )
    problem = replace(
        request.problem,
        problem_id=f"implies-exceptional-{token}",
        terminal_state_sets=(replace(state, atoms=(*state.atoms, exceptional)), *request.problem.terminal_state_sets[1:]),
    )
    return replace(request, problem=problem, budget=replace(request.budget, max_joint_states=6))


def _mutual_exclusion_exceptional(source: BenchmarkCase, token: str) -> OracleRequest:
    request = source.request
    state = request.problem.terminal_state_sets[0]
    action_id = next(action.action_id for action in request.problem.actions if action.market_contract_id == state.market_contract_id)
    exceptional = TerminalAtom(
        f"exception-{token}",
        TerminalKind.REFUND,
        "v1",
        (ActionPayout(action_id, 30),),
        request.problem.as_of,
    )
    relation = replace(request.problem.constraint_model.relations[0], kind=RelationKind.MUTUALLY_EXCLUSIVE)
    problem = replace(
        request.problem,
        problem_id=f"mutual-exclusion-exceptional-{token}",
        terminal_state_sets=(replace(state, atoms=(*state.atoms, exceptional)), *request.problem.terminal_state_sets[1:]),
        constraint_model=replace(request.problem.constraint_model, relations=(relation,)),
    )
    return replace(request, problem=problem, budget=replace(request.budget, max_joint_states=12))


def _piecewise_cost(source: BenchmarkCase, token: str) -> OracleRequest:
    request = source.request
    first = request.problem.actions[0]
    action = replace(
        first,
        cost_slices=(ExecutableCostSlice(1, 1, 44), ExecutableCostSlice(2, 2, 46)),
    )
    problem = replace(request.problem, problem_id=f"piecewise-cost-{token}", actions=(action, *request.problem.actions[1:]))
    return replace(request, problem=problem)


def _margin_boundary(source: BenchmarkCase, token: str) -> OracleRequest:
    request = source.request
    qualification = QualificationConstraint(
        f"margin-{token}",
        "v1",
        QualificationMetric.NET_MARGIN_PPM,
        Comparison.GREATER_THAN_OR_EQUAL,
        100_000,
        1,
    )
    return replace(
        request,
        problem=replace(request.problem, problem_id=f"margin-boundary-{token}", qualification_constraints=(qualification,)),
    )


def _annualized_round_up(source: BenchmarkCase, token: str, extra_seconds: int) -> OracleRequest:
    request = source.request
    release_at = request.problem.as_of + timedelta(days=1, seconds=extra_seconds)
    states = tuple(
        replace(state, atoms=tuple(replace(atom, capital_release_at=release_at) for atom in state.atoms))
        for state in request.problem.terminal_state_sets
    )
    qualification = QualificationConstraint(
        f"annualized-{token}",
        "v1",
        QualificationMetric.ANNUALIZED_RETURN_PPM,
        Comparison.GREATER_THAN_OR_EQUAL,
        20_277_777,
        1,
    )
    problem = replace(
        request.problem,
        problem_id=f"annualized-round-up-{token}",
        terminal_state_sets=states,
        qualification_constraints=(qualification,),
    )
    return replace(request, problem=problem)


def _release_delay_boundary(source: BenchmarkCase, token: str, delay_seconds: int) -> OracleRequest:
    request = source.request
    release_at = request.problem.as_of + timedelta(seconds=delay_seconds)
    states = tuple(
        replace(state, atoms=tuple(replace(atom, capital_release_at=release_at) for atom in state.atoms))
        for state in request.problem.terminal_state_sets
    )
    qualification = QualificationConstraint(
        f"release-{token}",
        "v1",
        QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS,
        Comparison.LESS_THAN_OR_EQUAL,
        delay_seconds,
        1,
    )
    problem = replace(
        request.problem,
        problem_id=f"release-delay-boundary-{token}",
        terminal_state_sets=states,
        qualification_constraints=(qualification,),
    )
    return replace(request, problem=problem)


def _unreachable_late_release(source: BenchmarkCase, token: str) -> OracleRequest:
    request = source.request
    state = request.problem.terminal_state_sets[0]
    late_atom_id = f"late-{token}"
    late = TerminalAtom(
        late_atom_id,
        TerminalKind.VOID,
        "v1",
        tuple(ActionPayout(action.action_id, 100) for action in request.problem.actions),
        request.problem.as_of + timedelta(days=30),
    )
    forbidden = ForbiddenAtomCombination(f"forbid-late-{token}", (late_atom_id,), "v1")
    problem = replace(
        request.problem,
        problem_id=f"unreachable-late-release-{token}",
        terminal_state_sets=(replace(state, atoms=(*state.atoms, late)),),
        constraint_model=ConstraintModel((), (forbidden,)),
    )
    return replace(request, problem=problem, budget=replace(request.budget, max_joint_states=3, max_support_rechecks=2))


def _same_observation_identity(source: BenchmarkCase, token: str) -> OracleRequest:
    request = source.request
    shared = request.problem.terminal_state_sets[0].settlement_observation_key
    second_contract = request.problem.terminal_state_sets[1].market_contract_id
    actions = tuple(
        replace(action, settlement_observation_key=shared)
        if action.market_contract_id == second_contract
        else action
        for action in request.problem.actions
    )
    states = tuple(
        replace(state, settlement_observation_key=shared)
        if state.market_contract_id == second_contract
        else state
        for state in request.problem.terminal_state_sets
    )
    problem = replace(request.problem, problem_id=f"same-observation-{token}", actions=actions, terminal_state_sets=states)
    return replace(request, problem=problem)


def _scale_case(size: int, *, dense: bool, token: str) -> dict[str, object]:
    base = load_canonical_cases()[0].request
    as_of = base.problem.as_of
    actions = []
    states = []
    contract_ids = tuple(f"scale-{token}-{size}-{index:02d}" for index in range(size))
    for index, contract_id in enumerate(contract_ids):
        action_id = f"action-{token}-{size}-{index:02d}"
        observation = SettlementObservationKey(
            base.problem.actions[0].settlement_observation_key.schema_version,
            f"oracle-{token}-{index:02d}",
            f"indicator-{token}-{index:02d}",
            as_of,
            as_of,
            "UTC",
            "v1",
        )
        actions.append(
            CandidateAction(
                action_id,
                f"venue-{token}",
                f"account-{token}",
                f"chain-{token}",
                contract_id,
                observation,
                base.problem.actions[0].side,
                1,
                1,
                1,
                1,
                "usd-cents",
                "usd-cents",
                "usd-cents-v1",
                (ExecutableCostSlice(1, 1, 100),),
            )
        )
        states.append(
            TerminalStateSet(
                contract_id,
                observation,
                "v1",
                (
                    TerminalAtom(f"atom-{token}-{index:02d}-no", TerminalKind.NORMAL_NO, "v1", (ActionPayout(action_id, 0),), as_of),
                    TerminalAtom(f"atom-{token}-{index:02d}-yes", TerminalKind.NORMAL_YES, "v1", (ActionPayout(action_id, 100),), as_of),
                ),
            )
        )
    relations = (
        (RelationConstraint(f"dense-{token}-{size}", RelationKind.EXACTLY_ONE, contract_ids, "v1"),)
        if dense
        else tuple(
            RelationConstraint(f"edge-{token}-{left}-{right}", RelationKind.IMPLIES, (left, right), "v1")
            for left, right in zip(contract_ids, contract_ids[1:], strict=False)
        )
    )
    problem = replace(
        base.problem,
        problem_id=f"scale-{size}-{'dense' if dense else 'sparse'}-{token}",
        actions=tuple(actions),
        terminal_state_sets=tuple(states),
        constraint_model=ConstraintModel(relations, ()),
        qualification_constraints=(),
    )
    request = OracleRequest(REQUEST_SCHEMA_V1, SearchMode.OPTIMIZATION, problem, OracleBudget(7, 7, 1))
    return {
        "case_id": f"scale_{size}_{'dense' if dense else 'sparse'}",
        "declared_search_surface": {
            "constraint_edges": size * (size - 1) // 2 if dense else size - 1,
            "joint_states": 2**size,
            "quantity_vectors": 2**size,
        },
        "expected_result": None,
        "request": canonical_payload(request),
        "request_fingerprint": fingerprint(request),
        "result_fingerprint": None,
        "truth_method": "measurement_only",
    }


def _approved_inbox_path(value: str | os.PathLike[str]) -> Path:
    text = os.fspath(value)
    lowered = text.lower()
    if "://" in lowered or lowered.startswith(("sqlite:", "http:", "https:")):
        raise ValueError("approved snapshot must be a local file")
    path = Path(text)
    if path.name != "approved_component.json" or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError("approved snapshot must be named approved_component.json")
    return path


def _problem_payload_for_gap_validation(payload: Mapping[str, object]) -> tuple[Mapping[str, object], bool]:
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return payload, False
    missing = False
    validated_actions = []
    for action in actions:
        if not isinstance(action, Mapping):
            validated_actions.append(action)
            continue
        identity = action.get("asset_valuation_rule_id")
        if "asset_valuation_rule_id" not in action or isinstance(identity, str) and not identity.strip():
            missing = True
            validated_actions.append({**action, "asset_valuation_rule_id": "missing-valuation-identity"})
        else:
            validated_actions.append(action)
    return ({**payload, "actions": validated_actions} if missing else payload), missing


def _load_approved_corpus(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("approved corpus must be readable JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "anonymization_salt", "cases", "input_gaps"}:
        raise ValueError("approved corpus has an invalid envelope")
    if payload["schema_version"] != _APPROVED_CORPUS_V1 or not _nonempty_text(payload["anonymization_salt"]):
        raise ValueError("approved corpus schema or anonymization salt is invalid")
    if not isinstance(payload["cases"], list) or not isinstance(payload["input_gaps"], list):
        raise ValueError("approved corpus cases and input_gaps must be arrays")
    return payload


def _append_input_gap(
    path: Path,
    corpus: dict[str, object],
    kind: str,
    source_alias: str,
    problem_payload: object,
) -> None:
    material = json.dumps(problem_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    gap_id = _anonymous_token(
        corpus["anonymization_salt"],
        "input_gap",
        f"{kind}\0{source_alias}\0{material}",
    )
    if any(gap.get("gap_id") == gap_id for gap in corpus["input_gaps"]):
        return
    corpus["input_gaps"].append(
        {
            "gap_id": gap_id,
            "reason": {
                "approval_provenance": "approved snapshot is missing required approval provenance",
                "terminal_payout": "approved snapshot is missing complete terminal payouts",
                "terminal_rule": "approved snapshot is missing terminal rule identity",
                "terminal_release": "approved snapshot is missing terminal capital release time",
                "valuation_identity": "approved snapshot is missing asset valuation identity",
            }[kind],
            "source_alias": source_alias,
        }
    )
    corpus["input_gaps"].sort(key=lambda gap: gap["gap_id"])
    _atomic_write_json(path, corpus)


def _anonymize_problem(problem: ArbitrageProblem, salt: str) -> ArbitrageProblem:
    token = lambda namespace, value: _anonymous_token(salt, namespace, value)

    def observation(value: SettlementObservationKey) -> SettlementObservationKey:
        return replace(
            value,
            oracle_id=token("observation_oracle", value.oracle_id),
            indicator_id=token("observation_indicator", value.indicator_id),
            rule_version=token("rule", value.rule_version),
        )

    actions = tuple(
        replace(
            action,
            action_id=token("action", action.action_id),
            venue_id=token("venue", action.venue_id),
            account_id=token("account", action.account_id),
            chain_id=token("chain", action.chain_id),
            market_contract_id=token("contract", action.market_contract_id),
            settlement_observation_key=observation(action.settlement_observation_key),
            settlement_asset_id=token("asset", action.settlement_asset_id),
            valuation_unit_id=token("asset", action.valuation_unit_id),
            asset_valuation_rule_id=token("valuation_rule", action.asset_valuation_rule_id),
        )
        for action in problem.actions
    )
    state_sets = tuple(
        replace(
            state,
            market_contract_id=token("contract", state.market_contract_id),
            settlement_observation_key=observation(state.settlement_observation_key),
            rule_version=token("rule", state.rule_version),
            atoms=tuple(
                replace(
                    atom,
                    atom_id=token("atom", atom.atom_id),
                    rule_version=token("rule", atom.rule_version),
                    payouts=tuple(
                        replace(payout, action_id=token("action", payout.action_id)) for payout in atom.payouts
                    ),
                )
                for atom in state.atoms
            ),
        )
        for state in problem.terminal_state_sets
    )
    relations = tuple(
        replace(
            relation,
            constraint_id=token("constraint", relation.constraint_id),
            contract_ids=tuple(token("contract", value) for value in relation.contract_ids),
            rule_version=token("rule", relation.rule_version),
        )
        for relation in problem.constraint_model.relations
    )
    forbidden = tuple(
        replace(
            item,
            constraint_id=token("constraint", item.constraint_id),
            atom_ids=tuple(token("atom", value) for value in item.atom_ids),
            rule_version=token("rule", item.rule_version),
        )
        for item in problem.constraint_model.forbidden_atom_combinations
    )
    qualifications = tuple(
        replace(
            item,
            constraint_id=token("constraint", item.constraint_id),
            rule_version=token("rule", item.rule_version),
        )
        for item in problem.qualification_constraints
    )
    return replace(
        problem,
        problem_id=token("problem", problem.problem_id),
        valuation_unit_id=token("asset", problem.valuation_unit_id),
        actions=actions,
        terminal_state_sets=state_sets,
        constraint_model=ConstraintModel(relations, forbidden),
        qualification_constraints=qualifications,
    )


def _anonymous_token(salt: str, namespace: str, value: str) -> str:
    return f"anon:{namespace}:{hashlib.sha256(f'{salt}\0{namespace}\0{value}'.encode()).hexdigest()}"


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _utc_z(value: str, name: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError(f"{name} must be a UTC timestamp ending in Z")
    try:
        decoded = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise ValueError(f"{name} must be a UTC timestamp ending in Z") from error
    if decoded.utcoffset() != UTC.utcoffset(decoded):
        raise ValueError(f"{name} must be UTC")
    return decoded


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
