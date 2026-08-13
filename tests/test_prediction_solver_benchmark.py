from __future__ import annotations

import hashlib
import json
import subprocess
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
    SolverRun,
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
from open_trader.prediction_solver_worker import WorkerOutcome, WorkerResponse
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


def _full_environment_fixture() -> tuple[dict[str, dict[str, object]], dict[str, dict[str, dict[str, object]]]]:
    environments = {
        environment: {
            "available": True,
            "architecture": "arm64" if environment == "macos" else "x86_64",
            "cpu": "fixture-cpu",
            "environment_id": f"{environment}-build",
            "git_sha": "1" * 40,
            "os_version": "fixture-os",
        }
        for environment in ("macos", "linux")
    }
    artifacts = {
        solver: {
            environment: {
                "adapter_version": "adapter-v1",
                "build_id": f"{solver}-{environment}-build-artifact",
                "commercial_key_required": False,
                "image_id": "none" if environment == "macos" else f"{solver}-image-1",
                "install_succeeded": True,
                "installation_ns": 10,
                "license_evidence_present": True,
                "open_source": True,
                "python_version": "3.12.11",
                "reuse_succeeded": True,
                "run_succeeded": True,
                "solver_version": "1.0",
                "source_evidence_present": True,
            }
            for environment in ("macos", "linux")
        }
        for solver in ("highs", "scip", "cp_sat")
    }
    assert all(set(evidence) == benchmark._ENVIRONMENT_KEYS for evidence in environments.values())
    assert all(
        set(evidence) == benchmark._SOLVER_ENVIRONMENT_KEYS
        for solver_artifacts in artifacts.values()
        for evidence in solver_artifacts.values()
    )
    return environments, artifacts


def test_full_cases_bind_all_three_frozen_layers_and_approved_oracle_truth() -> None:
    cases = benchmark._load_full_cases()

    assert len(cases) == 41
    assert [case.case_id for case in cases[:16]] == [case.case_id for case in load_canonical_cases()]
    assert [case.case_id for case in cases[16:40]] == [*SEMANTIC_CASE_IDS, *SCALE_CASE_IDS]
    approved = cases[-1]
    assert approved.case_id == "approved:a1b63df2776a88522c3a00ed"
    assert approved.request.mode == SearchMode.ADMISSION
    assert canonical_payload(approved.budget) == {
        "max_joint_states": 9,
        "max_quantity_vectors": 289,
        "max_support_rechecks": 64,
    }
    assert approved.expected_result is not None
    assert approved.expected_result.proof_status == ProofStatus.PROVEN
    assert approved.expected_result.business_status == BusinessStatus.NO_QUALIFIED_OPPORTUNITY
    assert fingerprint(approved.request.problem) == "sha256:a1b63df2776a88522c3a00ed535489b777b18be827445b0419bf7d8359f127c4"


def test_full_manifest_and_sample_plan_freeze_the_exact_task10_matrix() -> None:
    cases = benchmark._load_full_cases()
    environments, artifacts = _full_environment_fixture()
    manifest = benchmark._full_manifest(cases, environments, artifacts)
    plan = benchmark._full_sample_plan(cases)

    assert manifest["profile"] == "full"
    assert manifest["required_environments"] == ["macos", "linux"]
    assert manifest["required_solvers"] == ["highs", "scip", "cp_sat"]
    assert manifest["worker_counts"] == [1, 2, 4]
    assert manifest["warmup_samples"] == 5
    assert manifest["measured_samples"] == 30
    assert manifest["throughput_probe_case_ids"] == ["single_contract_complement"]
    assert manifest["cold_probe_case_ids"] == ["single_contract_complement"]
    assert manifest["rebuild_probe_case_ids"] == ["single_contract_complement"]
    assert len(plan) == 1_585
    assert plan[:2] == (
        (cases[0].case_id, "warmup", 0, 1),
        (cases[0].case_id, "warmup", 1, 1),
    )
    assert plan[-1] == ("single_contract_complement", "rebuild", 29, 1)


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


def test_quick_cases_are_exactly_canonical_plus_semantic_with_frozen_oracle_budgets() -> None:
    cases = benchmark._load_quick_cases()
    canonical = load_canonical_cases()
    synthetic = {
        item["case_id"]: request_from_payload(item["request"])
        for item in json.loads(SYNTHETIC_CORPUS.read_bytes())["cases"]
        if item["case_id"] in SEMANTIC_CASE_IDS
    }

    assert len(cases) == 34
    assert tuple(case.case_id for case in cases[:16]) == tuple(case.case_id for case in canonical)
    assert tuple(case.case_id for case in cases[16:]) == SEMANTIC_CASE_IDS
    assert all(case.truth_method == "exact_oracle_v1" for case in cases)
    assert all(case.budget == case.request.budget for case in cases)
    assert {case.case_id: case.budget for case in cases[16:]} == {
        case_id: request.budget for case_id, request in synthetic.items()
    }


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
    corpus.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.prediction_solver.approved_corpus.v1",
                "anonymization_salt": "test-approved-v1",
                "cases": [],
                "input_gaps": [
                    {
                        "gap_id": "legacy-incomplete-terminal-model",
                        "reason": "approved legacy relations/signals/previews do not contain a complete #48 terminal model",
                        "source_alias": "legacy-approved-prediction-data",
                    }
                ],
            }
        )
    )
    return inbox, corpus


def test_approved_corpus_contains_one_legal_case_with_the_known_legacy_gap() -> None:
    payload = benchmark._load_approved_corpus(APPROVED_CORPUS)

    assert len(payload["cases"]) == 1
    case = payload["cases"][0]
    problem = problem_from_payload(case["problem"])
    assert validate_problem(problem) == ()
    assert case["anonymized_problem_fingerprint"] == fingerprint(problem)
    assert case["case_id"] == f"approved:{case['anonymized_problem_fingerprint'].removeprefix('sha256:')[:24]}"
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


def test_checker_keeps_a_safe_noncanonical_admission_as_measurement_only() -> None:
    case = next(item for item in load_canonical_cases() if item.case_id == "native-complement-n2")
    assert case.request.mode == SearchMode.ADMISSION
    assert case.expected_result is not None and case.expected_result.solution is not None
    evidence = _evidence_for_quantities(
        case.request,
        (ActionQuantity("buy-no-a", 2), ActionQuantity("buy-yes-a", 2)),
    )
    assert evidence.candidate is not None
    assert evidence.candidate.quantities != case.expected_result.solution.quantities

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
        "build_id": f"{solver}-{environment}-build-artifact",
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
        "image_id": "none" if environment == "macos" else f"{solver}-image-1",
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
                "architecture": "arm64" if environment == "macos" else "x86_64",
                "cpu": "fixture-cpu",
                "environment_id": f"{environment}-build",
                "git_sha": "1" * 40,
                "os_version": "fixture-os",
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
                        "adapter_version": "adapter-v1",
                        "build_id": f"{solver}-{environment}-build-artifact",
                        "commercial_key_required": False,
                        "image_id": "none" if environment == "macos" else f"{solver}-image-1",
                        "install_succeeded": True,
                        "installation_ns": 10,
                        "license_evidence_present": True,
                        "open_source": True,
                        "python_version": "3.12.11",
                        "reuse_succeeded": True,
                        "run_succeeded": True,
                        "solver_version": "1.0",
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
        records[0][identity] = "other-build"
    elif identity == "git_sha":
        records[0][identity] = "2" * 40
    else:
        records[0][identity] = _SHA_D

    with pytest.raises(ValueError, match=identity):
        aggregate_benchmark_records(records, manifest)


def test_manifest_keeps_distinct_solver_artifact_identities_inside_one_environment() -> None:
    manifest = _benchmark_manifest(profile="quick")
    records = _quick_records()

    summary = aggregate_benchmark_records(records, manifest)

    environments = {
        solver: evidence["operational_evidence"]["environments"]["macos"]
        for solver, evidence in summary["solvers"].items()
    }
    assert len({evidence["build_id"] for evidence in environments.values()}) == 3
    assert all(evidence["python_version"] == "3.12.11" for evidence in environments.values())


@pytest.mark.parametrize(
    "field",
    ("build_id", "image_id", "python_version", "solver_version", "adapter_version", "cpu", "architecture", "os_version"),
)
def test_record_metadata_binds_to_its_solver_and_environment_evidence(field: str) -> None:
    manifest = _benchmark_manifest(profile="quick")
    records = _quick_records()
    records[0][field] = "mismatch"
    if field in {"solver_version", "adapter_version"}:
        records[0]["solver_run"][field] = "mismatch"

    with pytest.raises(ValueError, match=field):
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


def test_hard_gate_requires_a_completed_run_per_available_solver_environment() -> None:
    records = _quick_records()
    for record in records:
        if record["solver_name"] == "highs":
            _set_unknown_timeout(record)
            record["solver_run"]["termination_reason"] = "CRASH"
    _refresh_semantic_fingerprints(records)

    summary = aggregate_benchmark_records(records, _benchmark_manifest(profile="quick"))

    assert summary["solvers"]["highs"]["hard_gate_failures"] == ["NO_COMPLETED_RUN"]
    assert summary["solvers"]["scip"]["hard_gate_failures"] == []


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


def _set_unknown_timeout(record: dict[str, object]) -> None:
    run = record["solver_run"]
    run.update(
        {
            "business_status": "UNKNOWN",
            "classification": "UNKNOWN",
            "evidence": None,
            "objective_bounds": {
                "closed": False,
                "gap_units": None,
                "lower_bound_units": None,
                "upper_bound_units": None,
            },
            "optimality_status": "NOT_APPLICABLE",
            "solve_status": "UNKNOWN",
            "termination_reason": "HARD_TIMEOUT",
        }
    )
    run["phase_timings_ns"] = [[name, 0] for name, _ in run["phase_timings_ns"]]


@pytest.mark.parametrize(
    "unknown_environments",
    (("macos",), ("macos", "linux")),
    ids=("mixed-achieved-and-unachieved", "all-unachieved"),
)
def test_recommendation_ranks_phase_nonachievement_before_zero_timing(
    unknown_environments: tuple[str, ...],
) -> None:
    records = _full_records()
    for record in records:
        if record["solver_name"] == "highs" and record["environment"] in unknown_environments:
            _set_unknown_timeout(record)
    for environment in unknown_environments:
        next(
            record for record in records
            if record["solver_name"] == "highs" and record["environment"] == environment
        )["solver_run"]["termination_reason"] = "COMPLETED"
    _refresh_semantic_fingerprints(records)

    summary = aggregate_benchmark_records(records, _benchmark_manifest())

    assert summary["decision"]["selected_solver"] != "highs"
    assert summary["solvers"]["highs"]["hard_gate_failures"] == []
    for cell in summary["solvers"]["highs"]["metrics"]["warm"]:
        unachieved = 30 if cell["environment"] in unknown_environments else 0
        quality = cell["first_qualified_quality"]
        assert quality["achieved_samples"] == 30 - unachieved
        assert quality["unachieved_samples"] == unachieved
        if unachieved == 30:
            assert quality["timing_ns"] is None


def test_recommendation_ignores_phase_timing_when_every_solver_is_unachieved() -> None:
    records = _full_records()
    raw_phase_values = {"highs": 0, "scip": 10, "cp_sat": 20}
    for record in records:
        _set_unknown_timeout(record)
        record["solver_run"]["phase_timings_ns"] = [
            [name, raw_phase_values[record["solver_name"]] if name in {"first_qualified", "optimal"} else value]
            for name, value in record["solver_run"]["phase_timings_ns"]
        ]
    for solver in ("highs", "scip", "cp_sat"):
        for environment in ("macos", "linux"):
            next(
                record for record in records
                if record["solver_name"] == solver and record["environment"] == environment
            )["solver_run"]["termination_reason"] = "COMPLETED"
    _refresh_semantic_fingerprints(records)

    summary = aggregate_benchmark_records(records, _benchmark_manifest())

    assert summary["decision"] == {
        "status": "NO_DECISIVE_WINNER",
        "reason": "COMPLETE_TIE",
        "selected_solver": None,
    }
    assert all(
        cell["first_qualified_quality"]["timing_ns"] is None
        for solver in summary["solvers"].values()
        for cell in solver["metrics"]["warm"]
    )


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


def test_aggregate_rejects_coherent_solver_claim_values_outside_signed_int64() -> None:
    records = _quick_records()
    run = records[0]["solver_run"]
    evidence = run["evidence"]
    huge = 2**100
    bounds = {
        "closed": False,
        "gap_units": 2 * huge,
        "lower_bound_units": huge,
        "upper_bound_units": 3 * huge,
    }
    run["objective_bounds"] = deepcopy(bounds)
    evidence.update(
        {
            "candidate": {
                "claimed_guaranteed_profit_units": huge,
                "quantities": [{"action_id": "a", "quantity_lots": huge}],
            },
            "cost_upper_bound_units": huge,
            "cuts": [
                {
                    "cut_id": "cut-1",
                    "payout_per_lot": [
                        {"action_id": "a", "payout_lower_bound_per_lot_units": huge}
                    ],
                    "scenario": {
                        "atoms": [{"atom_id": "yes", "market_contract_id": "a"}]
                    },
                }
            ],
            "guaranteed_profit_units": huge,
            "objective_bounds": deepcopy(bounds),
            "payout_lower_bound_units": 2 * huge,
        }
    )
    _refresh_semantic_fingerprints(records)

    with pytest.raises(ValueError, match="signed 64-bit"):
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


def test_verify_report_allows_the_task10_inputs_beside_root_level_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "issue49"
    output.mkdir()
    manifest = output / "environment_manifest.json"
    macos = output / "macos.jsonl"
    linux = output / "linux.jsonl"
    manifest.write_text(json.dumps(_benchmark_manifest(profile="quick")))
    macos.write_text("".join(json.dumps(record) + "\n" for record in _quick_records()))
    linux.write_text("")

    generate_benchmark_report([macos, linux], manifest, output)

    verify_benchmark_report([macos, linux], manifest, output)


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


def _test_quick_environment(tmp_path: Path) -> Path:
    env_root = tmp_path / "envs"
    for solver in ("highs", "scip", "cp_sat"):
        environment = env_root / solver
        (environment / "bin").mkdir(parents=True)
        (environment / "bin" / "python").write_text("#!/bin/sh\n")
        (environment / "bin" / "python").chmod(0o755)
        (environment / ".build-key").write_text("a" * 64 + "\n")
    return env_root


def test_quick_environment_gate_starts_no_worker_when_any_reusable_venv_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_root = _test_quick_environment(tmp_path)
    (env_root / "scip" / ".build-key").unlink()
    monkeypatch.setattr(benchmark, "_expected_build_key", lambda solver: "a" * 64)
    monkeypatch.setattr(benchmark, "WorkerHarness", lambda *args, **kwargs: pytest.fail("worker started"))

    result = benchmark._run_quick_benchmark(tmp_path / "quick", env_root)

    assert result == 2
    assert capsys.readouterr().out.strip() == "BLOCKED_MISSING_ENVIRONMENT"


def test_quick_environment_gate_blocks_a_non_utf8_build_key_before_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_root = _test_quick_environment(tmp_path)
    (env_root / "scip" / ".build-key").write_bytes(b"\xff")
    monkeypatch.setattr(benchmark, "_SOLVERS", ("scip",))
    monkeypatch.setattr(benchmark, "_expected_build_key", lambda solver: "a" * 64)

    assert benchmark._discover_quick_environment(env_root) is None


@pytest.mark.parametrize("invalid_python", ("empty", "non-executable"))
def test_quick_environment_gate_rejects_an_unusable_venv_python_before_workers(
    invalid_python: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_root = _test_quick_environment(tmp_path)
    python = env_root / "highs" / "bin" / "python"
    if invalid_python == "empty":
        python.write_bytes(b"")
        python.chmod(0o755)
    else:
        python.chmod(0o644)
    monkeypatch.setattr(benchmark, "_expected_build_key", lambda solver: "a" * 64)
    monkeypatch.setattr(benchmark, "WorkerHarness", lambda *args, **kwargs: pytest.fail("worker started"))

    result = benchmark._run_quick_benchmark(tmp_path / "quick", env_root)

    assert result == 2
    assert capsys.readouterr().out.strip() == "BLOCKED_MISSING_ENVIRONMENT"


def test_quick_environment_discovery_runs_strict_serial_native_self_checks_with_sanitized_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_root = _test_quick_environment(tmp_path)
    monkeypatch.setattr(benchmark, "_expected_build_key", lambda solver: "a" * 64)
    monkeypatch.setattr(benchmark.platform, "processor", lambda: "fixture-cpu")
    monkeypatch.setattr(benchmark.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(benchmark.platform, "platform", lambda: "fixture-macos")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-smoke")
    calls: list[tuple[str, dict[str, str]]] = []
    git_environments: list[dict[str, str]] = []
    payloads = {
        "highs": {"adapter": "HighsBackend", "solver": "highspy", "version": "1.15.1", "status": "OPTIMAL"},
        "scip": {"adapter": "ScipBackend", "solver": "pyscipopt", "version": "10.0.2", "status": "OPTIMAL"},
        "cp_sat": {"adapter": "CpSatBackend", "solver": "ortools", "version": "9.15.6755", "status": "OPTIMAL"},
    }

    def run(command, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            git_environments.append(kwargs["env"])
            return subprocess.CompletedProcess(command, 0, "1" * 40 + "\n", "")
        solver = command[-1]
        calls.append((solver, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, json.dumps(payloads[solver]) + "\n", "")

    monkeypatch.setattr(benchmark.subprocess, "run", run)

    discovered = benchmark._discover_quick_environment(env_root)

    assert discovered is not None
    assert [solver for solver, _ in calls] == ["highs", "scip", "cp_sat"]
    assert all(
        environment == {
            "PATH": f"{env_root / solver / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONSAFEPATH": "1",
        }
        for solver, environment in calls
    )
    assert len(git_environments) == 1
    assert set(git_environments[0]) == {"PATH", "PYTHONNOUSERSITE", "PYTHONPATH", "PYTHONSAFEPATH"}
    assert "OPENAI_API_KEY" not in git_environments[0]
    assert all(discovered[1][solver]["run_succeeded"] is True for solver in payloads)


@pytest.mark.parametrize("failure", ("timeout", "nonzero", "empty", "malformed", "mismatch"))
def test_quick_environment_discovery_blocks_on_any_failed_native_self_check(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_root = _test_quick_environment(tmp_path)
    monkeypatch.setattr(benchmark, "_expected_build_key", lambda solver: "a" * 64)

    def run(command, **kwargs):
        del kwargs
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 5)
        if failure == "empty":
            return subprocess.CompletedProcess(command, 0, "", "")
        if failure == "malformed":
            return subprocess.CompletedProcess(command, 0, "not-json\n", "")
        payload = {"adapter": "HighsBackend", "solver": "highspy", "version": "1.15.1", "status": "OPTIMAL"}
        if failure == "mismatch":
            payload["status"] = "FEASIBLE"
        return subprocess.CompletedProcess(command, 1 if failure == "nonzero" else 0, json.dumps(payload) + "\n", "")

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    monkeypatch.setattr(benchmark, "WorkerHarness", lambda *args, **kwargs: pytest.fail("worker started"))

    assert benchmark._discover_quick_environment(env_root) is None
    assert benchmark._run_quick_benchmark(tmp_path / "quick", env_root) == 2
    assert capsys.readouterr().out.strip() == "BLOCKED_MISSING_ENVIRONMENT"


def test_empty_approved_corpus_gate_starts_no_worker_or_environment_discovery(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(benchmark, "_load_approved_corpus", lambda path: {"cases": []})
    monkeypatch.setattr(benchmark, "_discover_quick_environment", lambda *args: pytest.fail("environment discovered"))
    monkeypatch.setattr(benchmark, "WorkerHarness", lambda *args, **kwargs: pytest.fail("worker started"))

    result = benchmark._run_full_benchmark("macos")

    assert result == 2
    assert capsys.readouterr().out.strip() == "BLOCKED_REAL_CORPUS_EMPTY"


def test_solver_run_construction_attaches_checked_truth_and_maps_failures_honestly() -> None:
    case = load_canonical_cases()[0]
    assert case.expected_result is not None
    evidence = _evidence_for_result(case.expected_result)
    phases = {name: 0 for name in benchmark.WORKER_PHASE_NAMES}
    phases["backend"] = 7
    response = WorkerResponse(
        BENCHMARK_PROTOCOL_V1,
        "highs",
        case.case_id,
        "OK",
        canonical_payload(evidence),
        phases,
        (),
    )
    checked_outcome = WorkerOutcome(case.case_id, "OK", "COMPLETED", 123, 123, 4, False, True, response)
    failed_outcome = WorkerOutcome(case.case_id, "UNKNOWN", "CLEANUP_UNPROVEN", 123, 123, 4, False, False)

    checked, check = benchmark._solver_run_from_outcome(
        case,
        "highs",
        checked_outcome,
        _benchmark_manifest(profile="quick")["environments"]["macos"],
        _benchmark_manifest(profile="quick")["solvers"]["highs"]["environments"]["macos"],
    )
    failed, failed_check = benchmark._solver_run_from_outcome(
        case,
        "highs",
        failed_outcome,
        _benchmark_manifest(profile="quick")["environments"]["macos"],
        _benchmark_manifest(profile="quick")["solvers"]["highs"]["environments"]["macos"],
    )

    assert isinstance(checked, SolverRun)
    assert checked.classification == BenchmarkClassification.CHECKED
    assert checked.canonical_result == case.expected_result
    assert dict(checked.phase_timings_ns) == {**phases, "independent_check": checked.phase_timings_ns[-1][1]}
    assert check.hard_failure is False
    assert failed.classification == BenchmarkClassification.UNKNOWN
    assert failed.solve_status == SolveStatus.UNKNOWN
    assert failed.business_status == BusinessStatus.UNKNOWN
    assert failed.termination_reason == TerminationReason.CLEANUP_UNPROVEN
    assert failed_check.failure_reason is None


def _fake_batch_peaks(worker_count: int) -> tuple[int, ...]:
    return tuple((slot + 1) * 1024 for slot in range(worker_count))


def _fake_harness_factory(
    *,
    cleanup_proven: bool = True,
    batch_semantics_differ: bool = False,
    hard_check_failure: bool = False,
    raise_on_sample_kind: str | None = None,
    termination: str | None = None,
):
    next_pid = 10_000
    cases = benchmark._load_full_cases()

    class Harness:
        instances = []

        def __init__(self, solver: str) -> None:
            nonlocal next_pid
            type(self).instances.append(self)
            self.solver = solver
            self.worker_pid = next_pid
            next_pid += 1
            self.start_count = 0
            self.rebuild_count = 0
            self._memory_limit_bytes: int | None = None
            self.sample_kinds: list[str] = []
            self.exited = False
            self.exit_exception = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            del exc, traceback
            self.exited = True
            self.exit_exception = exc_type
            return None

        def submit(self, request):
            if self._memory_limit_bytes is None:
                self.start_count += 1
            elif self._memory_limit_bytes != request.limits.memory_limit_bytes:
                self.start_count += 1
                self.rebuild_count += 1
            self._memory_limit_bytes = request.limits.memory_limit_bytes
            slot = int(request.request_id.rsplit(":", 1)[1])
            sample_kind = request.request_id.split(":", 4)[3]
            self.sample_kinds.append(sample_kind)
            if sample_kind == raise_on_sample_kind:
                raise RuntimeError("FAKE_SAMPLE_FAILURE")
            if not cleanup_proven:
                return WorkerOutcome(
                    request.request_id, "UNKNOWN", "CLEANUP_UNPROVEN", self.worker_pid,
                    self.worker_pid, slot + 1, False, False,
                )
            if termination is not None:
                return WorkerOutcome(
                    request.request_id, "UNKNOWN", termination, self.worker_pid,
                    self.worker_pid, slot + 1, False, True,
                )
            case = next(case for case in cases if case.request == request.request)
            if case.expected_result is None:
                return WorkerOutcome(
                    request.request_id, "UNKNOWN", "HARD_TIMEOUT", self.worker_pid,
                    self.worker_pid, slot + 1, False, True,
                )
            assert case.expected_result is not None
            evidence = _evidence_for_result(case.expected_result)
            if batch_semantics_differ and ":throughput:" in request.request_id and slot == 1:
                evidence = replace(evidence, master_rounds=2)
            response = WorkerResponse(
                BENCHMARK_PROTOCOL_V1,
                self.solver,
                request.request_id,
                "OK",
                {} if hard_check_failure else canonical_payload(evidence),
                {name: 0 for name in benchmark.WORKER_PHASE_NAMES},
                (),
            )
            return WorkerOutcome(
                request.request_id, "OK", "COMPLETED", self.worker_pid, self.worker_pid,
                slot + 1, False, True, response,
            )

    return Harness


def _run_tiny_full_plan(harness_factory) -> list[dict[str, object]]:
    cases = benchmark._load_full_cases()
    environments, artifacts = _full_environment_fixture()
    manifest = benchmark._full_manifest(cases, environments, artifacts)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(benchmark, "_SOLVERS", ("highs",))
        monkeypatch.setattr(
            benchmark,
            "_full_sample_plan",
            lambda _: (
                (cases[0].case_id, "warmup", 0, 1),
                ("single_contract_complement", "throughput", 0, 2),
            ),
        )
        return benchmark._run_full_environment(cases, manifest, "macos", harness_factory)


def test_full_environment_runner_emits_the_exact_plan_and_record_counters(monkeypatch) -> None:
    cases = benchmark._load_full_cases()
    environments, artifacts = _full_environment_fixture()
    manifest = benchmark._full_manifest(cases, environments, artifacts)
    monkeypatch.setattr(benchmark, "_SOLVERS", ("highs",))
    monkeypatch.setattr(
        benchmark,
        "_full_sample_plan",
        lambda _: (
            (cases[0].case_id, "warmup", 0, 1),
            (cases[0].case_id, "warm", 0, 1),
            ("single_contract_complement", "throughput", 0, 2),
            ("single_contract_complement", "cold", 0, 1),
            ("single_contract_complement", "rebuild", 0, 1),
        ),
    )

    factory = _fake_harness_factory()
    records = benchmark._run_full_environment(cases, manifest, "macos", factory)

    assert [(row["sample_kind"], row["worker_count"], row["completed_requests"]) for row in records] == [
        ("warmup", 1, 1),
        ("warm", 1, 1),
        ("throughput", 2, 2),
        ("cold", 1, 1),
        ("rebuild", 1, 1),
    ]
    assert records[-1]["worker_rebuild_count"] >= 1
    assert records[2]["peak_aggregate_rss_bytes"] == sum(_fake_batch_peaks(2))
    assert all(row["profile"] == "full" for row in records)
    assert all(benchmark._validated_benchmark_record(row, manifest) for row in records)
    for sample_kind in ("warmup", "warm", "throughput", "cold", "rebuild"):
        harnesses = [harness for harness in factory.instances if sample_kind in harness.sample_kinds]
        assert harnesses
        assert all(harness.exited and harness.exit_exception is None for harness in harnesses)


def test_full_environment_runner_stops_on_unproven_cleanup_or_semantic_mismatch() -> None:
    with pytest.raises(RuntimeError, match="CLEANUP_UNPROVEN"):
        _run_tiny_full_plan(_fake_harness_factory(cleanup_proven=False))
    with pytest.raises(RuntimeError, match="SEMANTIC_NONDETERMINISM"):
        _run_tiny_full_plan(_fake_harness_factory(batch_semantics_differ=True))


def test_full_environment_runner_stops_on_hard_check_failure() -> None:
    with pytest.raises(RuntimeError, match="CHECK_HARD_FAILURE"):
        _run_tiny_full_plan(_fake_harness_factory(hard_check_failure=True))


@pytest.mark.parametrize("termination", ("MEMORY_LIMIT", "CRASH", "INVALID_OUTPUT", "PROTOCOL_MISMATCH", "unrecognized-format-failure"))
def test_full_environment_runner_stops_immediately_on_fatal_worker_termination(termination: str) -> None:
    factory = _fake_harness_factory(termination=termination)

    with pytest.raises(RuntimeError, match="FATAL_WORKER_TERMINATION"):
        _run_tiny_full_plan(factory)

    assert sum(len(harness.sample_kinds) for harness in factory.instances) == 1


@pytest.mark.parametrize("termination", ("SOFT_TIMEOUT", "HARD_TIMEOUT"))
def test_full_environment_runner_preserves_modeled_timeouts_as_unknown(termination: str, monkeypatch) -> None:
    cases = benchmark._load_full_cases()
    environments, artifacts = _full_environment_fixture()
    manifest = benchmark._full_manifest(cases, environments, artifacts)
    monkeypatch.setattr(benchmark, "_SOLVERS", ("highs",))
    monkeypatch.setattr(benchmark, "_full_sample_plan", lambda _: ((cases[0].case_id, "warmup", 0, 1),))

    records = benchmark._run_full_environment(cases, manifest, "macos", _fake_harness_factory(termination=termination))

    assert records[0]["solver_run"]["termination_reason"] == termination
    assert records[0]["solver_run"]["business_status"] == "UNKNOWN"


@pytest.mark.parametrize("sample_kind", ("warmup", "throughput", "cold", "rebuild"))
def test_full_environment_runner_exits_each_harness_context_on_exception(
    sample_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = benchmark._load_full_cases()
    environments, artifacts = _full_environment_fixture()
    manifest = benchmark._full_manifest(cases, environments, artifacts)
    case_id = cases[0].case_id if sample_kind == "warmup" else "single_contract_complement"
    worker_count = 2 if sample_kind == "throughput" else 1
    factory = _fake_harness_factory(raise_on_sample_kind=sample_kind)
    monkeypatch.setattr(benchmark, "_SOLVERS", ("highs",))
    monkeypatch.setattr(
        benchmark,
        "_full_sample_plan",
        lambda _: ((case_id, sample_kind, 0, worker_count),),
    )

    with pytest.raises(RuntimeError, match="FAKE_SAMPLE_FAILURE"):
        benchmark._run_full_environment(cases, manifest, "macos", factory)

    harnesses = [harness for harness in factory.instances if sample_kind in harness.sample_kinds]
    assert harnesses
    assert all(harness.exited and harness.exit_exception is RuntimeError for harness in harnesses)


def test_full_environment_runner_reuses_each_throughput_worker_slot(monkeypatch) -> None:
    cases = benchmark._load_full_cases()
    environments, artifacts = _full_environment_fixture()
    manifest = benchmark._full_manifest(cases, environments, artifacts)
    factory = _fake_harness_factory()
    monkeypatch.setattr(benchmark, "_SOLVERS", ("highs",))
    monkeypatch.setattr(
        benchmark,
        "_full_sample_plan",
        lambda _: tuple(("single_contract_complement", "throughput", index, 2) for index in range(30)),
    )

    records = benchmark._run_full_environment(cases, manifest, "macos", factory)

    assert len(records) == 30
    assert len(factory.instances) == 3


def test_full_environment_runner_emits_monotonic_bounded_progress(monkeypatch) -> None:
    cases = benchmark._load_full_cases()
    environments, artifacts = _full_environment_fixture()
    manifest = benchmark._full_manifest(cases, environments, artifacts)
    messages: list[str] = []
    clock = iter((0.0, 0.0, 60.0, 60.0, 120.0))
    monkeypatch.setattr(benchmark, "_SOLVERS", ("highs",))
    monkeypatch.setattr(
        benchmark,
        "_full_sample_plan",
        lambda _: tuple((cases[0].case_id, "warmup", index, 1) for index in range(4)),
    )

    benchmark._run_full_environment(
        cases,
        manifest,
        "macos",
        _fake_harness_factory(),
        progress=messages.append,
        monotonic=lambda: next(clock),
    )

    assert len(messages) == 2
    assert "phase=warmup solver=highs" in messages[0]
    assert f"case={cases[0].case_id}" in messages[0]
    assert "sample=2/4 elapsed_seconds=60 current_rss_bytes=1024 peak_rss_bytes=1024" in messages[0]
    assert "sample=4/4 elapsed_seconds=120" in messages[1]


def _scip_exact_self_check_payload() -> dict[str, object]:
    return {
        "adapter": "ScipBackend",
        "solver": "SCIP+VIPR",
        "version": "10.0.2",
        "status": "OPTIMAL",
        "certificate_sha256": "sha256:" + "b" * 64,
        "certificate_size_bytes": 1,
        "completed_certificate_sha256": "sha256:" + "c" * 64,
        "completed_certificate_size_bytes": 1,
        "generation_ns": 1,
        "completion_ns": 1,
        "check_ns": 1,
        "corrupt_checker_exit_code": 1,
        "corrupt_check_ns": 1,
        "constrained_status": "OPTIMAL",
        "equality_status": "OPTIMAL",
        "ranged_status": "OPTIMAL",
        "infeasible_status": "INFEASIBLE",
        "lossy_status": "UNKNOWN",
        "lossy_native_status": "PROOF_UNCLOSED",
    }


def _install_fake_docker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scip_exact_payload: dict[str, object] | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command, **kwargs):
        del kwargs
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            solver = next(name for name in benchmark._SOLVERS if f"solver-{name}:" in command[-1])
            image_id = {"highs": "a", "scip": "b", "cp_sat": "c"}[solver] * 64
            return subprocess.CompletedProcess(command, 0, "sha256:" + image_id + "\n", "")
        if command[:3] == ["docker", "run", "--rm"]:
            Path(command[command.index("--cidfile") + 1]).write_text("e" * 64 + "\n")
            if "linux-platform-highs" in command[-1]:
                stdout = json.dumps({"architecture": "x86_64", "cpu": "fake-cpu", "os_version": "Linux", "probe": "linux-platform-highs", "python_version": "3.12.11"}) + "\n"
            elif "scip-exact" in command[-1]:
                stdout = json.dumps(scip_exact_payload or _scip_exact_self_check_payload()) + "\n"
            else:
                solver = command[-1]
                stdout = json.dumps({
                    "adapter": {"highs": "HighsBackend", "scip": "ScipBackend", "cp_sat": "CpSatBackend"}[solver],
                    "solver": {"highs": "highspy", "scip": "pyscipopt", "cp_sat": "ortools"}[solver],
                    "status": "OPTIMAL",
                    "version": benchmark._SOLVER_VERSIONS[solver],
                }) + "\n"
            return subprocess.CompletedProcess(command, 0, stdout, "")
        if command[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(command, 1, "", f"Error: No such object: {'e' * 64}\n")
        raise AssertionError(command)

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    monkeypatch.setattr(benchmark, "_current_git_sha", lambda: "1" * 40)
    return calls


def test_linux_discovery_requires_pinned_images_and_scip_exact_smoke(monkeypatch) -> None:
    calls = _install_fake_docker(monkeypatch)

    environment, artifacts = benchmark._discover_linux_environment()

    assert environment["available"] is True
    assert set(artifacts) == {"highs", "scip", "cp_sat"}
    assert any("scip-exact" in " ".join(call) for call in calls)
    assert all(item["image_id"].startswith("sha256:") for item in artifacts.values())
    assert artifacts["scip"]["run_succeeded"] is True


def test_linux_discovery_runs_every_probe_by_the_resolved_immutable_image_id(monkeypatch) -> None:
    calls = _install_fake_docker(monkeypatch)

    _, artifacts = benchmark._discover_linux_environment()

    docker_runs = [call for call in calls if call[:3] == ["docker", "run", "--rm"]]
    image_arguments = [call[call.index("PYTHONNOUSERSITE=1") + 1] for call in docker_runs]
    assert image_arguments == [
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
        "sha256:" + "b" * 64,
        "sha256:" + "c" * 64,
        "sha256:" + "a" * 64,
    ]
    assert artifacts["highs"]["build_id"].startswith("open-trader-prediction-solver-highs:")
    assert artifacts["highs"]["image_id"] == "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "mutator",
    (
        lambda payload: payload.pop("constrained_status"),
        lambda payload: payload.__setitem__("equality_status", "UNKNOWN"),
        lambda payload: payload.__setitem__("ranged_status", "FEASIBLE"),
        lambda payload: payload.__setitem__("infeasible_status", "OPTIMAL"),
        lambda payload: payload.__setitem__("corrupt_checker_exit_code", 0),
        lambda payload: payload.__setitem__("lossy_status", "OPTIMAL"),
        lambda payload: payload.__setitem__("lossy_native_status", "closed"),
    ),
)
def test_linux_discovery_rejects_missing_or_invalid_scip_exact_evidence(monkeypatch, mutator) -> None:
    payload = _scip_exact_self_check_payload()
    mutator(payload)
    _install_fake_docker(monkeypatch, scip_exact_payload=payload)

    assert benchmark._discover_linux_environment() is None


def _install_discovery_container_run(monkeypatch, mode: str) -> list[list[str]]:
    calls: list[list[str]] = []
    inspections = 0

    def run(command, **kwargs):
        nonlocal inspections
        del kwargs
        calls.append(command)
        if command[:3] == ["docker", "run", "--rm"]:
            cidfile = Path(command[command.index("--cidfile") + 1])
            cidfile.write_text("d" * 64 + "\n")
            if mode == "timeout":
                raise subprocess.TimeoutExpired(command, 10)
            if mode == "oserror":
                raise OSError("docker startup failed")
            return subprocess.CompletedProcess(command, 1 if mode == "startup" else 0, '{"status":"OPTIMAL"}\n', "startup failed" if mode == "startup" else "")
        if command[:2] == ["docker", "inspect"]:
            inspections += 1
            if mode == "ambiguous":
                return subprocess.CompletedProcess(command, 1, "", "permission denied\n")
            if mode == "survivor" and inspections == 1:
                return subprocess.CompletedProcess(command, 0, "{}\n", "")
            return subprocess.CompletedProcess(command, 1, "", f"Error: No such object: {'d' * 64}\n")
        if command[:3] == ["docker", "rm", "--force"]:
            return subprocess.CompletedProcess(command, 0, "d" * 64 + "\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    return calls


def test_discovery_container_uses_cidfile_and_proves_exact_absence(monkeypatch) -> None:
    calls = _install_discovery_container_run(monkeypatch, "success")

    assert benchmark._docker_json_self_check("sha256:" + "a" * 64, ["python", "probe"], "probe") == {"status": "OPTIMAL"}

    run = calls[0]
    assert "--cidfile" in run
    assert [call[:2] for call in calls].count(["docker", "inspect"]) == 1


@pytest.mark.parametrize("mode", ("startup", "timeout", "oserror"))
def test_discovery_container_proves_absence_after_run_failure(monkeypatch, mode) -> None:
    calls = _install_discovery_container_run(monkeypatch, mode)

    assert benchmark._docker_json_self_check("sha256:" + "a" * 64, ["python", "probe"], "probe") is None

    assert any(call[:2] == ["docker", "inspect"] for call in calls)


@pytest.mark.parametrize("mode", ("ambiguous", "survivor"))
def test_discovery_container_stops_on_unproven_or_forced_cleanup(monkeypatch, mode) -> None:
    calls = _install_discovery_container_run(monkeypatch, mode)

    with pytest.raises(RuntimeError, match="CONTAINER_CLEANUP_UNPROVEN"):
        benchmark._docker_json_self_check("sha256:" + "a" * 64, ["python", "probe"], "probe")

    if mode == "survivor":
        assert ["docker", "rm", "--force", "d" * 64] in calls


def _install_fake_docker_worker(monkeypatch: pytest.MonkeyPatch, *, container_survives: bool = False) -> list[list[str]]:
    commands: list[list[str]] = []
    inspections: dict[str, int] = {}

    class Harness:
        def __init__(self, command, **kwargs) -> None:
            del kwargs
            commands.append(command)
            cidfile = Path(command[command.index("--cidfile") + 1])
            cidfile.write_text("a" * 64 + "\n")
            self.inner = _fake_harness_factory()(command[-1])

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def submit(self, request):
            return self.inner.submit(request)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def run(command, **kwargs):
        del kwargs
        if command[:2] == ["docker", "inspect"]:
            container_id = command[-1]
            inspections[container_id] = inspections.get(container_id, 0) + 1
            if container_survives and inspections[container_id] == 1:
                return subprocess.CompletedProcess(command, 0, "{}\n", "")
            return subprocess.CompletedProcess(command, 1, "", f"Error: No such object: {container_id}\n")
        if command[:3] == ["docker", "rm", "--force"]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, command[-1] + "\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(benchmark, "WorkerHarness", Harness)
    monkeypatch.setattr(benchmark.subprocess, "run", run)
    monkeypatch.setattr(benchmark, "_fake_docker_inspections", inspections, raising=False)
    return commands


def _run_tiny_linux_plan() -> list[dict[str, object]]:
    cases = benchmark._load_full_cases()
    environments, artifacts = _full_environment_fixture()
    manifest = benchmark._full_manifest(cases, environments, artifacts)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(benchmark, "_SOLVERS", ("highs",))
        monkeypatch.setattr(benchmark, "_full_sample_plan", lambda _: ((cases[0].case_id, "warmup", 0, 1),))
        return benchmark._run_full_environment(
            cases,
            manifest,
            "linux",
            lambda solver: benchmark._docker_harness("pinned-image", solver),
        )


def test_docker_harness_is_networkless_read_only_and_proves_container_cleanup(monkeypatch) -> None:
    commands = _install_fake_docker_worker(monkeypatch)

    records = _run_tiny_linux_plan()

    command = commands[0]
    cidfile = command[command.index("--cidfile") + 1]
    assert command == [
        "docker", "run", "--rm", "--interactive", "--network", "none",
        "--cidfile", cidfile,
        "--volume", f"{benchmark._ROOT}:/workspace:ro", "--workdir", "/workspace",
        "--env", "PYTHONPATH=/workspace/src", "--env", "PYTHONSAFEPATH=1",
        "--env", "PYTHONNOUSERSITE=1", "pinned-image",
        "python", "-m", "open_trader.prediction_solver_worker", "--backend", "highs",
    ]
    assert records[0]["container_id"] == "a" * 64
    assert benchmark._fake_docker_inspections["a" * 64] >= 1


def test_linux_cleanup_survivor_is_removed_and_the_run_stops(monkeypatch) -> None:
    commands = _install_fake_docker_worker(monkeypatch, container_survives=True)

    with pytest.raises(RuntimeError, match="CONTAINER_CLEANUP_UNPROVEN"):
        _run_tiny_linux_plan()

    assert [command for command in commands if command[:3] == ["docker", "rm", "--force"]] == [
        ["docker", "rm", "--force", "a" * 64]
    ]


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    (
        (1, "", "Error: No such object: " + "a" * 64 + "\n", True),
        (1, "", "permission denied while trying to connect to the Docker daemon\n", False),
        (1, "", "unexpected inspect failure\n", False),
        (2, "", "Error: No such object: " + "a" * 64 + "\n", False),
        (1, " \n", "Error: No such object: " + "a" * 64 + "\n", False),
        (1, "", "Error: No such object: " + "a" * 64, False),
        (1, "[]\n", "Error response from daemon: No such container\n", False),
        (1, "[]\n", "Error response from daemon: No such container: " + "b" * 64 + "\n", False),
        (1, "[]\n", "permission denied while trying to connect to the Docker daemon\n", False),
        (1, "", "Error response from daemon: No such container: " + "a" * 64 + "\n", False),
        (2, "[]\n", "Error response from daemon: No such container: " + "a" * 64 + "\n", False),
    ),
)
def test_container_absence_requires_the_exact_no_such_object_result(monkeypatch, returncode, stdout, stderr, expected) -> None:
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, returncode, stdout, stderr),
    )

    assert benchmark._container_is_absent("a" * 64) is expected


def test_container_absence_accepts_the_exact_docker_24_missing_container_result(monkeypatch) -> None:
    container_id = "a" * 64
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            "[]\n",
            f"Error response from daemon: No such container: {container_id}\n",
        ),
    )

    assert benchmark._container_is_absent(container_id)


@pytest.mark.parametrize("field,value", (("soft_time_limit_ms", 4_999), ("hard_time_limit_ms", 20_001), ("max_constraint_generation_rounds", 65)))
def test_full_linux_rejects_tampered_partial_execution_limits_before_docker(tmp_path, monkeypatch, field, value) -> None:
    environments, artifacts = _full_environment_fixture()
    environments["linux"]["available"] = False
    manifest = benchmark._full_manifest(benchmark._load_full_cases(), environments, artifacts)
    manifest[field] = value
    (tmp_path / "environment_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "macos.jsonl").write_text("")
    monkeypatch.setattr(benchmark, "_current_git_sha", lambda: "1" * 40)
    monkeypatch.setattr(benchmark, "_discover_linux_environment", lambda: pytest.fail("docker discovered"))
    monkeypatch.setattr(benchmark, "_full_sample_plan", lambda _: ())
    monkeypatch.setattr(benchmark, "aggregate_benchmark_records", lambda records, current: {})

    with pytest.raises(ValueError, match="identity"):
        benchmark._run_full_linux(tmp_path)


def test_full_linux_rejects_incomplete_partial_manifest_before_docker(tmp_path, monkeypatch) -> None:
    environments, artifacts = _full_environment_fixture()
    environments["linux"]["available"] = False
    manifest = benchmark._full_manifest(benchmark._load_full_cases(), environments, artifacts)
    del manifest["memory_limit_bytes"]
    (tmp_path / "environment_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "macos.jsonl").write_text("")
    monkeypatch.setattr(benchmark, "_discover_linux_environment", lambda: pytest.fail("docker discovered"))

    with pytest.raises(ValueError, match="macOS partial is invalid"):
        benchmark._run_full_linux(tmp_path)


def _current_macos_partial(tmp_path, monkeypatch) -> tuple[dict[str, object], dict[str, dict[str, dict[str, object]]]]:
    environments, artifacts = _full_environment_fixture()
    environments["linux"] = benchmark._unavailable_linux_environment()
    for solver in benchmark._SOLVERS:
        artifacts[solver]["linux"] = benchmark._unavailable_linux_artifact()
    manifest = benchmark._full_manifest(benchmark._load_full_cases(), environments, artifacts)
    (tmp_path / "environment_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "macos.jsonl").write_text("")
    monkeypatch.setattr(
        benchmark,
        "_discover_quick_environment",
        lambda root: (environments["macos"], {solver: artifacts[solver]["macos"] for solver in benchmark._SOLVERS}),
    )
    monkeypatch.setattr(benchmark, "_current_git_sha", lambda: "1" * 40)
    monkeypatch.setattr(benchmark, "_full_sample_plan", lambda _: ())
    monkeypatch.setattr(benchmark, "aggregate_benchmark_records", lambda records, current: {})
    return manifest, artifacts


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("license_evidence_present", False),
        ("source_evidence_present", False),
        ("open_source", False),
        ("commercial_key_required", True),
    ),
)
def test_full_linux_rejects_stale_macos_license_evidence_before_docker(tmp_path, monkeypatch, field, value) -> None:
    manifest, _ = _current_macos_partial(tmp_path, monkeypatch)
    for solver in benchmark._SOLVERS:
        manifest["solvers"][solver]["environments"]["macos"][field] = value
    (tmp_path / "environment_manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(benchmark, "_discover_linux_environment", lambda: pytest.fail("docker discovered"))

    with pytest.raises(ValueError, match="identity"):
        benchmark._run_full_linux(tmp_path)


def test_full_linux_rejects_jointly_tampered_macos_solver_build_and_version_before_docker(tmp_path, monkeypatch) -> None:
    manifest, _ = _current_macos_partial(tmp_path, monkeypatch)
    for solver in benchmark._SOLVERS:
        evidence = manifest["solvers"][solver]["environments"]["macos"]
        evidence["build_id"] = f"tampered-{solver}-build"
        evidence["solver_version"] = "tampered-version"
    (tmp_path / "environment_manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(benchmark, "_discover_linux_environment", lambda: pytest.fail("docker discovered"))

    with pytest.raises(ValueError, match="identity"):
        benchmark._run_full_linux(tmp_path)


def test_full_linux_rejects_mac_hard_gate_result_before_docker(tmp_path, monkeypatch) -> None:
    _current_macos_partial(tmp_path, monkeypatch)
    monkeypatch.setattr(
        benchmark,
        "aggregate_benchmark_records",
        lambda records, current: {"solvers": {solver: {"hard_gate_failures": ["CHECK_HARD_FAILURE"]} for solver in benchmark._SOLVERS}},
    )

    with pytest.raises(ValueError, match="macOS partial hard gates failed"):
        benchmark._read_full_macos_partial(tmp_path)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("environment", "architecture", "tampered-linux"),
        ("environment", "git_sha", "2" * 40),
        ("artifact", "build_id", "tampered-build"),
        ("artifact", "image_id", "sha256:" + "f" * 64),
        ("artifact", "run_succeeded", True),
    ),
)
def test_full_linux_rejects_tampered_unavailable_linux_placeholder_before_docker(
    tmp_path,
    monkeypatch,
    section,
    field,
    value,
) -> None:
    manifest, _ = _current_macos_partial(tmp_path, monkeypatch)
    if section == "environment":
        manifest["environments"]["linux"][field] = value
    else:
        manifest["solvers"]["highs"]["environments"]["linux"][field] = value
    (tmp_path / "environment_manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(benchmark, "_discover_linux_environment", lambda: pytest.fail("docker discovered"))

    with pytest.raises(ValueError, match="identity"):
        benchmark._run_full_linux(tmp_path)


def test_full_linux_requires_a_complete_validated_macos_partial_before_docker(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(benchmark, "_discover_linux_environment", lambda: pytest.fail("docker discovered"))

    with pytest.raises(ValueError, match="macOS partial"):
        benchmark._run_full_linux(tmp_path)


def test_full_linux_refuses_existing_linux_evidence_before_docker(tmp_path, monkeypatch) -> None:
    (tmp_path / "linux.jsonl").write_text("foreign\n")
    monkeypatch.setattr(benchmark, "_discover_linux_environment", lambda: pytest.fail("docker discovered"))

    with pytest.raises(ValueError, match="linux benchmark evidence already exists"):
        benchmark._run_full_linux(tmp_path)


def test_full_linux_completes_the_partial_without_rewriting_macos_evidence(tmp_path, monkeypatch) -> None:
    _install_full_macos_fakes(monkeypatch)
    assert benchmark._run_full_macos(tmp_path, tmp_path / "envs") == 0
    macos_before = (tmp_path / "macos.jsonl").read_bytes()
    environments, artifacts = _full_environment_fixture()
    linux_factory = _fake_harness_factory()
    worker_images: list[str] = []
    monkeypatch.setattr(benchmark, "_current_git_sha", lambda: "1" * 40)
    monkeypatch.setattr(
        benchmark,
        "_discover_linux_environment",
        lambda: (environments["linux"], {solver: artifacts[solver]["linux"] for solver in benchmark._SOLVERS}),
    )
    monkeypatch.setattr(
        benchmark,
        "_docker_harness",
        lambda image, solver: worker_images.append(image) or linux_factory(solver),
    )

    assert benchmark._run_full_linux(tmp_path) == 0

    manifest = json.loads((tmp_path / "environment_manifest.json").read_text())
    macos_records = (tmp_path / "macos.jsonl").read_bytes()
    linux_records = (tmp_path / "linux.jsonl").read_text().splitlines()
    assert macos_records == macos_before
    assert manifest["environments"]["linux"]["available"] is True
    assert set(worker_images) == {artifacts[solver]["linux"]["image_id"] for solver in benchmark._SOLVERS}
    assert len(linux_records) == 4_755
    assert len(macos_records.splitlines()) + len(linux_records) == 9_510
    benchmark.aggregate_benchmark_records(
        [json.loads(line) for line in macos_records.splitlines() + linux_records], manifest
    )


def _install_full_macos_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_after_records: int | None = None,
) -> None:
    environments, artifacts = _full_environment_fixture()
    cases = benchmark._load_full_cases()
    factory = _fake_harness_factory()
    submitted = 0

    monkeypatch.setattr(
        benchmark,
        "_discover_quick_environment",
        lambda root: (environments["macos"], {solver: artifacts[solver]["macos"] for solver in benchmark._SOLVERS}),
    )

    class Harness:
        def __init__(self, command, **kwargs) -> None:
            assert command == [
                str(Path(kwargs["env"]["PATH"].split(":", 1)[0]) / "python"),
                "-m",
                "open_trader.prediction_solver_worker",
                "--backend",
                command[-1],
            ]
            assert kwargs["request_timeout_ms"] == 20_000
            assert kwargs["startup_timeout_ms"] == 5_000
            self.inner = factory(command[-1])

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def submit(self, request):
            nonlocal submitted
            submitted += 1
            if fail_after_records is not None and submitted > fail_after_records:
                return WorkerOutcome(
                    request.request_id, "UNKNOWN", "CLEANUP_UNPROVEN", 1, 1, 0, False, False,
                )
            return self.inner.submit(request)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(benchmark, "WorkerHarness", Harness)


def test_full_macos_writes_only_a_complete_atomic_partial_handoff(tmp_path, monkeypatch) -> None:
    _install_full_macos_fakes(monkeypatch)

    assert benchmark._run_full_macos(tmp_path, tmp_path / "envs") == 0

    manifest = json.loads((tmp_path / "environment_manifest.json").read_text())
    records = [json.loads(line) for line in (tmp_path / "macos.jsonl").read_text().splitlines()]
    assert manifest["environments"]["macos"]["available"] is True
    assert manifest["environments"]["linux"]["available"] is False
    assert len(records) == len(benchmark._full_sample_plan(benchmark._load_full_cases())) * 3
    assert not (tmp_path / "linux.jsonl").exists()


def test_full_macos_failure_leaves_no_final_artifact(tmp_path, monkeypatch) -> None:
    _install_full_macos_fakes(monkeypatch, fail_after_records=2)

    with pytest.raises(RuntimeError, match="CLEANUP_UNPROVEN"):
        benchmark._run_full_macos(tmp_path, tmp_path / "envs")

    assert not (tmp_path / "macos.jsonl").exists()
    assert not (tmp_path / "environment_manifest.json").exists()


def test_full_macos_fatal_termination_never_reaches_publication(tmp_path, monkeypatch) -> None:
    _install_full_macos_publication_fakes(monkeypatch)

    def fail_before_publication(*args):
        del args
        raise RuntimeError("FATAL_WORKER_TERMINATION:CRASH")

    monkeypatch.setattr(benchmark, "_run_full_environment", fail_before_publication)

    with pytest.raises(RuntimeError, match="FATAL_WORKER_TERMINATION:CRASH"):
        benchmark._run_full_macos(tmp_path, tmp_path / "envs")

    assert not (tmp_path / "macos.jsonl").exists()
    assert not (tmp_path / "environment_manifest.json").exists()


def _install_full_macos_publication_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    environments, artifacts = _full_environment_fixture()
    monkeypatch.setattr(
        benchmark,
        "_discover_quick_environment",
        lambda root: (environments["macos"], {solver: artifacts[solver]["macos"] for solver in benchmark._SOLVERS}),
    )
    monkeypatch.setattr(benchmark, "aggregate_benchmark_records", lambda records, manifest: {})


@pytest.mark.parametrize("kind", ("file", "symlink"))
def test_full_macos_rejects_unsafe_output_root_before_discovery(tmp_path, monkeypatch, kind: str) -> None:
    output = tmp_path / "results"
    if kind == "file":
        output.write_text("not a directory\n")
    else:
        target = tmp_path / "foreign-results"
        target.mkdir()
        output.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(benchmark, "_discover_quick_environment", lambda root: pytest.fail("environment discovered"))

    with pytest.raises(ValueError, match="output root"):
        benchmark._run_full_macos(output, tmp_path / "envs")


@pytest.mark.parametrize("name", ("macos.jsonl", "linux.jsonl", "environment_manifest.json"))
@pytest.mark.parametrize("kind", ("file", "directory", "symlink"))
def test_full_macos_rejects_every_unsafe_final_path_before_discovery(
    tmp_path,
    monkeypatch,
    name: str,
    kind: str,
) -> None:
    output = tmp_path / "results"
    output.mkdir()
    path = output / name
    if kind == "file":
        path.write_text("foreign\n")
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(tmp_path / "missing-foreign-evidence")
    monkeypatch.setattr(benchmark, "_discover_quick_environment", lambda root: pytest.fail("environment discovered"))

    with pytest.raises(ValueError, match="already exists"):
        benchmark._run_full_macos(output, tmp_path / "envs")


@pytest.mark.parametrize("name", ("macos.jsonl", "environment_manifest.json"))
def test_full_macos_never_replaces_foreign_evidence_created_after_preflight(
    tmp_path,
    monkeypatch,
    name: str,
) -> None:
    output = tmp_path / "results"
    foreign = b"foreign evidence\n"
    _install_full_macos_publication_fakes(monkeypatch)

    def run(*args):
        del args
        output.mkdir(exist_ok=True)
        (output / name).write_bytes(foreign)
        return [{"record": "ours"}]

    monkeypatch.setattr(benchmark, "_run_full_environment", run)

    with pytest.raises(ValueError, match="already exists"):
        benchmark._run_full_macos(output, tmp_path / "envs")

    assert (output / name).read_bytes() == foreign
    assert not any(path.name.startswith(f".{name}.") for path in output.iterdir())


def test_full_macos_manifest_failure_leaves_an_invalid_fail_closed_partial_package(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "results"
    _install_full_macos_publication_fakes(monkeypatch)
    monkeypatch.setattr(benchmark, "_run_full_environment", lambda *args: [{"record": "ours"}])
    original_write_json = benchmark._atomic_write_json

    def fail_manifest(path, payload, **kwargs):
        del path, payload, kwargs
        raise OSError("injected manifest publication failure")

    monkeypatch.setattr(benchmark, "_atomic_write_json", fail_manifest)

    with pytest.raises(OSError, match="injected manifest publication failure"):
        benchmark._run_full_macos(output, tmp_path / "envs")

    assert (output / "macos.jsonl").is_file()
    assert not (output / "environment_manifest.json").exists()
    assert {path.name for path in output.iterdir()} == {"macos.jsonl"}
    monkeypatch.setattr(benchmark, "_FINAL_RESULTS", output)
    with pytest.raises(ValueError, match="absent"):
        benchmark._require_final_inputs()
    monkeypatch.setattr(benchmark, "_atomic_write_json", original_write_json)
    monkeypatch.setattr(benchmark, "_discover_quick_environment", lambda root: pytest.fail("environment discovered"))
    with pytest.raises(ValueError, match="already exists"):
        benchmark._run_full_macos(output, tmp_path / "envs")


@pytest.mark.parametrize("name", ("macos.jsonl", "linux.jsonl", "environment_manifest.json"))
def test_final_input_reader_rejects_symlinked_evidence(tmp_path, monkeypatch, name: str) -> None:
    output = tmp_path / "results"
    output.mkdir()
    for final_name in ("macos.jsonl", "linux.jsonl", "environment_manifest.json"):
        (output / final_name).write_text("{}\n")
    target = tmp_path / "foreign-evidence"
    target.write_text("{}\n")
    (output / name).unlink()
    (output / name).symlink_to(target)
    monkeypatch.setattr(benchmark, "_FINAL_RESULTS", output)

    with pytest.raises(ValueError, match="unsafe"):
        benchmark._require_final_inputs()


def test_quick_runner_is_serial_and_replays_only_semantic_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases = load_canonical_cases()[:2]
    env_root = _test_quick_environment(tmp_path)
    monkeypatch.setattr(benchmark, "_load_quick_cases", lambda: cases)
    fixture_manifest = _benchmark_manifest(profile="quick")
    monkeypatch.setattr(
        benchmark,
        "_discover_quick_environment",
        lambda root: (
            fixture_manifest["environments"]["macos"],
            {
                solver: fixture_manifest["solvers"][solver]["environments"]["macos"]
                for solver in ("highs", "scip", "cp_sat")
            },
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-worker")
    monkeypatch.setenv("HTTPS_PROXY", "http://credential-bearing-proxy.invalid")
    active = 0
    maximum_active = 0
    order: list[tuple[str, str, object]] = []
    worker_environments: list[dict[str, str]] = []

    class Harness:
        def __init__(self, command, **kwargs):
            self.solver = command[-1]
            self.start_count = 1
            self.rebuild_count = 0
            worker_environments.append(kwargs["env"])

        def __enter__(self):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            return self

        def __exit__(self, *args):
            nonlocal active
            active -= 1

        def submit(self, request):
            order.append((self.solver, request.request_id, request.request.budget))
            case = next(item for item in cases if item.case_id == request.request_id)
            phases = {name: 0 for name in benchmark.WORKER_PHASE_NAMES}
            response = WorkerResponse(
                BENCHMARK_PROTOCOL_V1,
                self.solver,
                request.request_id,
                "OK",
                canonical_payload(_evidence_for_result(case.expected_result)),
                phases,
                (),
            )
            return WorkerOutcome(request.request_id, "OK", "COMPLETED", 1, 1, 1, False, True, response)

    monkeypatch.setattr(benchmark, "WorkerHarness", Harness)
    output = tmp_path / "quick"

    assert benchmark._run_quick_benchmark(output, env_root) == 0
    first = (output / "records.jsonl").read_bytes()
    assert benchmark._run_quick_benchmark(output, env_root) == 0
    second = (output / "records.jsonl").read_bytes()

    expected_order = [
        (solver, case.case_id, case.budget)
        for solver in ("highs", "scip", "cp_sat")
        for case in cases
    ]
    assert maximum_active == 1
    assert order == expected_order * 2
    assert worker_environments
    assert all(
        environment == {
            "PATH": f"{env_root / solver / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONSAFEPATH": "1",
        }
        for environment, solver in zip(worker_environments[:3], ("highs", "scip", "cp_sat"), strict=True)
    )
    assert first != second
    assert "semantic replay PASS" in capsys.readouterr().out
    records = [json.loads(line) for line in second.splitlines()]
    assert all(record["memory_limit_bytes"] == 1 << 40 for record in records)
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert (
        manifest["soft_time_limit_ms"],
        manifest["hard_time_limit_ms"],
        manifest["memory_limit_bytes"],
        manifest["max_constraint_generation_rounds"],
    ) == (5_000, 20_000, 1 << 40, 64)

    first_line = second.splitlines(keepends=True)[0]
    (output / "records.jsonl").write_bytes(second + first_line)
    with pytest.raises(ValueError, match="duplicate structural sample"):
        benchmark._run_quick_benchmark(output, env_root)
    (output / "records.jsonl").write_bytes(second)

    monkeypatch.setattr(
        benchmark,
        "aggregate_benchmark_records",
        lambda records, run_manifest: {
            "solvers": {
                solver: {"hard_gate_failures": ["CHECK_HARD_FAILURE"]}
                for solver in ("highs", "scip", "cp_sat")
            }
        },
    )
    with pytest.raises(RuntimeError, match="quick hard gate failed"):
        benchmark._run_quick_benchmark(output, env_root)


def test_open_trader_dispatches_only_the_prediction_solver_benchmark_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import open_trader.__main__ as entrypoint

    seen: list[list[str]] = []
    monkeypatch.setattr(benchmark, "main", lambda args: seen.append(args) or 17)

    assert entrypoint.main(["prediction-solver-benchmark", "quick"]) == 17
    assert seen == [["quick"]]


def test_benchmark_cli_dispatches_every_operator_command_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(benchmark, "_run_quick_benchmark", lambda: calls.append("quick") or 0)
    monkeypatch.setattr(benchmark, "_load_approved_corpus", lambda path: {"cases": [{}]})
    monkeypatch.setattr(benchmark, "_run_full_macos", lambda: calls.append("full-macos") or 2)
    monkeypatch.setattr(benchmark, "import_approved_snapshot", lambda inbox, corpus: calls.append(("import", inbox, corpus)))
    monkeypatch.setattr(benchmark, "_require_final_inputs", lambda: (_ for _ in ()).throw(ValueError("final Task 10 benchmark inputs are absent")))

    assert benchmark.main(["quick"]) == 0
    assert benchmark.main(["full", "--environment", "macos"]) == 2
    assert benchmark.main(["import-approved", str(tmp_path / "approved_component.json"), str(tmp_path / "approved.json")]) == 0
    assert benchmark.main(["report"]) == 1
    assert benchmark.main(["verify-report"]) == 1
    assert calls == [
        "quick",
        "full-macos",
        ("import", str(tmp_path / "approved_component.json"), str(tmp_path / "approved.json")),
    ]
    assert capsys.readouterr().err.count("final Task 10 benchmark inputs are absent") == 2


def test_task10_report_cli_uses_the_root_environment_manifest_and_jsonl_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = ROOT / "benchmarks/prediction_solver/results/issue49"
    records = [output / "macos.jsonl", output / "linux.jsonl"]
    manifest = output / "environment_manifest.json"
    calls: list[tuple[str, object, object, object]] = []
    monkeypatch.setattr(
        benchmark,
        "generate_benchmark_report",
        lambda raw_records, raw_manifest, raw_output: calls.append(("report", raw_records, raw_manifest, raw_output)),
    )
    monkeypatch.setattr(
        benchmark,
        "verify_benchmark_report",
        lambda raw_records, raw_manifest, raw_output: calls.append(("verify", raw_records, raw_manifest, raw_output)),
    )
    monkeypatch.setattr(benchmark, "_require_final_inputs", benchmark._final_report_paths)

    assert benchmark._final_report_paths() == (records, manifest, output)
    assert benchmark.main(["report"]) == 0
    assert benchmark.main(["verify-report"]) == 0
    assert calls == [
        ("report", records, manifest, output),
        ("verify", records, manifest, output),
    ]


def test_make_targets_keep_environment_install_separate_from_quick_full_and_reports() -> None:
    commands = {
        target: subprocess.run(
            ["make", "-n", target],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        for target in (
            "prediction-solver-envs",
            "prediction-solver-quick",
            "prediction-solver-full-macos",
            "prediction-solver-full-linux",
            "prediction-solver-report",
            "prediction-solver-verify-report",
        )
    }

    assert all(result.returncode == 0 for result in commands.values())
    assert "build_prediction_solver_envs.sh" in commands["prediction-solver-envs"].stdout
    for target, subcommand in (
        ("prediction-solver-quick", "quick"),
        ("prediction-solver-full-macos", "full --environment macos"),
        ("prediction-solver-full-linux", "full --environment linux"),
        ("prediction-solver-report", "report"),
        ("prediction-solver-verify-report", "verify-report"),
    ):
        assert f"prediction-solver-benchmark {subcommand}" in commands[target].stdout
        assert "build_prediction_solver_envs.sh" not in commands[target].stdout
