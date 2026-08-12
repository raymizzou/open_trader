from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from fractions import Fraction
from itertools import combinations
from pathlib import Path

import pytest

import open_trader.prediction_solver_benchmark as benchmark
from open_trader.prediction_n_leg import (
    BusinessStatus,
    ActionQuantity,
    ObjectiveBounds,
    OptimalityStatus,
    PortfolioCandidate,
    ProofStatus,
    QualificationMetric,
    RelationKind,
    SearchMode,
    SolveStatus,
    TerminalKind,
    UnknownReason,
    canonical_payload,
    fingerprint,
    problem_from_payload,
    request_from_payload,
    result_from_payload,
    validate_problem,
)
from open_trader.prediction_n_leg_oracle import (
    build_relation_components,
    derive_selected_support_graph,
    diagnose_raw_arbitrage,
    evaluate_fixed_portfolio,
    find_qualified,
    solve_optimal,
)
from open_trader.prediction_solver import (
    BENCHMARK_PROTOCOL_V1,
    BenchmarkClassification,
    CertificateEvidence,
    SolverEvidence,
    TerminationReason,
    solve_with_constraint_generation,
)
from open_trader.prediction_solver_benchmark import (
    CheckFailureReason,
    aggregate_benchmark_records,
    check_solver_claim,
    generate_synthetic_corpus,
    generate_benchmark_report,
    import_approved_snapshot,
    load_canonical_cases,
    verify_benchmark_report,
)
from test_prediction_solver import BruteForceBackend, benchmark_limits


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_FIXTURE = ROOT / "tests" / "fixtures" / "prediction_n_leg_v1.json"
SYNTHETIC_CORPUS = ROOT / "benchmarks" / "prediction_solver" / "corpus" / "synthetic_v1.json"
APPROVED_CORPUS = ROOT / "benchmarks" / "prediction_solver" / "corpus" / "approved_v1.json"

SEMANTIC_CASE_IDS = (
    "single_contract_complement",
    "three_leg_exactly_one",
    "implies_exceptional",
    "mutual_exclusion_exceptional",
    "forbidden_atoms",
    "piecewise_cost",
    "quantity_bounds",
    "profit_boundary",
    "margin_boundary",
    "annualized_round_up",
    "release_delay_boundary",
    "unreachable_late_release",
    "disconnected_support",
    "same_observation_identity",
    "contradictory_terminal",
    "missing_terminal_data",
    "missing_valuation",
    "raw_no_arbitrage",
)
SCALE_CASE_IDS = (
    "scale_8_sparse",
    "scale_8_dense",
    "scale_16_sparse",
    "scale_16_dense",
    "scale_32_sparse",
    "scale_32_dense",
)


def test_canonical_corpus_loads_the_frozen_48_fixture_without_copying_it() -> None:
    fixture_bytes = CANONICAL_FIXTURE.read_bytes()
    cases = load_canonical_cases()

    assert hashlib.sha256(fixture_bytes).hexdigest() == "a4680fb2c66dedac9e85db9cd06d0872882ca69b09fba6d9f338d0b97243ecc7"
    assert len(cases) == 16
    assert tuple(case.case_id for case in cases) == tuple(
        item["case_id"] for item in json.loads(fixture_bytes)["cases"]
    )
    assert all(case.truth_method == "exact_oracle_v1" for case in cases)
    assert all(fingerprint(case.request) == case.request_fingerprint for case in cases)
    assert all(fingerprint(case.expected_result) == case.result_fingerprint for case in cases)
    assert all(case.budget == case.request.budget for case in cases)


def _synthetic_payload() -> dict[str, object]:
    return json.loads(generate_synthetic_corpus())


def test_synthetic_corpus_regenerates_byte_for_byte_with_a_non_circular_manifest() -> None:
    generated = generate_synthetic_corpus(4901)
    payload = json.loads(generated)
    manifest = dict(payload)
    stored_manifest = manifest.pop("manifest_sha256")

    assert generated == SYNTHETIC_CORPUS.read_bytes()
    assert generated.endswith(b"\n") and not generated.endswith(b"\n\n")
    assert stored_manifest == "sha256:" + hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert payload["seed"] == 4901
    assert payload["case_count"] == 24
    assert tuple(case["case_id"] for case in payload["cases"]) == SEMANTIC_CASE_IDS + SCALE_CASE_IDS
    assert generate_synthetic_corpus(4902) != generated


def test_synthetic_semantic_cases_store_exact_decoded_oracle_truth() -> None:
    cases = {case["case_id"]: case for case in _synthetic_payload()["cases"]}

    for case_id in SEMANTIC_CASE_IDS:
        case = cases[case_id]
        request = request_from_payload(case["request"])
        result = result_from_payload(case["expected_result"])

        assert case["truth_method"] == "exact_oracle_v1"
        assert fingerprint(request) == case["request_fingerprint"]
        assert fingerprint(result) == case["result_fingerprint"]
        assert canonical_payload(request) == case["request"]
        assert canonical_payload(result) == case["expected_result"]
        assert request.budget.max_quantity_vectors > 0
        assert request.budget.max_joint_states > 0
        assert request.budget.max_support_rechecks > 0
        assert result == (
            find_qualified(request)
            if request.mode == SearchMode.ADMISSION
            else solve_optimal(request)
            if request.mode == SearchMode.OPTIMIZATION
            else diagnose_raw_arbitrage(request.problem, request.budget)
        )


def test_synthetic_case_names_describe_real_semantic_dimensions() -> None:
    requests = {
        case["case_id"]: request_from_payload(case["request"])
        for case in _synthetic_payload()["cases"]
        if case["case_id"] in SEMANTIC_CASE_IDS
    }

    assert len(requests["single_contract_complement"].problem.terminal_state_sets) == 1
    assert {action.side.value for action in requests["single_contract_complement"].problem.actions} == {"BUY_YES", "BUY_NO"}
    assert requests["three_leg_exactly_one"].problem.constraint_model.relations[0].kind == RelationKind.EXACTLY_ONE
    assert len(requests["three_leg_exactly_one"].problem.actions) == 3
    for case_id, kind in (
        ("implies_exceptional", RelationKind.IMPLIES),
        ("mutual_exclusion_exceptional", RelationKind.MUTUALLY_EXCLUSIVE),
    ):
        problem = requests[case_id].problem
        assert any(relation.kind == kind for relation in problem.constraint_model.relations)
        assert any(atom.kind not in {TerminalKind.NORMAL_YES, TerminalKind.NORMAL_NO} for state in problem.terminal_state_sets for atom in state.atoms)
    assert requests["forbidden_atoms"].problem.constraint_model.forbidden_atom_combinations
    assert any(
        len(action.cost_slices) > 1
        and len({item.incremental_cost_upper_bound_units for item in action.cost_slices}) > 1
        for action in requests["piecewise_cost"].problem.actions
    )
    assert any(action.max_quantity_lots > action.min_quantity_lots for action in requests["quantity_bounds"].problem.actions)
    assert {item.metric for item in requests["profit_boundary"].problem.qualification_constraints} == {QualificationMetric.GUARANTEED_PROFIT_UNITS}
    assert {item.metric for item in requests["margin_boundary"].problem.qualification_constraints} == {QualificationMetric.NET_MARGIN_PPM}
    annualized = requests["annualized_round_up"]
    assert {item.metric for item in annualized.problem.qualification_constraints} == {QualificationMetric.ANNUALIZED_RETURN_PPM}
    assert max(atom.capital_release_at for state in annualized.problem.terminal_state_sets for atom in state.atoms) > annualized.problem.as_of + timedelta(days=1)
    assert {item.metric for item in requests["release_delay_boundary"].problem.qualification_constraints} == {QualificationMetric.MAX_CAPITAL_RELEASE_DELAY_SECONDS}
    late = requests["unreachable_late_release"]
    late_result = result_from_payload(next(case["expected_result"] for case in _synthetic_payload()["cases"] if case["case_id"] == "unreachable_late_release"))
    assert late_result.solution is not None
    assert late_result.solution.payout_proof.conservative_capital_release_at < max(
        atom.capital_release_at for state in late.problem.terminal_state_sets for atom in state.atoms
    )
    assert len(build_relation_components(requests["disconnected_support"].problem)) > 1
    assert len(build_relation_components(requests["same_observation_identity"].problem)) == 1
    assert result_from_payload(next(case["expected_result"] for case in _synthetic_payload()["cases"] if case["case_id"] == "contradictory_terminal")).unknown_reason.value == "CONTRADICTORY_CONSTRAINT_MODEL"
    assert result_from_payload(next(case["expected_result"] for case in _synthetic_payload()["cases"] if case["case_id"] == "missing_terminal_data")).unknown_reason.value == "UNKNOWN_TERMINAL_DATA"
    assert result_from_payload(next(case["expected_result"] for case in _synthetic_payload()["cases"] if case["case_id"] == "missing_valuation")).unknown_reason.value == "UNKNOWN_VALUATION"
    assert requests["raw_no_arbitrage"].mode == SearchMode.RAW_ARBITRAGE_DIAGNOSTIC


def test_synthetic_scale_cases_are_measurement_only_and_exceed_their_oracle_budgets() -> None:
    cases = {case["case_id"]: case for case in _synthetic_payload()["cases"]}

    for size in (8, 16, 32):
        sparse = cases[f"scale_{size}_sparse"]
        dense = cases[f"scale_{size}_dense"]
        for case in (sparse, dense):
            request = request_from_payload(case["request"])
            surface = case["declared_search_surface"]
            assert case["truth_method"] == "measurement_only"
            assert case["expected_result"] is None
            assert case["result_fingerprint"] is None
            assert len(request.problem.terminal_state_sets) == size
            assert request.budget.max_quantity_vectors < surface["quantity_vectors"]
            assert request.budget.max_joint_states < surface["joint_states"]
            assert fingerprint(request) == case["request_fingerprint"]
        sparse_request = request_from_payload(sparse["request"])
        dense_request = request_from_payload(dense["request"])
        assert len(dense_request.problem.constraint_model.relations) == 1
        assert dense_request.problem.constraint_model.relations[0].kind == RelationKind.EXACTLY_ONE
        assert len(dense_request.problem.constraint_model.relations[0].contract_ids) == size
        assert _constraint_graph_edges(dense_request) > _constraint_graph_edges(sparse_request)


def _constraint_graph_edges(request) -> set[tuple[str, str]]:
    return {
        pair
        for relation in request.problem.constraint_model.relations
        for pair in combinations(sorted(relation.contract_ids), 2)
    }


def _approved_envelope() -> dict[str, object]:
    request = canonical_payload(
        next(case.request for case in load_canonical_cases() if case.case_id == "exactly-one-n3")
    )
    request["problem"]["constraint_model"]["forbidden_atom_combinations"] = [
        {
            "atom_ids": ["a-yes", "b-yes"],
            "constraint_id": "redundant-forbidden-pair",
            "rule_version": "v1",
        }
    ]
    return {
        "schema_version": "open_trader.prediction_solver.approved_envelope.v1",
        "source_alias": "approved-component-alpha",
        "approval_id": "approval-live-123",
        "generation_id": "generation-live-456",
        "approver": "operator@example.test",
        "approved_at": "2026-08-12T01:02:03Z",
        "captured_at": "2026-08-12T01:03:04Z",
        "problem": request["problem"],
    }


def _temporary_approved_paths(tmp_path: Path) -> tuple[Path, Path]:
    inbox = tmp_path / "approved_component.json"
    corpus = tmp_path / "approved_v1.json"
    corpus.write_bytes(APPROVED_CORPUS.read_bytes())
    return inbox, corpus


def test_approved_corpus_starts_empty_with_the_known_legacy_gap() -> None:
    payload = json.loads(APPROVED_CORPUS.read_bytes())

    assert payload["schema_version"] == "open_trader.prediction_solver.approved_corpus.v1"
    assert payload["anonymization_salt"]
    assert payload["cases"] == []
    assert payload["input_gaps"] == [
        {
            "gap_id": "legacy-incomplete-terminal-model",
            "reason": "approved legacy relations/signals/previews do not contain a complete #48 terminal model",
            "source_alias": "legacy-approved-prediction-data",
        }
    ]


def test_approved_snapshot_import_anonymizes_every_identity_and_deduplicates(tmp_path: Path) -> None:
    inbox, corpus = _temporary_approved_paths(tmp_path)
    envelope = _approved_envelope()
    inbox.write_text(json.dumps(envelope))
    source_problem = problem_from_payload(envelope["problem"])

    import_approved_snapshot(inbox, corpus)
    import_approved_snapshot(inbox, corpus)

    payload = json.loads(corpus.read_bytes())
    assert len(payload["cases"]) == 1
    case = payload["cases"][0]
    anonymized = problem_from_payload(case["problem"])
    assert case["source_alias"] == envelope["source_alias"]
    assert case["approved_at"] == envelope["approved_at"]
    assert case["captured_at"] == envelope["captured_at"]
    assert case["approval_id"] != envelope["approval_id"]
    assert case["generation_id"] != envelope["generation_id"]
    assert case["approver"] != envelope["approver"]
    assert len(case["approval_id"].rsplit(":", 1)[-1]) == 64
    assert len(case["generation_id"].rsplit(":", 1)[-1]) == 64
    assert len(case["approver"].rsplit(":", 1)[-1]) == 64
    assert case["source_problem_fingerprint"] == fingerprint(source_problem)
    assert case["anonymized_problem_fingerprint"] == fingerprint(anonymized)
    assert not validate_problem(anonymized)
    serialized = json.dumps(case["problem"], sort_keys=True)
    for source_identity in (
        source_problem.problem_id,
        source_problem.actions[0].action_id,
        source_problem.actions[0].market_contract_id,
        source_problem.actions[0].venue_id,
        source_problem.actions[0].account_id,
        source_problem.actions[0].chain_id,
        source_problem.actions[0].settlement_observation_key.oracle_id,
        source_problem.actions[0].settlement_observation_key.indicator_id,
        source_problem.actions[0].settlement_asset_id,
        source_problem.actions[0].asset_valuation_rule_id,
        source_problem.terminal_state_sets[0].atoms[0].atom_id,
        source_problem.constraint_model.relations[0].constraint_id,
        source_problem.constraint_model.forbidden_atom_combinations[0].constraint_id,
        source_problem.qualification_constraints[0].constraint_id,
    ):
        assert json.dumps(source_identity) not in serialized
    assert anonymized.actions[0].valuation_unit_id == anonymized.valuation_unit_id
    assert anonymized.actions[0].settlement_asset_id == anonymized.valuation_unit_id
    state = next(
        item for item in anonymized.terminal_state_sets if item.market_contract_id == anonymized.actions[0].market_contract_id
    )
    assert anonymized.actions[0].settlement_observation_key == state.settlement_observation_key
    assert anonymized.actions[0].settlement_observation_key.rule_version == state.rule_version
    assert state.rule_version == state.atoms[0].rule_version
    assert anonymized.constraint_model.relations[0].rule_version == anonymized.constraint_model.forbidden_atom_combinations[0].rule_version
    assert anonymized.actions[0].side == source_problem.actions[0].side
    assert anonymized.actions[0].cost_slices == source_problem.actions[0].cost_slices
    assert anonymized.as_of == source_problem.as_of


@pytest.mark.parametrize(
    "inbox_value",
    (
        "https://example.test/approved_component.json",
        "sqlite:///tmp/approved_component.json",
        "approved_component.sqlite3",
        "approved_component.db",
    ),
)
def test_approved_snapshot_rejects_urls_and_database_paths(tmp_path: Path, inbox_value: str) -> None:
    _, corpus = _temporary_approved_paths(tmp_path)

    with pytest.raises(ValueError):
        import_approved_snapshot(inbox_value, corpus)


@pytest.mark.parametrize("unknown_location", ("envelope", "problem"))
def test_approved_snapshot_rejects_unknown_input_instead_of_retaining_it(tmp_path: Path, unknown_location: str) -> None:
    inbox, corpus = _temporary_approved_paths(tmp_path)
    envelope = _approved_envelope()
    target = envelope if unknown_location == "envelope" else envelope["problem"]
    target["unexpected"] = "must not survive"
    inbox.write_text(json.dumps(envelope))
    before = corpus.read_bytes()

    with pytest.raises(ValueError):
        import_approved_snapshot(inbox, corpus)

    assert corpus.read_bytes() == before


@pytest.mark.parametrize("malformation", ("wrong_schema", "unknown_problem_key"))
def test_approved_snapshot_does_not_hide_malformed_input_behind_a_provenance_gap(
    tmp_path: Path,
    malformation: str,
) -> None:
    inbox, corpus = _temporary_approved_paths(tmp_path)
    envelope = _approved_envelope()
    envelope.pop("approver")
    if malformation == "wrong_schema":
        envelope["schema_version"] = "unsupported.approval.v9"
    else:
        envelope["problem"]["unexpected"] = True
    inbox.write_text(json.dumps(envelope))
    before = corpus.read_bytes()

    with pytest.raises(ValueError):
        import_approved_snapshot(inbox, corpus)

    assert corpus.read_bytes() == before


@pytest.mark.parametrize(
    ("missing_kind", "mutate"),
    (
        ("approval_provenance", lambda envelope: envelope.pop("approver")),
        ("terminal_release", lambda envelope: envelope["problem"]["terminal_state_sets"][0]["atoms"][0].pop("capital_release_at")),
        ("terminal_rule", lambda envelope: envelope["problem"]["terminal_state_sets"][0]["atoms"][0].pop("rule_version")),
        ("terminal_payout", lambda envelope: envelope["problem"]["terminal_state_sets"][0]["atoms"][0].__setitem__("payouts", [])),
        (
            "valuation_identity",
            lambda envelope: (
                envelope["problem"]["actions"][0].__setitem__("settlement_asset_id", "other-asset"),
                envelope["problem"]["actions"][0].__setitem__("asset_valuation_rule_id", ""),
            ),
        ),
        (
            "valuation_identity",
            lambda envelope: (
                envelope["problem"]["actions"][0].__setitem__("settlement_asset_id", "other-asset"),
                envelope["problem"]["actions"][0].pop("asset_valuation_rule_id"),
            ),
        ),
    ),
)
def test_approved_snapshot_records_missing_input_as_one_deduplicated_gap(
    tmp_path: Path,
    missing_kind: str,
    mutate,
) -> None:
    inbox, corpus = _temporary_approved_paths(tmp_path)
    envelope = deepcopy(_approved_envelope())
    mutate(envelope)
    inbox.write_text(json.dumps(envelope))

    import_approved_snapshot(inbox, corpus)
    import_approved_snapshot(inbox, corpus)

    payload = json.loads(corpus.read_bytes())
    assert payload["cases"] == []
    generated = [gap for gap in payload["input_gaps"] if gap["gap_id"].startswith("anon:input_gap:")]
    assert len(generated) == 1
    assert missing_kind.replace("_", " ").split()[0] in generated[0]["reason"]


def test_approved_snapshot_gap_identity_uses_the_corpus_salt_without_emitting_raw_ids(tmp_path: Path) -> None:
    envelope = deepcopy(_approved_envelope())
    envelope["problem"]["terminal_state_sets"][0]["atoms"][0].pop("capital_release_at")
    raw_problem_id = envelope["problem"]["problem_id"]
    gap_ids = []

    for index, salt in enumerate(("salt-alpha", "salt-beta")):
        directory = tmp_path / str(index)
        directory.mkdir()
        inbox, corpus = _temporary_approved_paths(directory)
        corpus_payload = json.loads(corpus.read_bytes())
        corpus_payload["anonymization_salt"] = salt
        corpus.write_text(json.dumps(corpus_payload))
        inbox.write_text(json.dumps(envelope))

        import_approved_snapshot(inbox, corpus)
        import_approved_snapshot(inbox, corpus)

        gaps = [gap for gap in json.loads(corpus.read_bytes())["input_gaps"] if gap["gap_id"].startswith("anon:input_gap:")]
        assert len(gaps) == 1
        assert raw_problem_id not in json.dumps(gaps[0])
        gap_ids.append(gaps[0]["gap_id"])

    assert gap_ids[0] != gap_ids[1]


def _certificate() -> CertificateEvidence:
    return CertificateEvidence(
        certificate_sha256="sha256:" + "a" * 64,
        certificate_size_bytes=10,
        completed_certificate_sha256="sha256:" + "b" * 64,
        completed_certificate_size_bytes=12,
        checker_name="viprchk",
        checker_version="1.0",
        checker_exit_code=0,
        checker_succeeded=True,
        generation_ns=1,
        completion_ns=2,
        check_ns=3,
    )


def _evidence_for_result(result, *, certificate: CertificateEvidence | None = None) -> SolverEvidence:
    if result.solution is not None:
        proof = result.solution.payout_proof
        return SolverEvidence(
            native_status=result.business_status.value,
            candidate=PortfolioCandidate(result.solution.quantities, proof.guaranteed_profit_units),
            objective_bounds=result.objective_bounds,
            worst_scenario=proof.worst_scenario,
            payout_lower_bound_units=proof.payout_lower_bound_units,
            cost_upper_bound_units=proof.cost_upper_bound_units,
            guaranteed_profit_units=proof.guaranteed_profit_units,
            conservative_capital_release_at=proof.conservative_capital_release_at,
            fixed_portfolio_closed=True,
            global_search_closed=result.objective_bounds.closed,
            master_rounds=1,
            adversary_rounds=1,
            cuts=(proof.worst_state_cut,),
            certificate=certificate,
        )
    return SolverEvidence(
        native_status=(result.business_status.value if result.business_status != BusinessStatus.UNKNOWN else result.unknown_reason.value),
        candidate=None,
        objective_bounds=result.objective_bounds,
        worst_scenario=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        fixed_portfolio_closed=False,
        global_search_closed=result.business_status != BusinessStatus.UNKNOWN,
        master_rounds=1,
        adversary_rounds=1,
        cuts=(),
        certificate=certificate,
    )


def _evidence_for_quantities(request, quantities: tuple[ActionQuantity, ...]) -> SolverEvidence:
    evaluation = evaluate_fixed_portfolio(request.problem, quantities, request.budget)
    profit = evaluation.guaranteed_profit_units
    return SolverEvidence(
        native_status="OPTIMAL",
        candidate=PortfolioCandidate(evaluation.quantities, profit),
        objective_bounds=ObjectiveBounds(profit, profit, 0, True),
        worst_scenario=evaluation.worst_scenario,
        payout_lower_bound_units=evaluation.payout_lower_bound_units,
        cost_upper_bound_units=evaluation.cost_upper_bound_units,
        guaranteed_profit_units=profit,
        conservative_capital_release_at=evaluation.conservative_capital_release_at,
        fixed_portfolio_closed=True,
        global_search_closed=True,
        master_rounds=1,
        adversary_rounds=1,
        cuts=(evaluation.worst_state_cut,),
        certificate=None,
    )


def test_checker_attaches_only_the_unchanged_exact_oracle_result_for_all_48_cases() -> None:
    for case in load_canonical_cases():
        assert case.expected_result is not None
        checked = check_solver_claim(
            case.request,
            _evidence_for_result(case.expected_result),
            claimed_problem_fingerprint=fingerprint(case.request.problem),
            truth_method="exact_oracle_v1",
        )

        assert checked.classification == BenchmarkClassification.CHECKED, case.case_id
        assert checked.canonical_result == case.expected_result, case.case_id
        assert checked.hard_failure is False
        assert checked.failure_reason is None


@pytest.mark.parametrize("case_id", ("void-refund-split", "no-arbitrage"))
def test_checker_accepts_honest_common_engine_negative_evidence_with_retained_diagnostics(case_id: str) -> None:
    case = next(item for item in load_canonical_cases() if item.case_id == case_id)
    evidence = solve_with_constraint_generation(case.request, BruteForceBackend(), benchmark_limits(100))
    assert evidence.candidate is None
    assert evidence.cuts
    if case_id == "no-arbitrage":
        assert evidence.worst_scenario is not None
        assert evidence.payout_lower_bound_units is not None
        assert evidence.cost_upper_bound_units is not None
        assert evidence.guaranteed_profit_units is not None
        assert evidence.conservative_capital_release_at is not None

    checked = check_solver_claim(
        case.request,
        evidence,
        claimed_problem_fingerprint=fingerprint(case.request.problem),
        truth_method="exact_oracle_v1",
    )

    assert checked.classification == BenchmarkClassification.CHECKED
    assert checked.canonical_result == case.expected_result
    assert checked.hard_failure is False
    assert checked.failure_reason is None


@pytest.mark.parametrize(
    ("case_id", "unknown_reason"),
    (
        ("unknown-state-budget", UnknownReason.ORACLE_STATE_LIMIT_EXCEEDED),
        ("unknown-decision-budget", UnknownReason.ORACLE_DECISION_LIMIT_EXCEEDED),
    ),
)
def test_checker_keeps_a_locally_checked_positive_as_measurement_when_exact_oracle_budget_is_unclosed(
    case_id: str,
    unknown_reason: UnknownReason,
) -> None:
    case = next(item for item in load_canonical_cases() if item.case_id == case_id)
    assert case.expected_result is not None and case.expected_result.unknown_reason == unknown_reason
    evidence = solve_with_constraint_generation(case.request, BruteForceBackend(), benchmark_limits(100))
    assert evidence.candidate is not None

    checked = check_solver_claim(
        case.request,
        evidence,
        claimed_problem_fingerprint=fingerprint(case.request.problem),
        truth_method="exact_oracle_v1",
    )

    assert checked.classification == BenchmarkClassification.MEASUREMENT_ONLY
    assert checked.canonical_result is None
    assert checked.hard_failure is False
    assert checked.failure_reason is None


def test_checker_keeps_large_decision_limited_recheck_within_the_frozen_state_budget(monkeypatch) -> None:
    payload = next(case for case in _synthetic_payload()["cases"] if case["case_id"] == "scale_32_dense")
    request = request_from_payload(payload["request"])
    exact = solve_optimal(request)
    assert exact.unknown_reason == UnknownReason.ORACLE_DECISION_LIMIT_EXCEEDED
    action = request.problem.actions[0]
    evidence = SolverEvidence(
        native_status="FEASIBLE",
        candidate=PortfolioCandidate((ActionQuantity(action.action_id, 1),), 0),
        objective_bounds=ObjectiveBounds(None, None, None, False),
        worst_scenario=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        fixed_portfolio_closed=False,
        global_search_closed=False,
        master_rounds=1,
        adversary_rounds=1,
        cuts=(),
        certificate=None,
    )
    real_evaluate = benchmark.evaluate_fixed_portfolio
    seen_budgets = []

    def evaluate_with_budget_guard(problem, quantities, budget):
        seen_budgets.append(budget)
        assert budget == request.budget
        return real_evaluate(problem, quantities, budget)

    monkeypatch.setattr(benchmark, "evaluate_fixed_portfolio", evaluate_with_budget_guard)

    checked = check_solver_claim(
        request,
        evidence,
        claimed_problem_fingerprint=fingerprint(request.problem),
        truth_method="exact_oracle_v1",
    )

    assert seen_budgets == [request.budget]
    assert checked.classification == BenchmarkClassification.MEASUREMENT_ONLY
    assert checked.canonical_result is None
    assert checked.hard_failure is False
    assert checked.failure_reason is None


@pytest.mark.parametrize(
    ("tampered_field", "failure_reason"),
    (
        ("conservative_capital_release_at", CheckFailureReason.WRONG_RELEASE),
        ("payout_lower_bound_units", CheckFailureReason.CLAIM_MISMATCH),
    ),
)
def test_checker_rejects_tampered_fixed_claim_before_bounded_support_fallback(
    tampered_field: str,
    failure_reason: CheckFailureReason,
) -> None:
    case = next(item for item in load_canonical_cases() if item.case_id == "exactly-one-n3")
    assert case.expected_result is not None and case.expected_result.solution is not None
    payload = canonical_payload(case.request)
    payload["budget"] = {"max_quantity_vectors": 1, "max_joint_states": 8, "max_support_rechecks": 1}
    payload["problem"]["constraint_model"]["forbidden_atom_combinations"] = [
        {
            "atom_ids": ["a-yes", "b-yes"],
            "constraint_id": "redundant-forbidden-pair",
            "rule_version": "v1",
        }
    ]
    request = request_from_payload(payload)
    assert find_qualified(request).unknown_reason == UnknownReason.ORACLE_DECISION_LIMIT_EXCEEDED
    evaluation = evaluate_fixed_portfolio(request.problem, case.expected_result.solution.quantities, request.budget)
    assert derive_selected_support_graph(request.problem, evaluation, request.budget) == UnknownReason.ORACLE_SUPPORT_LIMIT_EXCEEDED
    evidence = _evidence_for_quantities(request, evaluation.quantities)
    evidence = replace(
        evidence,
        **{
            tampered_field: (
                evidence.conservative_capital_release_at + timedelta(seconds=1)
                if tampered_field == "conservative_capital_release_at"
                else evidence.payout_lower_bound_units + 1
            )
        },
    )

    _assert_hard_checker_failure(request, evidence, failure_reason)


def test_checker_rejects_a_profitable_claim_for_an_actually_lossy_portfolio() -> None:
    case = next(item for item in load_canonical_cases() if item.case_id == "rounded-false-edge")
    evidence = _evidence_for_quantities(case.request, (ActionQuantity("a", 1),))
    evidence = replace(
        evidence,
        candidate=PortfolioCandidate(evidence.candidate.quantities, 1),
        guaranteed_profit_units=1,
        objective_bounds=ObjectiveBounds(1, 1, 0, True),
    )

    _assert_hard_checker_failure(case.request, evidence, CheckFailureReason.FALSE_SAFE)


def test_checker_rejects_false_infeasibility() -> None:
    case = next(item for item in load_canonical_cases() if item.case_id == "native-complement-n2")
    evidence = SolverEvidence(
        native_status=BusinessStatus.NO_QUALIFIED_OPPORTUNITY.value,
        candidate=None,
        objective_bounds=ObjectiveBounds(None, None, None, False),
        worst_scenario=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        fixed_portfolio_closed=False,
        global_search_closed=True,
        master_rounds=1,
        adversary_rounds=0,
        cuts=(),
        certificate=None,
    )

    _assert_hard_checker_failure(case.request, evidence, CheckFailureReason.FALSE_NEGATIVE)


def test_checker_rejects_false_optimality() -> None:
    case = next(item for item in load_canonical_cases() if item.case_id == "quantity-selection-n4")
    evidence = _evidence_for_quantities(
        case.request,
        (ActionQuantity("b", 1), ActionQuantity("c", 1), ActionQuantity("d", 1)),
    )

    _assert_hard_checker_failure(case.request, evidence, CheckFailureReason.FALSE_OPTIMAL)


def test_checker_rejects_the_wrong_canonical_tie_break() -> None:
    source = next(item for item in load_canonical_cases() if item.case_id == "disconnected-double-arbitrage")
    request = replace(source.request, mode=SearchMode.OPTIMIZATION)
    exact = solve_optimal(request)
    assert exact.solution is not None
    assert exact.solution.quantities == (ActionQuantity("component-a", 1),)
    evidence = _evidence_for_quantities(request, (ActionQuantity("component-z", 1),))

    _assert_hard_checker_failure(request, evidence, CheckFailureReason.WRONG_TIE_BREAK)


def test_checker_rejects_wrong_release() -> None:
    case = next(item for item in load_canonical_cases() if item.case_id == "native-complement-n2")
    evidence = _evidence_for_result(case.expected_result)
    evidence = replace(evidence, conservative_capital_release_at=evidence.conservative_capital_release_at + timedelta(seconds=1))

    _assert_hard_checker_failure(case.request, evidence, CheckFailureReason.WRONG_RELEASE)


def test_checker_rejects_disconnected_selected_support() -> None:
    case = next(item for item in load_canonical_cases() if item.case_id == "disconnected-double-arbitrage")
    evidence = _evidence_for_quantities(
        case.request,
        (ActionQuantity("component-a", 1), ActionQuantity("component-z", 1)),
    )

    _assert_hard_checker_failure(case.request, evidence, CheckFailureReason.DISCONNECTED_SUPPORT)


def test_checker_rejects_changed_problem_fingerprint_before_any_claim() -> None:
    case = load_canonical_cases()[0]

    _assert_hard_checker_failure(
        case.request,
        _evidence_for_result(case.expected_result),
        CheckFailureReason.CHANGED_PROBLEM_FINGERPRINT,
        claimed_problem_fingerprint="sha256:" + "0" * 64,
    )


def test_checker_rejects_an_unverified_large_negative() -> None:
    case = load_canonical_cases()[0]
    evidence = replace(
        _evidence_for_result(case.expected_result),
        native_status=BusinessStatus.NO_QUALIFIED_OPPORTUNITY.value,
        candidate=None,
        objective_bounds=ObjectiveBounds(None, None, None, False),
        worst_scenario=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        fixed_portfolio_closed=False,
        global_search_closed=True,
        cuts=(),
    )

    _assert_hard_checker_failure(case.request, evidence, CheckFailureReason.UNVERIFIED_NEGATIVE, truth_method="measurement_only")


def test_checker_retains_measurements_but_rejects_an_unbound_successful_negative_certificate() -> None:
    case = load_canonical_cases()[0]
    positive = check_solver_claim(
        case.request,
        _evidence_for_result(case.expected_result),
        claimed_problem_fingerprint=fingerprint(case.request.problem),
        truth_method="measurement_only",
    )
    negative = replace(
        _evidence_for_result(case.expected_result),
        native_status=BusinessStatus.NO_QUALIFIED_OPPORTUNITY.value,
        candidate=None,
        objective_bounds=ObjectiveBounds(None, None, None, False),
        worst_scenario=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        fixed_portfolio_closed=False,
        global_search_closed=True,
        cuts=(),
        certificate=_certificate(),
    )
    certified = check_solver_claim(
        case.request,
        negative,
        claimed_problem_fingerprint=fingerprint(case.request.problem),
        truth_method="measurement_only",
    )
    unknown_case = next(item for item in load_canonical_cases() if item.case_id == "unknown-state-budget")
    unknown = check_solver_claim(
        unknown_case.request,
        _evidence_for_result(unknown_case.expected_result),
        claimed_problem_fingerprint=fingerprint(unknown_case.request.problem),
        truth_method="measurement_only",
    )

    assert positive.classification == BenchmarkClassification.MEASUREMENT_ONLY
    assert positive.canonical_result is None
    assert positive.hard_failure is False
    assert certified.classification == BenchmarkClassification.UNKNOWN
    assert certified.canonical_result is None
    assert certified.hard_failure is True
    assert certified.failure_reason == CheckFailureReason.UNVERIFIED_NEGATIVE
    assert unknown.classification == BenchmarkClassification.MEASUREMENT_ONLY
    assert unknown.canonical_result is None
    assert unknown.hard_failure is False


def _assert_hard_checker_failure(
    request,
    evidence: SolverEvidence,
    reason: CheckFailureReason,
    *,
    claimed_problem_fingerprint: str | None = None,
    truth_method: str = "exact_oracle_v1",
) -> None:
    checked = check_solver_claim(
        request,
        evidence,
        claimed_problem_fingerprint=claimed_problem_fingerprint or fingerprint(request.problem),
        truth_method=truth_method,
    )

    assert checked.classification == BenchmarkClassification.UNKNOWN
    assert checked.hard_failure is True
    assert checked.failure_reason == reason
    assert checked.canonical_result is None


_RECORD_SCHEMA_V1 = "open_trader.prediction_solver.record.v1"
_MANIFEST_SCHEMA_V1 = "open_trader.prediction_solver.run_manifest.v1"
_SHA_A = "sha256:" + "a" * 64
_SHA_B = "sha256:" + "b" * 64
_SHA_C = "sha256:" + "c" * 64
_SHA_D = "sha256:" + "d" * 64


def _record_semantic_fingerprint(record: dict[str, object]) -> str:
    run = record["solver_run"]
    evidence = run["evidence"]
    payload = {
        "business_status": run["business_status"],
        "canonical_result": run["canonical_result"],
        "check_failure_reason": record["check_failure_reason"],
        "check_hard_failure": record["check_hard_failure"],
        "classification": run["classification"],
        "evidence": None if evidence is None else {
            **{
                field: evidence[field]
                for field in (
                "adversary_rounds", "candidate", "conservative_capital_release_at",
                "cost_upper_bound_units", "cuts", "fixed_portfolio_closed",
                "global_search_closed", "guaranteed_profit_units", "master_rounds",
                "native_status", "objective_bounds", "payout_lower_bound_units",
                "worst_scenario",
                )
            },
            "certificate": None if evidence["certificate"] is None else {
                field: evidence["certificate"][field]
                for field in (
                    "certificate_sha256", "certificate_size_bytes",
                    "completed_certificate_sha256", "completed_certificate_size_bytes",
                    "checker_name", "checker_version", "checker_exit_code", "checker_succeeded",
                )
            },
        },
        "objective_bounds": run["objective_bounds"],
        "optimality_status": run["optimality_status"],
        "proof_status": run["proof_status"],
        "solve_status": run["solve_status"],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _benchmark_solver_run(*, wall_ns: int, first_ns: int, optimal_ns: int, solver: str = "highs") -> dict[str, object]:
    return {
        "adapter_version": "adapter-v1",
        "business_status": "QUALIFIED_FEASIBLE",
        "canonical_result": None,
        "classification": "MEASUREMENT_ONLY",
        "diagnostics": [],
        "environment_id": "mac-build",
        "evidence": {
            "adversary_rounds": 2,
            "candidate": {"claimed_guaranteed_profit_units": 7, "quantities": [{"action_id": "a", "quantity_lots": 1}]},
            "certificate": None,
            "conservative_capital_release_at": "2026-08-12T00:00:00Z",
            "cost_upper_bound_units": 3,
            "cuts": [],
            "fixed_portfolio_closed": True,
            "global_search_closed": False,
            "guaranteed_profit_units": 7,
            "master_rounds": 3,
            "native_status": "FEASIBLE",
            "objective_bounds": {"closed": False, "gap_units": 2, "lower_bound_units": 7, "upper_bound_units": 9},
            "payout_lower_bound_units": 10,
            "worst_scenario": {"atoms": [{"atom_id": "yes", "market_contract_id": "a"}]},
        },
        "objective_bounds": {"closed": False, "gap_units": 2, "lower_bound_units": 7, "upper_bound_units": 9},
        "optimality_status": "NOT_PROVEN",
        "peak_rss_bytes": 600,
        "phase_timings_ns": [
            ["backend", max(0, wall_ns - 6)],
            ["certificate_check", 0],
            ["certificate_completion", 0],
            ["certificate_generation", 0],
            ["first_qualified", max(0, first_ns)],
            ["independent_check", 1],
            ["optimal", max(0, optimal_ns)],
            ["serialization", 2],
        ],
        "proof_status": "UNKNOWN",
        "request_id": "request-1",
        "schema_version": BENCHMARK_PROTOCOL_V1,
        "solve_status": "FEASIBLE",
        "solver_name": solver,
        "solver_version": "1.0",
        "termination_reason": TerminationReason.COMPLETED.value,
        "worker_id": "worker-1",
    }


def _benchmark_record(
    *,
    environment: str = "macos",
    solver: str = "highs",
    sample_kind: str = "warm",
    sample_index: int = 0,
    wall_ns: int = 100,
    worker_count: int = 1,
) -> dict[str, object]:
    run = _benchmark_solver_run(wall_ns=wall_ns, first_ns=wall_ns - 10, optimal_ns=wall_ns - 5, solver=solver)
    run["environment_id"] = f"{environment}-build"
    run["worker_id"] = f"worker-{worker_count}"
    record = {
        "adapter_version": "adapter-v1",
        "architecture": "arm64" if environment == "macos" else "x86_64",
        "case_id": "case-a",
        "check_failure_reason": None,
        "check_hard_failure": False,
        "cleanup_proven": True,
        "completed_requests": 1,
        "container_id": "none" if environment == "macos" else "container-1",
        "corpus_manifest_sha256": _SHA_A,
        "corpus_version": "corpus-v1",
        "cpu": "fixture-cpu",
        "environment": environment,
        "git_sha": "1" * 40,
        "image_id": "none" if environment == "macos" else "image-1",
        "license_manifest_sha256": _SHA_B,
        "memory_limit_bytes": 10_000,
        "os_version": "fixture-os",
        "peak_aggregate_rss_bytes": 700 + worker_count,
        "peak_process_group_rss_bytes": 600,
        "problem_fingerprint": _SHA_C,
        "profile": "full",
        "protocol_version": BENCHMARK_PROTOCOL_V1,
        "python_version": "3.12.11",
        "request_fingerprint": _SHA_A,
        "request_wall_ns": wall_ns,
        "sample_index": sample_index,
        "sample_kind": sample_kind,
        "schema_version": _RECORD_SCHEMA_V1,
        "semantic_fingerprint": _SHA_A,
        "solver_name": solver,
        "solver_run": run,
        "solver_version": "1.0",
        "truth_method": "exact_oracle_v1",
        "worker_count": worker_count,
        "worker_id": f"worker-{worker_count}",
        "worker_rebuild_count": int(sample_kind == "rebuild"),
        "worker_start_count": 1,
    }
    record["semantic_fingerprint"] = _record_semantic_fingerprint(record)
    return record


def _benchmark_manifest(*, profile: str = "full", solvers: tuple[str, ...] = ("highs", "scip", "cp_sat")) -> dict[str, object]:
    environments = ("macos", "linux") if profile == "full" else ("macos",)
    probes = ["case-a"] if profile == "full" else []
    return {
        "approved_case_count": 1,
        "cold_probe_case_ids": probes,
        "cases": {
            "case-a": {
                "model_dimensions": {
                    "action_count": 2,
                    "contract_count": 1,
                    "cost_slice_count": 2,
                    "joint_state_count": 2,
                    "quantity_domain_size": 4,
                    "relationship_count": 0,
                    "terminal_atom_count": 2,
                },
                "oracle_limits": {
                    "max_joint_states": 100,
                    "max_quantity_vectors": 200,
                    "max_support_rechecks": 300,
                },
                "problem_fingerprint": _SHA_C,
                "request_fingerprint": _SHA_A,
                "request_id": "request-1",
            }
        },
        "corpus_manifest_sha256": _SHA_A,
        "environments": {
            environment: {
                "available": True,
                "build_id": f"{environment}-build-artifact",
                "environment_id": f"{environment}-build",
                "git_sha": "1" * 40,
                "image_id": "none" if environment == "macos" else "image-1",
            }
            for environment in environments
        },
        "first_qualified_case_ids": ["case-a"],
        "hard_time_limit_ms": 2_000,
        "license_manifest_sha256": _SHA_B,
        "measured_samples": 30 if profile == "full" else 1,
        "memory_limit_bytes": 10_000,
        "max_constraint_generation_rounds": 8,
        "optimal_case_ids": ["case-a"],
        "profile": profile,
        "rebuild_probe_case_ids": probes,
        "required_case_ids": ["case-a"],
        "required_environments": list(environments),
        "required_solvers": list(solvers),
        "schema_version": _MANIFEST_SCHEMA_V1,
        "solvers": {
            solver: {
                "environments": {
                    environment: {
                        "build_id": f"{environment}-build-artifact",
                        "commercial_key_required": False,
                        "install_succeeded": True,
                        "installation_ns": 10,
                        "license_evidence_present": True,
                        "open_source": True,
                        "reuse_succeeded": True,
                        "run_succeeded": True,
                        "source_evidence_present": True,
                    }
                    for environment in environments
                },
                "manual_interventions": 0,
                "whole_claim_certificate_bound": False,
            }
            for solver in solvers
        },
        "throughput_probe_case_ids": probes,
        "soft_time_limit_ms": 1_000,
        "warmup_samples": 5 if profile == "full" else 0,
        "worker_counts": [1, 2, 4] if profile == "full" else [1],
    }


def _quick_records(*, solvers: tuple[str, ...] = ("highs", "scip", "cp_sat")) -> list[dict[str, object]]:
    return [
        {
            **_benchmark_record(environment="macos", solver=solver, sample_index=0, wall_ns=100 + index),
            "profile": "quick",
        }
        for index, solver in enumerate(solvers)
    ]


def _refresh_semantic_fingerprints(records: list[dict[str, object]]) -> None:
    for record in records:
        record["semantic_fingerprint"] = _record_semantic_fingerprint(record)


def _full_records(*, solvers: tuple[str, ...] = ("highs", "scip", "cp_sat")) -> list[dict[str, object]]:
    records = [
        _benchmark_record(
            environment=environment,
            solver=solver,
            sample_kind="warmup" if index < 5 else "warm",
            sample_index=index if index < 5 else index - 5,
            wall_ns=(index + 1 if index < 5 else index - 5 + 1) * (100 if environment == "macos" else 200),
        )
        for environment in ("macos", "linux")
        for solver in solvers
        for index in range(35)
    ]
    for environment in ("macos", "linux"):
        for solver in solvers:
            for worker_count in (1, 2, 4):
                records.extend(
                    _benchmark_record(environment=environment, solver=solver, sample_kind="throughput", sample_index=index, wall_ns=3, worker_count=worker_count)
                    for index in range(30)
                )
            for sample_kind, base in (("cold", 50), ("rebuild", 70)):
                records.extend(
                    _benchmark_record(environment=environment, solver=solver, sample_kind=sample_kind, sample_index=index, wall_ns=base + index)
                    for index in range(30)
                )
    return records


def test_aggregate_requires_exact_five_warmups_and_thirty_measurements_per_environment() -> None:
    summary = aggregate_benchmark_records(_full_records(), _benchmark_manifest())
    cells = summary["solvers"]["highs"]["metrics"]["warm"]

    assert [cell["environment"] for cell in cells] == ["linux", "macos"]
    assert cells[0]["request_wall_ns"] == {"p50": 3100, "p95": 5710, "worst": 6000}
    assert cells[1]["request_wall_ns"] == {"p50": 1550, "p95": 2855, "worst": 3000}
    assert cells[1]["first_qualified_ns"] == {"p50": 1540, "p95": 2845, "worst": 2990}
    assert cells[1]["optimal_ns"] == {"p50": 1545, "p95": 2850, "worst": 2995}
    assert cells[1]["phase_timings_ns"]["backend"] == {"p50": 1544, "p95": 2849, "worst": 2994}
    assert cells[1]["master_rounds"] == {"p50": 3, "p95": 3, "worst": 3}
    assert cells[1]["adversary_rounds"] == {"p50": 2, "p95": 2, "worst": 2}
    assert cells[1]["cut_count"] == {"p50": 0, "p95": 0, "worst": 0}
    assert cells[1]["peak_process_group_rss_bytes"] == {"p50": 600, "p95": 600, "worst": 600}
    assert cells[1]["peak_aggregate_rss_bytes"] == {"p50": 701, "p95": 701, "worst": 701}
    assert cells[1]["objective_gap_quality"] == {"closed_samples": 0, "known_gap_samples": 30, "unknown_gap_samples": 0, "gap_units": {"p50": 2, "p95": 2, "worst": 2}}
    assert all(isinstance(value, int | dict) and not isinstance(value, float) for cell in cells for value in cell["request_wall_ns"].values())


def test_measurement_order_statistics_are_exact_rationals_before_serialization() -> None:
    assert benchmark._summary(range(30))["p95"] == {"numerator": 551, "denominator": 20}


def test_aggregate_keeps_exact_rational_throughput_without_pooling_environments() -> None:
    manifest = _benchmark_manifest()
    records = _full_records()

    summary = aggregate_benchmark_records(records, manifest)
    cells = summary["solvers"]["highs"]["metrics"]["throughput"]

    assert {(cell["environment"], cell["worker_count"]) for cell in cells} == {
        (environment, worker_count) for environment in ("macos", "linux") for worker_count in (1, 2, 4)
    }
    assert all(cell["requests_per_second"]["p50"] == {"numerator": 1_000_000_000, "denominator": 3} for cell in cells)
    assert Fraction(cells[0]["requests_per_second"]["p50"]["numerator"], cells[0]["requests_per_second"]["p50"]["denominator"]) == Fraction(1_000_000_000, 3)


def test_throughput_worst_is_the_minimum_exact_rate_because_higher_is_better() -> None:
    assert benchmark._throughput_summary([Fraction(1, 3), Fraction(2, 3)])["worst"] == {
        "numerator": 1,
        "denominator": 3,
    }
    records = _full_records()
    cell = [
        record for record in records
        if record["environment"] == "macos" and record["solver_name"] == "highs"
        and record["sample_kind"] == "throughput" and record["worker_count"] == 1
    ]
    for index, record in enumerate(cell):
        record["request_wall_ns"] = index + 1

    metrics = aggregate_benchmark_records(records, _benchmark_manifest())["solvers"]["highs"]["metrics"]["throughput"]
    metric = next(item for item in metrics if item["environment"] == "macos" and item["worker_count"] == 1)["requests_per_second"]

    assert metric["worst"] == {"numerator": 100_000_000, "denominator": 3}


@pytest.mark.parametrize(
    ("kind", "counter"),
    (("cold", "worker_start_count"), ("rebuild", "worker_rebuild_count")),
)
def test_aggregate_rejects_sample_kind_labels_without_the_required_worker_event(kind: str, counter: str) -> None:
    records = _full_records()
    next(record for record in records if record["sample_kind"] == kind)[counter] = 0

    with pytest.raises(ValueError, match=counter):
        aggregate_benchmark_records(records, _benchmark_manifest())


@pytest.mark.parametrize("identity", ("build_id", "git_sha", "request_fingerprint", "problem_fingerprint"))
def test_aggregate_binds_every_environment_and_case_identity(identity: str) -> None:
    manifest = _benchmark_manifest(profile="quick")
    records = _quick_records()
    if identity == "build_id":
        manifest["environments"]["macos"]["build_id"] = "other-build"
    elif identity == "git_sha":
        records[0][identity] = "2" * 40
    else:
        records[0][identity] = _SHA_D

    with pytest.raises(ValueError, match=identity):
        aggregate_benchmark_records(records, manifest)


def test_aggregate_binds_each_run_to_its_case_request_id() -> None:
    records = _quick_records()
    records[0]["solver_run"]["request_id"] = "case-b"

    with pytest.raises(ValueError, match="request_id"):
        aggregate_benchmark_records(records, _benchmark_manifest(profile="quick"))


@pytest.mark.parametrize("field", ("install_succeeded", "run_succeeded", "reuse_succeeded"))
def test_hard_gate_requires_install_run_and_reuse_evidence_for_each_environment(field: str) -> None:
    manifest = _benchmark_manifest()
    manifest["solvers"]["highs"]["environments"]["linux"][field] = False

    summary = aggregate_benchmark_records(_full_records(), manifest)

    assert summary["solvers"]["highs"]["hard_gate_failures"] == ["ENVIRONMENT_EVIDENCE_FAILED"]
    assert summary["solvers"]["highs"]["operational_evidence"]["environments"]["linux"][field] is False


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    (
        ("license_evidence_present", False),
        ("source_evidence_present", False),
        ("commercial_key_required", True),
    ),
)
def test_hard_gate_requires_license_evidence_for_each_environment(field: str, unsafe_value: bool) -> None:
    manifest = _benchmark_manifest()
    manifest["solvers"]["highs"]["environments"]["linux"][field] = unsafe_value

    summary = aggregate_benchmark_records(_full_records(), manifest)

    assert summary["solvers"]["highs"]["hard_gate_failures"] == ["LICENSE_EVIDENCE_FAILED"]
    assert summary["solvers"]["highs"]["operational_evidence"]["environments"]["linux"][field] is unsafe_value


@pytest.mark.parametrize("mutation", ("missing", "duplicate"))
def test_aggregate_rejects_missing_or_duplicate_structural_samples(mutation: str) -> None:
    records = _full_records()
    if mutation == "missing":
        records.pop()
    else:
        records.append(deepcopy(records[-1]))

    with pytest.raises(ValueError, match=mutation):
        aggregate_benchmark_records(records, _benchmark_manifest())


@pytest.mark.parametrize(
    "group",
    (
        "first_qualified_case_ids",
        "optimal_case_ids",
        "throughput_probe_case_ids",
        "cold_probe_case_ids",
        "rebuild_probe_case_ids",
    ),
)
def test_aggregate_rejects_a_full_manifest_that_skips_a_mandatory_phase_or_probe_group(group: str) -> None:
    manifest = _benchmark_manifest()
    manifest[group] = []

    with pytest.raises(ValueError, match="non-empty"):
        aggregate_benchmark_records(_full_records(), manifest)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    (
        (lambda records, manifest: records[0].update({"check_hard_failure": True, "check_failure_reason": "FALSE_SAFE"}), "CHECK_HARD_FAILURE"),
        (lambda records, manifest: records[0].__setitem__("cleanup_proven", False), "CLEANUP_UNPROVEN"),
        (lambda records, manifest: records[0].__setitem__("peak_aggregate_rss_bytes", 10_001), "MEMORY_LIMIT_EXCEEDED"),
        (lambda records, manifest: (manifest.__setitem__("memory_limit_bytes", 0), [record.__setitem__("memory_limit_bytes", 0) for record in records]), "MEMORY_LIMIT_UNBOUNDED"),
        (lambda records, manifest: manifest["solvers"]["highs"]["environments"]["macos"].__setitem__("run_succeeded", False), "ENVIRONMENT_EVIDENCE_FAILED"),
        (lambda records, manifest: manifest["solvers"]["highs"]["environments"]["macos"].__setitem__("open_source", False), "LICENSE_EVIDENCE_FAILED"),
        (lambda records, manifest: manifest["solvers"]["highs"]["environments"]["macos"].__setitem__("commercial_key_required", True), "LICENSE_EVIDENCE_FAILED"),
    ),
)
def test_hard_gate_eliminates_each_unsafe_evidence_class(mutate, reason: str) -> None:
    manifest = _benchmark_manifest(profile="quick")
    records = _quick_records()
    mutate(records, manifest)
    _refresh_semantic_fingerprints(records)

    summary = aggregate_benchmark_records(records, manifest)

    assert reason in summary["solvers"]["highs"]["hard_gate_failures"]


def test_hard_gate_allows_truthful_unknown_failure_but_rejects_unsafe_nonunknown_mapping() -> None:
    manifest = _benchmark_manifest(profile="quick")
    truthful = _quick_records()
    truthful[0]["solver_run"].update(
        {
            "business_status": "UNKNOWN",
            "classification": "UNKNOWN",
            "evidence": None,
            "objective_bounds": {"closed": False, "gap_units": None, "lower_bound_units": None, "upper_bound_units": None},
            "optimality_status": "NOT_APPLICABLE",
            "solve_status": "UNKNOWN",
            "termination_reason": "HARD_TIMEOUT",
        }
    )
    unsafe = _quick_records()
    unsafe[0]["solver_run"]["termination_reason"] = "HARD_TIMEOUT"
    unsafe[0]["solver_run"]["classification"] = "MEASUREMENT_ONLY"
    _refresh_semantic_fingerprints(truthful)
    _refresh_semantic_fingerprints(unsafe)

    truthful_summary = aggregate_benchmark_records(truthful, manifest)
    unsafe_summary = aggregate_benchmark_records(unsafe, manifest)

    assert "UNSAFE_FAILURE_MAPPING" not in truthful_summary["solvers"]["highs"]["hard_gate_failures"]
    assert "UNSAFE_FAILURE_MAPPING" in unsafe_summary["solvers"]["highs"]["hard_gate_failures"]


def test_hard_gate_detects_semantic_nondeterminism_for_repeated_case() -> None:
    records = _full_records()
    records[5]["solver_run"]["evidence"]["worst_scenario"]["atoms"][0]["atom_id"] = "no"
    _refresh_semantic_fingerprints(records)

    summary = aggregate_benchmark_records(records, _benchmark_manifest())

    assert summary["solvers"]["highs"]["hard_gate_failures"] == ["SEMANTIC_NONDETERMINISM"]


def test_semantic_fingerprint_includes_certificate_result_but_excludes_certificate_timing() -> None:
    record = _benchmark_record()
    record["solver_run"]["evidence"]["certificate"] = {
        "certificate_sha256": _SHA_A,
        "certificate_size_bytes": 10,
        "completed_certificate_sha256": _SHA_B,
        "completed_certificate_size_bytes": 12,
        "checker_name": "viprcomplete",
        "checker_version": "1.0",
        "checker_exit_code": 0,
        "checker_succeeded": True,
        "generation_ns": 1,
        "completion_ns": 2,
        "check_ns": 3,
    }
    changed_result = deepcopy(record)
    changed_result["solver_run"]["evidence"]["certificate"]["checker_succeeded"] = False
    changed_timing = deepcopy(record)
    changed_timing["solver_run"]["evidence"]["certificate"]["check_ns"] = 999

    assert benchmark._semantic_fingerprint(record) != benchmark._semantic_fingerprint(changed_result)
    assert benchmark._semantic_fingerprint(record) == benchmark._semantic_fingerprint(changed_timing)


@pytest.mark.parametrize(
    ("manifest_change", "expected_status", "expected_reason"),
    (
        ({"approved_case_count": 0}, "BLOCKED_REAL_CORPUS_EMPTY", "REAL_CORPUS_EMPTY"),
        ({}, "BLOCKED_MISSING_ENVIRONMENT", "QUICK_PROFILE_NOT_SELECTION_EVIDENCE"),
    ),
)
def test_recommendation_emits_blocked_decisions_before_comparison(
    manifest_change: dict[str, object],
    expected_status: str,
    expected_reason: str,
) -> None:
    manifest = _benchmark_manifest(profile="quick")
    manifest.update(manifest_change)

    decision = aggregate_benchmark_records(_quick_records(), manifest)["decision"]

    assert decision == {"status": expected_status, "reason": expected_reason, "selected_solver": None}


def test_recommendation_uses_worst_cell_first_and_never_solver_name_for_a_tie() -> None:
    manifest = _benchmark_manifest(profile="quick", solvers=("zeta", "alpha"))
    tied = _quick_records(solvers=("zeta", "alpha"))
    tied[1]["request_wall_ns"] = tied[0]["request_wall_ns"]
    tied[1]["solver_run"]["phase_timings_ns"] = deepcopy(tied[0]["solver_run"]["phase_timings_ns"])

    tie = aggregate_benchmark_records(tied, manifest)["decision"]
    tied[0]["solver_run"]["phase_timings_ns"] = [
        [name, value + 10 if name == "first_qualified" else value]
        for name, value in tied[0]["solver_run"]["phase_timings_ns"]
    ]
    selected = aggregate_benchmark_records(tied, manifest)["decision"]

    assert tie["status"] == "BLOCKED_MISSING_ENVIRONMENT"
    assert tie["selected_solver"] is None
    assert selected["status"] == "BLOCKED_MISSING_ENVIRONMENT"
    assert selected["selected_solver"] is None


def _set_first_qualified(records: list[dict[str, object]], solver: str, environment: str, value: int) -> None:
    for record in records:
        if record["solver_name"] == solver and record["environment"] == environment:
            record["solver_run"]["phase_timings_ns"] = [
                [name, value if name == "first_qualified" else timing]
                for name, timing in record["solver_run"]["phase_timings_ns"]
            ]


def test_recommendation_selects_by_descending_worst_cell_vector_and_records_contributors() -> None:
    records = _full_records()
    _set_first_qualified(records, "highs", "macos", 100)
    _set_first_qualified(records, "highs", "linux", 100)
    _set_first_qualified(records, "scip", "macos", 90)
    _set_first_qualified(records, "scip", "linux", 110)
    next(record for record in records if record["solver_name"] == "cp_sat").update(
        {"check_hard_failure": True, "check_failure_reason": "FALSE_SAFE"}
    )
    _refresh_semantic_fingerprints(records)

    decision = aggregate_benchmark_records(records, _benchmark_manifest())["decision"]

    assert decision["status"] == "SELECTED"
    assert decision["selected_solver"] == "highs"
    assert decision["decisive_stage"] == "FIRST_QUALIFIED_P95"
    assert decision["contributing_cells"] == ["linux/case-a", "macos/case-a"]


def test_recommendation_memory_stage_includes_every_throughput_worker_cell() -> None:
    records = _full_records()
    for record in records:
        if record["sample_kind"] == "throughput" and record["solver_name"] in {"highs", "scip"}:
            record["peak_aggregate_rss_bytes"] = 800 if record["solver_name"] == "highs" else 900
    next(record for record in records if record["solver_name"] == "cp_sat").update(
        {"check_hard_failure": True, "check_failure_reason": "FALSE_SAFE"}
    )
    _refresh_semantic_fingerprints(records)

    decision = aggregate_benchmark_records(records, _benchmark_manifest())["decision"]

    assert decision["selected_solver"] == "highs"
    assert decision["decisive_stage"] == "PEAK_RSS_P95"
    assert any("workers-4" in label for label in decision["contributing_cells"])


def test_recommendation_selects_the_only_hard_gate_survivor_by_elimination() -> None:
    records = _full_records()
    for solver in ("scip", "cp_sat"):
        next(record for record in records if record["solver_name"] == solver).update(
            {"check_hard_failure": True, "check_failure_reason": "FALSE_SAFE"}
        )
    _refresh_semantic_fingerprints(records)

    decision = aggregate_benchmark_records(records, _benchmark_manifest())["decision"]

    assert decision == {
        "status": "SELECTED",
        "reason": "ONLY_SURVIVOR_AFTER_HARD_GATES",
        "selected_solver": "highs",
        "decisive_stage": "HARD_GATE_ELIMINATION",
        "contributing_cells": [],
    }


def test_recommendation_emits_no_survivor_and_no_decisive_winner_without_name_tiebreak() -> None:
    tied_records = _full_records()
    for environment in ("macos", "linux"):
        for solver in ("highs", "scip", "cp_sat"):
            _set_first_qualified(tied_records, solver, environment, 100)
    tie = aggregate_benchmark_records(tied_records, _benchmark_manifest())["decision"]
    eliminated = deepcopy(tied_records)
    for solver in ("highs", "scip", "cp_sat"):
        next(record for record in eliminated if record["solver_name"] == solver).update(
            {"check_hard_failure": True, "check_failure_reason": "FALSE_SAFE"}
        )
    _refresh_semantic_fingerprints(eliminated)
    no_survivor = aggregate_benchmark_records(eliminated, _benchmark_manifest())["decision"]

    assert tie == {"status": "NO_DECISIVE_WINNER", "reason": "COMPLETE_TIE", "selected_solver": None}
    assert no_survivor == {"status": "NO_SURVIVOR", "reason": "ALL_CANDIDATES_HARD_ELIMINATED", "selected_solver": None}


def test_recommendation_blocks_when_a_full_manifest_environment_is_unavailable() -> None:
    manifest = _benchmark_manifest()
    manifest["environments"]["linux"]["available"] = False
    records = [record for record in _full_records() if record["environment"] == "macos"]

    decision = aggregate_benchmark_records(records, manifest)["decision"]

    assert decision == {"status": "BLOCKED_MISSING_ENVIRONMENT", "reason": "MANDATORY_ENVIRONMENT_UNAVAILABLE", "selected_solver": None}


@pytest.mark.parametrize(
    "mutation",
    (
        lambda record: record.__setitem__("semantic_fingerprint", _SHA_C),
        lambda record: record["solver_run"].update({"classification": "CHECKED", "canonical_result": None}),
        lambda record: record["solver_run"].update({"classification": "UNKNOWN", "solve_status": "FEASIBLE"}),
        lambda record: record["solver_run"]["evidence"]["candidate"].__setitem__("extra", 1),
        lambda record: record["solver_run"]["evidence"].__setitem__("cuts", [{}]),
        lambda record: record["solver_run"]["evidence"].__setitem__("certificate", {"checker_succeeded": True}),
    ),
)
def test_aggregate_rejects_unbound_or_malformed_canonical_solver_run_payloads(mutation) -> None:
    record = _quick_records()[0]
    record["semantic_fingerprint"] = _record_semantic_fingerprint(record)
    mutation(record)

    with pytest.raises(ValueError):
        aggregate_benchmark_records([record, *_quick_records()[1:]], _benchmark_manifest(profile="quick"))


def test_aggregate_refuses_to_promote_current_unbound_certificate_evidence() -> None:
    manifest = _benchmark_manifest(profile="quick")
    manifest["solvers"]["highs"]["whole_claim_certificate_bound"] = True

    with pytest.raises(ValueError, match="unbound"):
        aggregate_benchmark_records(_quick_records(), manifest)


@pytest.mark.parametrize(
    "violation",
    (
        "checked_axes",
        "measurement_proof",
        "unknown_business",
        "missing_phase",
        "rss_order",
        "certificate_pair",
        "certificate_success",
    ),
)
def test_aggregate_validates_all_solver_run_invariants_used_by_the_report(violation: str) -> None:
    records = _quick_records()
    record = records[0]
    run = record["solver_run"]
    if violation == "checked_axes":
        result = canonical_payload(load_canonical_cases()[0].expected_result)
        run["canonical_result"] = result
        for field in ("solve_status", "proof_status", "business_status", "optimality_status", "objective_bounds"):
            run[field] = result[field]
        run["classification"] = "CHECKED"
        run["business_status"] = "NO_ARBITRAGE"
    elif violation == "measurement_proof":
        run["proof_status"] = "PROVEN"
    elif violation == "unknown_business":
        run.update({"classification": "UNKNOWN", "solve_status": "UNKNOWN"})
    elif violation == "missing_phase":
        run["phase_timings_ns"] = run["phase_timings_ns"][:-1]
    elif violation == "rss_order":
        run["peak_rss_bytes"] = record["peak_process_group_rss_bytes"] + 1
    else:
        run["evidence"]["certificate"] = {
            "certificate_sha256": _SHA_A,
            "certificate_size_bytes": 10,
            "completed_certificate_sha256": _SHA_B,
            "completed_certificate_size_bytes": 10 if violation == "certificate_success" else None,
            "checker_name": "" if violation == "certificate_success" else "checker",
            "checker_version": "1",
            "checker_exit_code": 1 if violation == "certificate_success" else 0,
            "checker_succeeded": violation == "certificate_success",
            "generation_ns": 1,
            "completion_ns": 1,
            "check_ns": 1,
        }
    _refresh_semantic_fingerprints(records)

    with pytest.raises(ValueError):
        aggregate_benchmark_records(records, _benchmark_manifest(profile="quick"))


@pytest.mark.parametrize("violation", ("closed_bounds", "evidence_bounds", "candidate_profit", "closure", "solve_status", "optimality_status"))
def test_aggregate_rejects_contradictory_or_unbound_solver_claim_fields(violation: str) -> None:
    records = _quick_records()
    run = records[0]["solver_run"]
    if violation == "closed_bounds":
        run["objective_bounds"] = {"closed": True, "gap_units": 1, "lower_bound_units": 7, "upper_bound_units": 8}
        run["evidence"]["objective_bounds"] = deepcopy(run["objective_bounds"])
        run["evidence"]["global_search_closed"] = True
    elif violation == "evidence_bounds":
        run["evidence"]["objective_bounds"]["gap_units"] = 3
    elif violation == "candidate_profit":
        run["evidence"]["candidate"]["claimed_guaranteed_profit_units"] = 8
    elif violation == "closure":
        run["evidence"]["global_search_closed"] = True
    elif violation == "solve_status":
        run["solve_status"] = "INFEASIBLE"
    else:
        run["optimality_status"] = "OPTIMAL"
    _refresh_semantic_fingerprints(records)

    with pytest.raises(ValueError):
        aggregate_benchmark_records(records, _benchmark_manifest(profile="quick"))


def test_objective_gap_quality_ranks_labelled_cells_worst_first_independent_of_input_order() -> None:
    cells = [
        {
            "case_id": "case-a",
            "environment": "macos",
            "objective_gap_quality": {
                "closed_samples": 1,
                "known_gap_samples": 30,
                "unknown_gap_samples": 0,
                "gap_units": {"p50": 0, "p95": 0, "worst": 0},
            },
        },
        {
            "case_id": "case-a",
            "environment": "linux",
            "objective_gap_quality": {
                "closed_samples": 0,
                "known_gap_samples": 0,
                "unknown_gap_samples": 30,
                "gap_units": None,
            },
        },
    ]

    expected = ((Fraction(2), Fraction(0)), (Fraction(1), Fraction(0)))
    assert benchmark._gap_vector(cells, ("case-a",)) == expected
    assert benchmark._gap_vector(list(reversed(cells)), ("case-a",)) == expected


def test_generate_report_is_order_independent_and_verify_detects_changed_or_extra_bytes(tmp_path: Path) -> None:
    manifest = _benchmark_manifest(profile="quick")
    records = _quick_records()
    manifest_path = tmp_path / "manifest.json"
    left_jsonl = tmp_path / "left.jsonl"
    left_tail_jsonl = tmp_path / "left-tail.jsonl"
    right_jsonl = tmp_path / "right.jsonl"
    right_tail_jsonl = tmp_path / "right-tail.jsonl"
    left_output = tmp_path / "left"
    right_output = tmp_path / "right"
    manifest_path.write_text(json.dumps(manifest))
    left_jsonl.write_text(json.dumps(records[0]) + "\n")
    left_tail_jsonl.write_text("".join(json.dumps(record) + "\n" for record in records[1:]))
    right_jsonl.write_text("".join(json.dumps(record) + "\n" for record in reversed(records[1:])))
    right_tail_jsonl.write_text(json.dumps(records[0]) + "\n")

    generate_benchmark_report([left_jsonl, left_tail_jsonl], manifest_path, left_output)
    generate_benchmark_report([right_jsonl, right_tail_jsonl], manifest_path, right_output)

    assert {path.name: path.read_bytes() for path in left_output.iterdir()} == {
        path.name: path.read_bytes() for path in right_output.iterdir()
    }
    assert sorted(path.name for path in left_output.iterdir()) == ["production_envelope.json", "report.md", "summary.json"]
    assert all(path.read_bytes().endswith(b"\n") and not path.read_bytes().endswith(b"\n\n") for path in left_output.iterdir())
    envelope = json.loads((left_output / "production_envelope.json").read_bytes())
    assert envelope["proof_policy"] == {
        "benchmark_certificate_evidence": "UNBOUND_BENCHMARK_ONLY",
        "canonical_proof": "EXACT_ORACLE_CHECKING",
    }
    assert envelope["selection"] == {
        "reason": "QUICK_PROFILE_NOT_SELECTION_EVIDENCE",
        "solver": None,
        "status": "BLOCKED_MISSING_ENVIRONMENT",
    }
    assert envelope["hard_limits"] == {
        "hard_time_limit_ms": 2_000,
        "memory_limit_bytes": 10_000,
        "max_constraint_generation_rounds": 8,
        "oracle_limits": {
            "case-a": {
                "max_joint_states": 100,
                "max_quantity_vectors": 200,
                "max_support_rechecks": 300,
            }
        },
        "unknown_mappings": {
            "hard_time_limit_ms": "HARD_TIMEOUT",
            "max_constraint_generation_rounds": "PROOF_UNCLOSED",
            "memory_limit_bytes": "MEMORY_LIMIT",
            "oracle_limits": {
                "max_joint_states": "ORACLE_STATE_LIMIT_EXCEEDED",
                "max_quantity_vectors": "ORACLE_DECISION_LIMIT_EXCEEDED",
                "max_support_rechecks": "ORACLE_SUPPORT_LIMIT_EXCEEDED",
            },
            "soft_time_limit_ms": "SOFT_TIMEOUT",
        },
        "soft_time_limit_ms": 1_000,
    }
    assert envelope["measured_dimensions"] == {
        "case_ids": ["case-a"],
        "cold_probe_case_ids": [],
        "first_qualified_case_ids": ["case-a"],
        "measured_samples": 1,
        "model_dimensions": {
            "case-a": {
                "action_count": 2,
                "contract_count": 1,
                "cost_slice_count": 2,
                "joint_state_count": 2,
                "quantity_domain_size": 4,
                "relationship_count": 0,
                "terminal_atom_count": 2,
            }
        },
        "optimal_case_ids": ["case-a"],
        "profiles": ["quick"],
        "rebuild_probe_case_ids": [],
        "sample_kinds": ["warm"],
        "solvers": ["cp_sat", "highs", "scip"],
        "throughput_probe_case_ids": [],
        "warmup_samples": 0,
        "worker_counts": [1],
    }
    verify_benchmark_report([left_jsonl, left_tail_jsonl], manifest_path, left_output)
    (left_output / "summary.json").write_bytes((left_output / "summary.json").read_bytes() + b" ")
    with pytest.raises(ValueError, match="summary.json"):
        verify_benchmark_report([left_jsonl, left_tail_jsonl], manifest_path, left_output)
    generate_benchmark_report([left_jsonl, left_tail_jsonl], manifest_path, left_output)
    (left_output / "extra.txt").write_text("extra")
    with pytest.raises(ValueError, match="extra"):
        verify_benchmark_report([left_jsonl, left_tail_jsonl], manifest_path, left_output)
    (left_output / "extra.txt").unlink()
    (left_output / "report.md").unlink()
    with pytest.raises(ValueError, match="missing"):
        verify_benchmark_report([left_jsonl, left_tail_jsonl], manifest_path, left_output)


@pytest.mark.parametrize("bad_line", ('{"schema_version":1,"schema_version":2}\n', '{"value":NaN}\n'))
def test_generate_report_rejects_duplicate_json_keys_and_nonstandard_constants(tmp_path: Path, bad_line: str) -> None:
    manifest_path = tmp_path / "manifest.json"
    records_path = tmp_path / "records.jsonl"
    manifest_path.write_text(json.dumps(_benchmark_manifest(profile="quick")))
    records_path.write_text(bad_line)

    with pytest.raises(ValueError):
        generate_benchmark_report([records_path], manifest_path, tmp_path / "output")


def test_generate_report_rejects_a_jsonl_line_above_the_worker_limit(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    records_path = tmp_path / "records.jsonl"
    manifest_path.write_text(json.dumps(_benchmark_manifest(profile="quick")))
    records_path.write_bytes(b" " * (1024 * 1024 + 1))

    with pytest.raises(ValueError, match="line limit"):
        generate_benchmark_report([records_path], manifest_path, tmp_path / "output")
