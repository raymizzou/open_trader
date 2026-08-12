from __future__ import annotations

import hashlib
import json
import os
import random
import statistics
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from fractions import Fraction
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
from open_trader.prediction_solver import BENCHMARK_PROTOCOL_V1, BenchmarkClassification, SolverEvidence, TerminationReason


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
_RECORD_SCHEMA_V1 = "open_trader.prediction_solver.record.v1"
_MANIFEST_SCHEMA_V1 = "open_trader.prediction_solver.run_manifest.v1"
_SAMPLE_KINDS = {"warmup", "warm", "cold", "rebuild", "throughput"}
_RECORD_KEYS = {
    "schema_version", "profile", "environment", "case_id", "truth_method",
    "request_fingerprint", "problem_fingerprint", "git_sha", "cpu", "architecture",
    "os_version", "container_id", "image_id", "python_version", "solver_name",
    "solver_version", "adapter_version", "protocol_version", "corpus_version",
    "corpus_manifest_sha256", "license_manifest_sha256", "sample_kind", "sample_index",
    "worker_count", "worker_id", "worker_start_count", "worker_rebuild_count",
    "request_wall_ns", "completed_requests", "peak_process_group_rss_bytes",
    "peak_aggregate_rss_bytes", "memory_limit_bytes", "cleanup_proven",
    "semantic_fingerprint", "check_hard_failure", "check_failure_reason", "solver_run",
}
_MANIFEST_KEYS = {
    "schema_version", "profile", "approved_case_count", "required_environments",
    "required_solvers", "required_case_ids", "first_qualified_case_ids", "optimal_case_ids",
    "throughput_probe_case_ids", "cold_probe_case_ids", "rebuild_probe_case_ids",
    "worker_counts", "warmup_samples", "measured_samples", "corpus_manifest_sha256",
    "license_manifest_sha256", "environments", "solvers",
}
_RUN_KEYS = {
    "schema_version", "request_id", "solver_name", "solver_version", "adapter_version",
    "worker_id", "environment_id", "solve_status", "proof_status", "business_status",
    "optimality_status", "objective_bounds", "classification", "termination_reason",
    "evidence", "canonical_result", "phase_timings_ns", "peak_rss_bytes", "diagnostics",
}
_PHASE_NAMES = {
    "backend", "certificate_check", "certificate_completion", "certificate_generation",
    "first_qualified", "independent_check", "optimal", "serialization",
}
_ENVIRONMENT_KEYS = {"available", "build_id", "environment_id", "image_id"}
_SOLVER_KEYS = {
    "commercial_key_required", "install_succeeded", "installation_ns",
    "license_evidence_present", "manual_interventions", "open_source",
    "reuse_succeeded", "run_succeeded", "source_evidence_present",
    "whole_claim_certificate_bound",
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


def aggregate_benchmark_records(
    records: object,
    manifest: Mapping[str, object],
) -> dict[str, object]:
    manifest_value = _validated_run_manifest(manifest)
    if not isinstance(records, list | tuple):
        raise ValueError("records must be a list or tuple")
    decoded = tuple(_validated_benchmark_record(record, manifest_value) for record in records)
    _require_sample_matrix(decoded, manifest_value)
    solvers = {}
    for solver in sorted(manifest_value["required_solvers"]):
        solver_records = tuple(record for record in decoded if record["solver_name"] == solver)
        solvers[solver] = {
            "hard_gate_failures": _hard_gate_failures(solver_records, manifest_value, solver),
            "metrics": _aggregate_solver_metrics(decoded, manifest_value, solver),
        }
    decision = _benchmark_decision(solvers, manifest_value)
    return {
        "decision": decision,
        "profile": manifest_value["profile"],
        "solvers": solvers,
    }


def generate_benchmark_report(
    jsonl_paths: object,
    manifest_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
) -> None:
    if not isinstance(jsonl_paths, list | tuple) or not jsonl_paths:
        raise ValueError("jsonl_paths must be a non-empty list or tuple")
    manifest = _strict_json(Path(manifest_path).read_bytes(), "manifest")
    records = []
    for raw_path in jsonl_paths:
        path = Path(raw_path)
        with path.open("rb") as stream:
            for line_number, line in enumerate(stream, 1):
                if len(line) > 1024 * 1024:
                    raise ValueError(f"{path}:{line_number}: line limit exceeded")
                records.append(_strict_json(line, f"{path}:{line_number}"))
    summary = aggregate_benchmark_records(records, _mapping(manifest, "manifest"))
    manifest_value = _validated_run_manifest(_mapping(manifest, "manifest"))
    envelope = {
        "decision": summary["decision"],
        "environments": manifest_value["environments"],
        "hard_limits": {
            "memory_limit_bytes": sorted({record["memory_limit_bytes"] for record in records}),
            "unknown_mappings": ["HARD_TIMEOUT", "MEMORY_LIMIT", "ORACLE_DECISION_LIMIT_EXCEEDED", "ORACLE_STATE_LIMIT_EXCEEDED", "ORACLE_SUPPORT_LIMIT_EXCEEDED"],
        },
        "measured_dimensions": {
            "case_ids": list(manifest_value["required_case_ids"]),
            "cold_probe_case_ids": list(manifest_value["cold_probe_case_ids"]),
            "first_qualified_case_ids": list(manifest_value["first_qualified_case_ids"]),
            "measured_samples": manifest_value["measured_samples"],
            "optimal_case_ids": list(manifest_value["optimal_case_ids"]),
            "profiles": [manifest_value["profile"]],
            "rebuild_probe_case_ids": list(manifest_value["rebuild_probe_case_ids"]),
            "sample_kinds": sorted({record["sample_kind"] for record in records}),
            "solvers": sorted(manifest_value["required_solvers"]),
            "throughput_probe_case_ids": list(manifest_value["throughput_probe_case_ids"]),
            "warmup_samples": manifest_value["warmup_samples"],
            "worker_counts": list(manifest_value["worker_counts"]),
        },
        "proof_policy": {
            "benchmark_certificate_evidence": "UNBOUND_BENCHMARK_ONLY",
            "canonical_proof": "EXACT_ORACLE_CHECKING",
        },
        "selection": {
            "reason": summary["decision"]["reason"],
            "solver": summary["decision"]["selected_solver"],
            "status": summary["decision"]["status"],
        },
        "solver_evidence": summary["solvers"],
    }
    report = _markdown_report(summary)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_bytes(_json_bytes(summary))
    (output / "production_envelope.json").write_bytes(_json_bytes(envelope))
    (output / "report.md").write_text(report)


def verify_benchmark_report(
    jsonl_paths: object,
    manifest_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
) -> None:
    output = Path(output_dir)
    expected_names = {"summary.json", "production_envelope.json", "report.md"}
    actual_names = {path.name for path in output.iterdir()} if output.is_dir() else set()
    if actual_names != expected_names:
        raise ValueError(f"generated artifact names changed: missing={sorted(expected_names - actual_names)} extra={sorted(actual_names - expected_names)}")
    with tempfile.TemporaryDirectory() as directory:
        regenerated = Path(directory)
        generate_benchmark_report(jsonl_paths, manifest_path, regenerated)
        for name in sorted(expected_names):
            if (output / name).read_bytes() != (regenerated / name).read_bytes():
                raise ValueError(f"generated artifact changed: {name}")


def _validated_run_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    value = _strict_object(manifest, "manifest", _MANIFEST_KEYS)
    if value["schema_version"] != _MANIFEST_SCHEMA_V1:
        raise ValueError("unsupported run manifest schema")
    profile = _choice(value["profile"], "profile", {"quick", "full"})
    required_environments = _unique_strings(value["required_environments"], "required_environments")
    required_solvers = _unique_strings(value["required_solvers"], "required_solvers")
    required_case_ids = _unique_strings(value["required_case_ids"], "required_case_ids")
    first_cases = _subset_strings(value["first_qualified_case_ids"], "first_qualified_case_ids", required_case_ids)
    optimal_cases = _subset_strings(value["optimal_case_ids"], "optimal_case_ids", required_case_ids)
    throughput_cases = _subset_strings(value["throughput_probe_case_ids"], "throughput_probe_case_ids", required_case_ids)
    cold_cases = _subset_strings(value["cold_probe_case_ids"], "cold_probe_case_ids", required_case_ids)
    rebuild_cases = _subset_strings(value["rebuild_probe_case_ids"], "rebuild_probe_case_ids", required_case_ids)
    worker_counts = _unique_positive_ints(value["worker_counts"], "worker_counts")
    warmups = _nonnegative_integer(value["warmup_samples"], "warmup_samples")
    measured = _positive_integer(value["measured_samples"], "measured_samples")
    approved = _nonnegative_integer(value["approved_case_count"], "approved_case_count")
    if profile == "full" and (
        required_environments != ("macos", "linux")
        or set(required_solvers) != {"highs", "scip", "cp_sat"}
        or worker_counts != (1, 2, 4)
        or warmups != 5
        or measured != 30
        or not all((first_cases, optimal_cases, throughput_cases, cold_cases, rebuild_cases))
    ):
        raise ValueError("full manifest must freeze its matrix and non-empty phase/probe groups")
    if profile == "quick" and (required_environments != ("macos",) or worker_counts != (1,) or warmups != 0):
        raise ValueError("quick manifest must use macos, worker count 1, and no warmups")
    environments = _strict_named_objects(value["environments"], "environments", required_environments)
    for environment, evidence in environments.items():
        evidence = _strict_object(evidence, f"environments.{environment}", _ENVIRONMENT_KEYS)
        if not isinstance(evidence["available"], bool):
            raise ValueError("environment availability must be a bool")
        for field in ("build_id", "environment_id", "image_id"):
            _text(evidence[field], f"environments.{environment}.{field}")
        environments[environment] = evidence
    solvers = _strict_named_objects(value["solvers"], "solvers", required_solvers)
    for solver, evidence in solvers.items():
        evidence = _strict_object(evidence, f"solvers.{solver}", _SOLVER_KEYS)
        for field in ("commercial_key_required", "install_succeeded", "license_evidence_present", "open_source", "reuse_succeeded", "run_succeeded", "source_evidence_present", "whole_claim_certificate_bound"):
            if not isinstance(evidence[field], bool):
                raise ValueError(f"solvers.{solver}.{field} must be a bool")
        for field in ("installation_ns", "manual_interventions"):
            _nonnegative_integer(evidence[field], f"solvers.{solver}.{field}")
        if evidence["whole_claim_certificate_bound"]:
            raise ValueError("current certificate evidence is unbound and cannot gain proof credit")
        solvers[solver] = evidence
    _sha(value["corpus_manifest_sha256"], "corpus_manifest_sha256")
    _sha(value["license_manifest_sha256"], "license_manifest_sha256")
    return {
        **value,
        "approved_case_count": approved,
        "required_environments": required_environments,
        "required_solvers": required_solvers,
        "required_case_ids": required_case_ids,
        "first_qualified_case_ids": first_cases,
        "optimal_case_ids": optimal_cases,
        "throughput_probe_case_ids": throughput_cases,
        "cold_probe_case_ids": cold_cases,
        "rebuild_probe_case_ids": rebuild_cases,
        "worker_counts": worker_counts,
        "warmup_samples": warmups,
        "measured_samples": measured,
        "environments": environments,
        "solvers": solvers,
    }


def _validated_benchmark_record(record: object, manifest: Mapping[str, object]) -> dict[str, object]:
    value = _strict_object(record, "benchmark record", _RECORD_KEYS)
    if value["schema_version"] != _RECORD_SCHEMA_V1:
        raise ValueError("unsupported benchmark record schema")
    if value["profile"] != manifest["profile"]:
        raise ValueError("record profile does not match manifest")
    environment = _choice(value["environment"], "environment", set(manifest["required_environments"]))
    solver = _choice(value["solver_name"], "solver_name", set(manifest["required_solvers"]))
    case_id = _choice(value["case_id"], "case_id", set(manifest["required_case_ids"]))
    sample_kind = _choice(value["sample_kind"], "sample_kind", _SAMPLE_KINDS)
    run = _validated_solver_run_payload(value["solver_run"])
    for field in ("solver_name", "solver_version", "adapter_version", "worker_id"):
        if run[field] != value[field]:
            raise ValueError(f"solver_run {field} does not match record")
    environment_evidence = manifest["environments"][environment]
    if run["environment_id"] != environment_evidence.get("environment_id"):
        raise ValueError("solver_run environment_id does not match manifest")
    if value["image_id"] != environment_evidence.get("image_id"):
        raise ValueError("record image_id does not match manifest")
    if value["corpus_manifest_sha256"] != manifest["corpus_manifest_sha256"] or value["license_manifest_sha256"] != manifest["license_manifest_sha256"]:
        raise ValueError("record manifest fingerprint mismatch")
    for field in ("request_fingerprint", "problem_fingerprint", "corpus_manifest_sha256", "license_manifest_sha256", "semantic_fingerprint"):
        _sha(value[field], field)
    if not isinstance(value["git_sha"], str) or len(value["git_sha"]) != 40 or any(character not in "0123456789abcdef" for character in value["git_sha"]):
        raise ValueError("git_sha must be lowercase hexadecimal")
    for field in ("truth_method", "cpu", "architecture", "os_version", "container_id", "image_id", "python_version", "corpus_version"):
        _text(value[field], field)
    if value["protocol_version"] != BENCHMARK_PROTOCOL_V1:
        raise ValueError("unsupported record protocol")
    for field in ("sample_index", "worker_start_count", "worker_rebuild_count", "request_wall_ns", "peak_process_group_rss_bytes", "peak_aggregate_rss_bytes"):
        _nonnegative_integer(value[field], field)
    for field in ("worker_count", "completed_requests"):
        _positive_integer(value[field], field)
    _nonnegative_integer(value["memory_limit_bytes"], "memory_limit_bytes")
    for field in ("cleanup_proven", "check_hard_failure"):
        if not isinstance(value[field], bool):
            raise ValueError(f"{field} must be a bool")
    if value["check_failure_reason"] is not None:
        _choice(value["check_failure_reason"], "check_failure_reason", {item.value for item in CheckFailureReason})
    if value["check_hard_failure"] != (value["check_failure_reason"] is not None):
        raise ValueError("check_hard_failure and check_failure_reason must agree")
    if sample_kind == "throughput" and not value["request_wall_ns"]:
        raise ValueError("throughput request_wall_ns must be positive")
    if not run["peak_rss_bytes"] <= value["peak_process_group_rss_bytes"] <= value["peak_aggregate_rss_bytes"]:
        raise ValueError("solver/process-group/aggregate RSS must be ordered")
    decoded = {**value, "environment": environment, "solver_name": solver, "case_id": case_id, "sample_kind": sample_kind, "solver_run": run}
    if decoded["semantic_fingerprint"] != _semantic_fingerprint(decoded):
        raise ValueError("semantic_fingerprint does not match recomputed claim")
    return decoded


def _validated_solver_run_payload(payload: object) -> dict[str, object]:
    run = _strict_object(payload, "solver_run", _RUN_KEYS)
    if run["schema_version"] != BENCHMARK_PROTOCOL_V1:
        raise ValueError("unsupported solver_run protocol")
    for field in ("request_id", "solver_name", "solver_version", "adapter_version", "worker_id", "environment_id"):
        _text(run[field], field)
    _choice(run["solve_status"], "solve_status", {"UNKNOWN", "FEASIBLE", "INFEASIBLE"})
    _choice(run["proof_status"], "proof_status", {"UNKNOWN", "PROVEN"})
    _choice(run["business_status"], "business_status", {"UNKNOWN", "QUALIFIED_FEASIBLE", "NO_QUALIFIED_OPPORTUNITY", "NO_ARBITRAGE"})
    _choice(run["optimality_status"], "optimality_status", {"NOT_APPLICABLE", "NOT_PROVEN", "OPTIMAL"})
    _choice(run["classification"], "classification", {item.value for item in BenchmarkClassification})
    _choice(run["termination_reason"], "termination_reason", {item.value for item in TerminationReason})
    _objective_bounds_payload(run["objective_bounds"])
    if run["canonical_result"] is not None:
        canonical_result = result_from_payload(_mapping(run["canonical_result"], "canonical_result"))
        if run["classification"] != BenchmarkClassification.CHECKED.value:
            raise ValueError("only CHECKED solver runs may carry a canonical result")
        result_payload = canonical_payload(canonical_result)
        if any(run[field] != result_payload[field] for field in ("solve_status", "proof_status", "business_status", "optimality_status", "objective_bounds")):
            raise ValueError("CHECKED solver run axes must match its canonical result")
    elif run["classification"] == BenchmarkClassification.CHECKED.value:
        raise ValueError("CHECKED solver runs require a canonical result")
    if run["classification"] in {BenchmarkClassification.MEASUREMENT_ONLY.value, BenchmarkClassification.UNKNOWN.value} and run["proof_status"] != "UNKNOWN":
        raise ValueError("measurement-only and unknown runs require UNKNOWN proof_status")
    if run["classification"] == BenchmarkClassification.UNKNOWN.value and (run["solve_status"] != "UNKNOWN" or run["business_status"] != "UNKNOWN"):
        raise ValueError("UNKNOWN classification requires UNKNOWN solve/business status")
    _positive_or_zero(run["peak_rss_bytes"], "peak_rss_bytes")
    timings = _named_nonnegative_pairs(run["phase_timings_ns"], "phase_timings_ns")
    if set(timings) != _PHASE_NAMES:
        raise ValueError("phase_timings_ns must contain the exact benchmark phases")
    diagnostics = run["diagnostics"]
    if not isinstance(diagnostics, list) or any(not isinstance(item, list) or len(item) != 2 or not all(isinstance(part, str) for part in item) for item in diagnostics):
        raise ValueError("diagnostics must be string pairs")
    if len({item[0] for item in diagnostics}) != len(diagnostics):
        raise ValueError("diagnostics names must be unique")
    if run["evidence"] is not None:
        _validated_evidence_payload(run["evidence"])
    if run["classification"] == BenchmarkClassification.CERTIFICATE_CHECKED.value:
        raise ValueError("current certificate evidence is unbound and cannot be CERTIFICATE_CHECKED")
    return {**run, "phase_timings_ns": timings}


def _validated_evidence_payload(payload: object) -> None:
    value = _strict_object(
        payload,
        "solver evidence",
        {"native_status", "candidate", "objective_bounds", "worst_scenario", "payout_lower_bound_units", "cost_upper_bound_units", "guaranteed_profit_units", "conservative_capital_release_at", "fixed_portfolio_closed", "global_search_closed", "master_rounds", "adversary_rounds", "cuts", "certificate"},
    )
    _text(value["native_status"], "native_status")
    _objective_bounds_payload(value["objective_bounds"])
    for field in ("payout_lower_bound_units", "cost_upper_bound_units", "guaranteed_profit_units"):
        if value[field] is not None:
            _integer(value[field], field)
    if value["conservative_capital_release_at"] is not None:
        _utc_z(value["conservative_capital_release_at"], "conservative_capital_release_at")
    for field in ("fixed_portfolio_closed", "global_search_closed"):
        if not isinstance(value[field], bool):
            raise ValueError(f"{field} must be a bool")
    for field in ("master_rounds", "adversary_rounds"):
        _nonnegative_integer(value[field], field)
    if not isinstance(value["cuts"], list):
        raise ValueError("cuts must be an array")
    for cut in value["cuts"]:
        cut_value = _strict_object(cut, "cut", {"cut_id", "scenario", "payout_per_lot"})
        _text(cut_value["cut_id"], "cut_id")
        scenario = _strict_object(cut_value["scenario"], "cut.scenario", {"atoms"})
        _selected_atoms(scenario["atoms"], "cut.scenario.atoms")
        if not isinstance(cut_value["payout_per_lot"], list):
            raise ValueError("cut payout_per_lot must be an array")
        for payout in cut_value["payout_per_lot"]:
            payout_value = _strict_object(payout, "cut payout", {"action_id", "payout_lower_bound_per_lot_units"})
            _text(payout_value["action_id"], "action_id")
            _integer(payout_value["payout_lower_bound_per_lot_units"], "payout_lower_bound_per_lot_units")
    if value["candidate"] is not None:
        candidate = _strict_object(value["candidate"], "candidate", {"quantities", "claimed_guaranteed_profit_units"})
        _integer(candidate["claimed_guaranteed_profit_units"], "claimed_guaranteed_profit_units")
        if not isinstance(candidate["quantities"], list):
            raise ValueError("candidate quantities must be an array")
        for quantity in candidate["quantities"]:
            quantity_value = _strict_object(quantity, "quantity", {"action_id", "quantity_lots"})
            _text(quantity_value["action_id"], "action_id")
            _nonnegative_integer(quantity_value["quantity_lots"], "quantity_lots")
    if value["worst_scenario"] is not None:
        scenario = _strict_object(value["worst_scenario"], "worst_scenario", {"atoms"})
        _selected_atoms(scenario["atoms"], "worst_scenario.atoms")
    if value["certificate"] is not None:
        certificate = _strict_object(value["certificate"], "certificate", {"certificate_sha256", "certificate_size_bytes", "completed_certificate_sha256", "completed_certificate_size_bytes", "checker_name", "checker_version", "checker_exit_code", "checker_succeeded", "generation_ns", "completion_ns", "check_ns"})
        _sha(certificate["certificate_sha256"], "certificate_sha256")
        if certificate["completed_certificate_sha256"] is not None:
            _sha(certificate["completed_certificate_sha256"], "completed_certificate_sha256")
        for field in ("certificate_size_bytes", "completed_certificate_size_bytes"):
            if certificate[field] is not None:
                _nonnegative_integer(certificate[field], field)
        for field in ("checker_name", "checker_version"):
            if not isinstance(certificate[field], str):
                raise ValueError(f"{field} must be text")
        _integer(certificate["checker_exit_code"], "checker_exit_code")
        if not isinstance(certificate["checker_succeeded"], bool):
            raise ValueError("checker_succeeded must be a bool")
        for field in ("generation_ns", "completion_ns", "check_ns"):
            _nonnegative_integer(certificate[field], field)
        if (certificate["completed_certificate_sha256"] is None) != (certificate["completed_certificate_size_bytes"] is None):
            raise ValueError("completed certificate hash and size must both be present or absent")
        if certificate["checker_succeeded"] and (certificate["checker_exit_code"] != 0 or not certificate["checker_name"].strip() or not certificate["checker_version"].strip()):
            raise ValueError("successful certificate checks require exit code zero and checker identity")


def _require_sample_matrix(records: tuple[dict[str, object], ...], manifest: Mapping[str, object]) -> None:
    expected: set[tuple[object, ...]] = set()
    measured = manifest["measured_samples"]
    warmups = manifest["warmup_samples"]
    for environment in sorted(manifest["required_environments"]):
        if not manifest["environments"][environment]["available"]:
            continue
        for solver in manifest["required_solvers"]:
            for case_id in manifest["required_case_ids"]:
                expected.update((environment, solver, case_id, "warmup", 1, index) for index in range(warmups))
                expected.update((environment, solver, case_id, "warm", 1, index) for index in range(measured))
            for case_id in manifest["throughput_probe_case_ids"]:
                for count in manifest["worker_counts"]:
                    expected.update((environment, solver, case_id, "throughput", count, index) for index in range(measured))
            for kind, case_ids in (("cold", manifest["cold_probe_case_ids"]), ("rebuild", manifest["rebuild_probe_case_ids"])):
                for case_id in case_ids:
                    expected.update((environment, solver, case_id, kind, 1, index) for index in range(measured))
    actual = [(record["environment"], record["solver_name"], record["case_id"], record["sample_kind"], record["worker_count"], record["sample_index"]) for record in records]
    if len(actual) != len(set(actual)):
        raise ValueError("duplicate structural sample")
    missing = expected - set(actual)
    extra = set(actual) - expected
    if missing:
        raise ValueError(f"missing structural sample: {sorted(missing)[0]}")
    if extra:
        raise ValueError(f"unexpected structural sample: {sorted(extra)[0]}")


def _hard_gate_failures(
    records: tuple[dict[str, object], ...],
    manifest: Mapping[str, object],
    solver: str,
) -> list[str]:
    failures = set()
    if any(record["check_hard_failure"] for record in records):
        failures.add("CHECK_HARD_FAILURE")
    if any(not record["cleanup_proven"] for record in records):
        failures.add("CLEANUP_UNPROVEN")
    if any(
        record["memory_limit_bytes"] == 0
        for record in records
    ):
        failures.add("MEMORY_LIMIT_UNBOUNDED")
    if any(
        record["peak_process_group_rss_bytes"] > record["memory_limit_bytes"]
        or record["peak_aggregate_rss_bytes"] > record["memory_limit_bytes"]
        for record in records
    ):
        failures.add("MEMORY_LIMIT_EXCEEDED")
    if any(
        record["solver_run"]["termination_reason"] != TerminationReason.COMPLETED.value
        and (
            record["solver_run"]["solve_status"] != "UNKNOWN"
            or record["solver_run"]["business_status"] != "UNKNOWN"
            or record["solver_run"]["proof_status"] != "UNKNOWN"
        )
        for record in records
    ):
        failures.add("UNSAFE_FAILURE_MAPPING")
    semantic_groups: dict[tuple[str, str], set[str]] = {}
    for record in records:
        semantic_groups.setdefault((record["environment"], record["case_id"]), set()).add(
            _semantic_fingerprint(record)
        )
    if any(len(values) != 1 for values in semantic_groups.values()):
        failures.add("SEMANTIC_NONDETERMINISM")
    solver_evidence = manifest["solvers"][solver]
    if not all(solver_evidence[field] for field in ("install_succeeded", "run_succeeded", "reuse_succeeded")):
        failures.add("ENVIRONMENT_EVIDENCE_FAILED")
    if (
        not all(solver_evidence[field] for field in ("open_source", "license_evidence_present", "source_evidence_present"))
        or solver_evidence["commercial_key_required"]
    ):
        failures.add("LICENSE_EVIDENCE_FAILED")
    return sorted(failures)


def _semantic_fingerprint(record: Mapping[str, object]) -> str:
    run = record["solver_run"]
    evidence = run["evidence"]
    payload = {
        "business_status": run["business_status"],
        "canonical_result": run["canonical_result"],
        "check_failure_reason": record["check_failure_reason"],
        "check_hard_failure": record["check_hard_failure"],
        "classification": run["classification"],
        "evidence": None
        if evidence is None
        else {
            "adversary_rounds": evidence["adversary_rounds"],
            "candidate": evidence["candidate"],
            "conservative_capital_release_at": evidence["conservative_capital_release_at"],
            "cost_upper_bound_units": evidence["cost_upper_bound_units"],
            "cuts": evidence["cuts"],
            "fixed_portfolio_closed": evidence["fixed_portfolio_closed"],
            "global_search_closed": evidence["global_search_closed"],
            "guaranteed_profit_units": evidence["guaranteed_profit_units"],
            "master_rounds": evidence["master_rounds"],
            "native_status": evidence["native_status"],
            "objective_bounds": evidence["objective_bounds"],
            "payout_lower_bound_units": evidence["payout_lower_bound_units"],
            "worst_scenario": evidence["worst_scenario"],
        },
        "objective_bounds": run["objective_bounds"],
        "optimality_status": run["optimality_status"],
        "proof_status": run["proof_status"],
        "solve_status": run["solve_status"],
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"sha256:{digest}"


def _aggregate_solver_metrics(records: tuple[dict[str, object], ...], manifest: Mapping[str, object], solver: str) -> dict[str, object]:
    metrics: dict[str, object] = {}
    warm = []
    for environment in sorted(manifest["required_environments"]):
        if not manifest["environments"][environment]["available"]:
            continue
        for case_id in manifest["required_case_ids"]:
            cell = [record for record in records if record["solver_name"] == solver and record["environment"] == environment and record["case_id"] == case_id and record["sample_kind"] == "warm"]
            result = {
                "adversary_rounds": _summary(item["solver_run"]["evidence"]["adversary_rounds"] if item["solver_run"]["evidence"] is not None else 0 for item in cell),
                "case_id": case_id,
                "cut_count": _summary(len(item["solver_run"]["evidence"]["cuts"]) if item["solver_run"]["evidence"] is not None else 0 for item in cell),
                "environment": environment,
                "master_rounds": _summary(item["solver_run"]["evidence"]["master_rounds"] if item["solver_run"]["evidence"] is not None else 0 for item in cell),
                "objective_gap_quality": _gap_quality(cell),
                "peak_aggregate_rss_bytes": _summary(item["peak_aggregate_rss_bytes"] for item in cell),
                "peak_process_group_rss_bytes": _summary(item["peak_process_group_rss_bytes"] for item in cell),
                "phase_timings_ns": {
                    phase: _summary(item["solver_run"]["phase_timings_ns"][phase] for item in cell)
                    for phase in ("backend", "certificate_check", "certificate_completion", "certificate_generation", "independent_check", "serialization")
                },
                "request_wall_ns": _summary(item["request_wall_ns"] for item in cell),
            }
            if case_id in manifest["first_qualified_case_ids"]:
                result["first_qualified_ns"] = _summary(item["solver_run"]["phase_timings_ns"]["first_qualified"] for item in cell)
            if case_id in manifest["optimal_case_ids"]:
                result["optimal_ns"] = _summary(item["solver_run"]["phase_timings_ns"]["optimal"] for item in cell)
            warm.append(result)
    metrics["warm"] = warm
    throughput = []
    for environment in manifest["required_environments"]:
        if not manifest["environments"][environment]["available"]:
            continue
        for case_id in manifest["throughput_probe_case_ids"]:
            for worker_count in manifest["worker_counts"]:
                cell = [record for record in records if record["solver_name"] == solver and record["environment"] == environment and record["case_id"] == case_id and record["sample_kind"] == "throughput" and record["worker_count"] == worker_count]
                throughput.append(
                    {
                        "case_id": case_id,
                        "environment": environment,
                        "worker_count": worker_count,
                        "requests_per_second": _throughput_summary(Fraction(item["completed_requests"] * 1_000_000_000, item["request_wall_ns"]) for item in cell),
                    }
                )
    metrics["throughput"] = throughput
    for kind in ("cold", "rebuild"):
        cells = []
        case_ids = manifest[f"{kind}_probe_case_ids"]
        for environment in sorted(manifest["required_environments"]):
            if not manifest["environments"][environment]["available"]:
                continue
            for case_id in case_ids:
                selected = [record for record in records if record["solver_name"] == solver and record["environment"] == environment and record["case_id"] == case_id and record["sample_kind"] == kind]
                cells.append({"case_id": case_id, "environment": environment, f"{kind}_ns": _summary(item["request_wall_ns"] for item in selected)})
        metrics[kind] = cells
    return metrics


def _gap_quality(records: list[dict[str, object]]) -> dict[str, object]:
    bounds = [record["solver_run"]["objective_bounds"] for record in records]
    gaps = [bound["gap_units"] for bound in bounds if bound["gap_units"] is not None]
    return {
        "closed_samples": sum(bound["closed"] for bound in bounds),
        "known_gap_samples": len(gaps),
        "unknown_gap_samples": sum(bound["gap_units"] is None for bound in bounds),
        "gap_units": None if not gaps else _summary(gaps),
    }


def _benchmark_decision(solvers: Mapping[str, object], manifest: Mapping[str, object]) -> dict[str, object]:
    empty = {"selected_solver": None}
    if manifest["approved_case_count"] == 0:
        return {"status": "BLOCKED_REAL_CORPUS_EMPTY", "reason": "REAL_CORPUS_EMPTY", **empty}
    if any(not manifest["environments"][environment]["available"] for environment in manifest["required_environments"]):
        return {"status": "BLOCKED_MISSING_ENVIRONMENT", "reason": "MANDATORY_ENVIRONMENT_UNAVAILABLE", **empty}
    if manifest["profile"] == "quick":
        return {"status": "BLOCKED_MISSING_ENVIRONMENT", "reason": "QUICK_PROFILE_NOT_SELECTION_EVIDENCE", **empty}
    active = [solver for solver in sorted(solvers) if not solvers[solver]["hard_gate_failures"]]
    if not active:
        return {"status": "NO_SURVIVOR", "reason": "ALL_CANDIDATES_HARD_ELIMINATED", **empty}

    stages = (
        ("FIRST_QUALIFIED_P95", lambda solver: _cell_vector(solvers[solver]["metrics"]["warm"], "first_qualified_ns", "p95", manifest["first_qualified_case_ids"])),
        ("FIRST_QUALIFIED_WORST", lambda solver: _cell_vector(solvers[solver]["metrics"]["warm"], "first_qualified_ns", "worst", manifest["first_qualified_case_ids"])),
        ("PEAK_RSS_P95", lambda solver: _cell_vector(solvers[solver]["metrics"]["warm"], "peak_aggregate_rss_bytes", "p95")),
        ("PEAK_RSS_WORST", lambda solver: _cell_vector(solvers[solver]["metrics"]["warm"], "peak_aggregate_rss_bytes", "worst")),
        ("THROUGHPUT_WORST", lambda solver: tuple(-value for value in _cell_vector(solvers[solver]["metrics"]["throughput"], "requests_per_second", "worst", descending=False))),
        ("REBUILD_P95", lambda solver: _cell_vector(solvers[solver]["metrics"]["rebuild"], "rebuild_ns", "p95")),
        ("REBUILD_WORST", lambda solver: _cell_vector(solvers[solver]["metrics"]["rebuild"], "rebuild_ns", "worst")),
        ("MANUAL_INTERVENTIONS", lambda solver: (Fraction(manifest["solvers"][solver]["manual_interventions"]),)),
        ("INSTALLATION_DURATION", lambda solver: (Fraction(manifest["solvers"][solver]["installation_ns"]),)),
        ("COLD_START_P95", lambda solver: _cell_vector(solvers[solver]["metrics"]["cold"], "cold_ns", "p95")),
        ("COLD_START_WORST", lambda solver: _cell_vector(solvers[solver]["metrics"]["cold"], "cold_ns", "worst")),
        ("TIME_TO_OPTIMAL_P95", lambda solver: _cell_vector(solvers[solver]["metrics"]["warm"], "optimal_ns", "p95", manifest["optimal_case_ids"])),
        ("TIME_TO_OPTIMAL_WORST", lambda solver: _cell_vector(solvers[solver]["metrics"]["warm"], "optimal_ns", "worst", manifest["optimal_case_ids"])),
        ("OBJECTIVE_GAP_QUALITY", lambda solver: _gap_vector(solvers[solver]["metrics"]["warm"], manifest["optimal_case_ids"])),
        ("BOUND_CERTIFICATE_CAPABILITY", lambda solver: (Fraction(not manifest["solvers"][solver]["whole_claim_certificate_bound"]),)),
        ("CERTIFICATE_GENERATION_P95", lambda solver: _certificate_vector(solvers[solver], manifest, "certificate_generation", "p95", solver)),
        ("CERTIFICATE_GENERATION_WORST", lambda solver: _certificate_vector(solvers[solver], manifest, "certificate_generation", "worst", solver)),
        ("CERTIFICATE_COMPLETION_P95", lambda solver: _certificate_vector(solvers[solver], manifest, "certificate_completion", "p95", solver)),
        ("CERTIFICATE_COMPLETION_WORST", lambda solver: _certificate_vector(solvers[solver], manifest, "certificate_completion", "worst", solver)),
        ("CERTIFICATE_CHECK_P95", lambda solver: _certificate_vector(solvers[solver], manifest, "certificate_check", "p95", solver)),
        ("CERTIFICATE_CHECK_WORST", lambda solver: _certificate_vector(solvers[solver], manifest, "certificate_check", "worst", solver)),
    )
    for stage, key in stages:
        scores = {solver: key(solver) for solver in active}
        best = min(scores.values())
        active = [solver for solver in active if scores[solver] == best]
        if len(active) == 1:
            selected = active[0]
            return {
                "status": "SELECTED",
                "reason": "DECISIVE_STAGE",
                "selected_solver": selected,
                "decisive_stage": stage,
                "contributing_cells": _stage_cell_labels(solvers[selected]["metrics"], stage, manifest),
            }
    return {"status": "NO_DECISIVE_WINNER", "reason": "COMPLETE_TIE", **empty}


def _cell_vector(
    cells: list[dict[str, object]],
    metric: str,
    statistic: str,
    case_ids: tuple[str, ...] | None = None,
    *,
    descending: bool = True,
) -> tuple[Fraction, ...]:
    values = [
        _fraction(cell[metric][statistic])
        for cell in cells
        if metric in cell and (case_ids is None or cell["case_id"] in case_ids)
    ]
    return tuple(sorted(values, reverse=descending))


def _gap_vector(cells: list[dict[str, object]], case_ids: tuple[str, ...]) -> tuple[Fraction, ...]:
    values = []
    for cell in cells:
        if cell["case_id"] not in case_ids:
            continue
        quality = cell["objective_gap_quality"]
        total = quality["known_gap_samples"] + quality["unknown_gap_samples"]
        rank = 0 if quality["closed_samples"] == total else 1 if quality["known_gap_samples"] else 2
        gap = 0 if quality["gap_units"] is None else _fraction(quality["gap_units"]["worst"])
        values.extend((Fraction(rank), gap))
    return tuple(values)


def _certificate_vector(solver: Mapping[str, object], manifest: Mapping[str, object], phase: str, statistic: str, solver_name: str) -> tuple[Fraction, ...]:
    if not manifest["solvers"][solver_name]["whole_claim_certificate_bound"]:
        return ()
    return _cell_vector(
        [{**cell, phase: cell["phase_timings_ns"][phase]} for cell in solver["metrics"]["warm"]],
        phase,
        statistic,
    )


def _stage_cell_labels(metrics: Mapping[str, object], stage: str, manifest: Mapping[str, object]) -> list[str]:
    if stage.startswith("FIRST_QUALIFIED"):
        cells = [cell for cell in metrics["warm"] if cell["case_id"] in manifest["first_qualified_case_ids"]]
    elif stage.startswith("TIME_TO_OPTIMAL") or stage == "OBJECTIVE_GAP_QUALITY":
        cells = [cell for cell in metrics["warm"] if cell["case_id"] in manifest["optimal_case_ids"]]
    elif stage.startswith("THROUGHPUT"):
        cells = metrics["throughput"]
    elif stage.startswith("REBUILD"):
        cells = metrics["rebuild"]
    elif stage.startswith("COLD"):
        cells = metrics["cold"]
    else:
        cells = metrics["warm"] if "RSS" in stage or "CERTIFICATE" in stage else []
    return sorted(f"{cell['environment']}/{cell['case_id']}" + (f"/workers-{cell['worker_count']}" if "worker_count" in cell else "") for cell in cells)


def _summary(values: object) -> dict[str, object]:
    ordered = list(values)
    return {
        "p50": _exact_json(statistics.median(ordered)),
        "p95": _exact_json(ordered[0] if len(ordered) == 1 else statistics.quantiles(ordered, n=100, method="inclusive")[94]),
        "worst": _exact_json(max(ordered)),
    }


def _throughput_summary(values: object) -> dict[str, object]:
    ordered = list(values)
    return {
        "p50": _exact_json(statistics.median(ordered)),
        "p95": _exact_json(ordered[0] if len(ordered) == 1 else statistics.quantiles(ordered, n=100, method="inclusive")[94]),
        "worst": _exact_json(max(ordered)),
    }


def _exact_json(value: int | Fraction) -> int | dict[str, int]:
    rational = value if isinstance(value, Fraction) else Fraction(value)
    return rational.numerator if rational.denominator == 1 else {"numerator": rational.numerator, "denominator": rational.denominator}


def _fraction(value: object) -> Fraction:
    if isinstance(value, int) and not isinstance(value, bool):
        return Fraction(value)
    rational = _strict_object(value, "rational", {"numerator", "denominator"})
    return Fraction(_integer(rational["numerator"], "numerator"), _positive_integer(rational["denominator"], "denominator"))


def _strict_object(value: object, name: str, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value) or set(value) != keys:
        raise ValueError(f"{name} must contain exactly {sorted(keys)}")
    return dict(value)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _selected_atoms(value: object, name: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    for atom in value:
        atom_value = _strict_object(atom, name, {"market_contract_id", "atom_id"})
        _text(atom_value["market_contract_id"], "market_contract_id")
        _text(atom_value["atom_id"], "atom_id")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _choice(value: object, name: str, choices: set[str]) -> str:
    text = _text(value, name)
    if text not in choices:
        raise ValueError(f"invalid {name}")
    return text


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _positive_integer(value: object, name: str) -> int:
    number = _integer(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _positive_or_zero(value: object, name: str) -> int:
    return _nonnegative_integer(value, name)


def _nonnegative_integer(value: object, name: str) -> int:
    number = _integer(value, name)
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 71 or not text.startswith("sha256:") or any(character not in "0123456789abcdef" for character in text[7:]):
        raise ValueError(f"{name} must be a sha256 fingerprint")
    return text


def _unique_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    items = tuple(_text(item, name) for item in value)
    if len(items) != len(set(items)):
        raise ValueError(f"{name} must not contain duplicates")
    return items


def _subset_strings(value: object, name: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
    items = _unique_strings(value, name)
    if not set(items) <= set(allowed):
        raise ValueError(f"{name} must be a subset of required_case_ids")
    return items


def _unique_positive_ints(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    items = tuple(_positive_integer(item, name) for item in value)
    if len(items) != len(set(items)):
        raise ValueError(f"{name} must not contain duplicates")
    return items


def _strict_named_objects(value: object, name: str, required: tuple[str, ...]) -> dict[str, dict[str, object]]:
    mapping = _mapping(value, name)
    if set(mapping) != set(required):
        raise ValueError(f"{name} must contain exactly the required names")
    return {key: dict(_mapping(item, f"{name}.{key}")) for key, item in mapping.items()}


def _objective_bounds_payload(value: object) -> None:
    bounds = _strict_object(value, "objective_bounds", {"lower_bound_units", "upper_bound_units", "gap_units", "closed"})
    for field in ("lower_bound_units", "upper_bound_units", "gap_units"):
        if bounds[field] is not None:
            _integer(bounds[field], field)
    if not isinstance(bounds["closed"], bool):
        raise ValueError("objective_bounds.closed must be a bool")


def _named_nonnegative_pairs(value: object, name: str) -> dict[str, int]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    result = {}
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(f"{name} must contain pairs")
        key = _text(item[0], name)
        if key in result:
            raise ValueError(f"{name} contains duplicate names")
        result[key] = _nonnegative_integer(item[1], f"{name}.{key}")
    return result


def _strict_json(raw: bytes, name: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{name}: duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError(f"{name}: non-standard JSON constant: {value}")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{name}: invalid JSON") from error


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _markdown_report(summary: Mapping[str, object]) -> str:
    decision = summary["decision"]
    lines = [
        "# Solver benchmark report",
        "",
        f"Decision: `{decision['status']}`",
        f"Reason: `{decision['reason']}`",
        f"Selected solver: `{decision['selected_solver'] or 'none'}`",
        "",
        "Current proof policy: exact Oracle checking. Unbound VIPR evidence is benchmark-only.",
        "",
        "## Candidates",
        "",
    ]
    for solver, evidence in sorted(summary["solvers"].items()):
        failures = ", ".join(evidence["hard_gate_failures"]) or "none"
        lines.extend((f"### {solver}", "", f"Hard-gate failures: {failures}", "", "| metric | cell | p95 | worst |", "| --- | --- | ---: | ---: |"))
        for cell in evidence["metrics"]["warm"]:
            label = f"{cell['environment']}/{cell['case_id']}"
            for metric in ("request_wall_ns", "first_qualified_ns", "optimal_ns", "peak_aggregate_rss_bytes"):
                if metric in cell:
                    lines.append(f"| {metric} | {label} | {_display_exact(cell[metric]['p95'])} | {_display_exact(cell[metric]['worst'])} |")
        for cell in evidence["metrics"]["throughput"]:
            metric = cell["requests_per_second"]
            label = f"{cell['environment']}/{cell['case_id']}/workers-{cell['worker_count']}"
            lines.append(f"| requests_per_second | {label} | {_display_exact(metric['p95'])} | {_display_exact(metric['worst'])} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _display_exact(value: object) -> str:
    return str(value) if isinstance(value, int) else f"{value['numerator']}/{value['denominator']}"


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
    if evidence.conservative_capital_release_at != evaluation.conservative_capital_release_at:
        return _check_failure(CheckFailureReason.WRONG_RELEASE)
    if not _fixed_claim_matches(problem, evidence, evaluation):
        return _check_failure(CheckFailureReason.CLAIM_MISMATCH)
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
