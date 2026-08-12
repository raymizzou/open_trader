from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
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
    BenchmarkClassification,
    CertificateEvidence,
    SolverEvidence,
    solve_with_constraint_generation,
)
from open_trader.prediction_solver_benchmark import (
    CheckFailureReason,
    check_solver_claim,
    generate_synthetic_corpus,
    import_approved_snapshot,
    load_canonical_cases,
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
