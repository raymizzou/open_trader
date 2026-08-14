from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
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
    ObjectiveBounds,
    OptimalityStatus,
    ProofStatus,
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
    SolveStatus,
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
from open_trader.prediction_solver import (
    BENCHMARK_PROTOCOL_V1,
    INT64_MAX,
    INT64_MIN,
    BenchmarkClassification,
    BenchmarkLimits,
    SolverEvidence,
    SolverRun,
    TerminationReason,
    solver_evidence_from_payload,
)
from open_trader.prediction_solver_worker import (
    WORKER_PHASE_NAMES,
    WORKER_VERSION,
    WorkerHarness,
    WorkerOutcome,
    WorkerRequest,
)


_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_FIXTURE = _ROOT / "tests" / "fixtures" / "prediction_n_leg_v1.json"
_CANONICAL_FIXTURE_SHA256 = "a4680fb2c66dedac9e85db9cd06d0872882ca69b09fba6d9f338d0b97243ecc7"
_BENCHMARK_ROOT = _ROOT / "benchmarks" / "prediction_solver"
_SYNTHETIC_CORPUS = _BENCHMARK_ROOT / "corpus" / "synthetic_v1.json"
_APPROVED_CORPUS = _BENCHMARK_ROOT / "corpus" / "approved_v1.json"
_LICENSE_MANIFEST = _BENCHMARK_ROOT / "licenses.json"
_BENCHMARK_ENVS = _ROOT / ".benchmark-envs"
_QUICK_OUTPUT = _ROOT / "reports" / "prediction_solver" / "quick"
_FINAL_RESULTS = _BENCHMARK_ROOT / "results" / "issue49"
QUICK_SOFT_TIME_LIMIT_MS = 5_000
QUICK_HARD_TIME_LIMIT_MS = 20_000
QUICK_MEMORY_LIMIT_BYTES = 1 << 40
QUICK_MAX_CONSTRAINT_GENERATION_ROUNDS = 64
_SOLVERS = ("highs", "scip", "cp_sat")
_SOLVER_VERSIONS = {"highs": "1.15.1", "scip": "10.0.2", "cp_sat": "9.15.6755"}
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
    "os_version", "container_id", "build_id", "image_id", "python_version", "solver_name",
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
    "license_manifest_sha256", "environments", "solvers", "cases", "memory_limit_bytes",
    "soft_time_limit_ms", "hard_time_limit_ms", "max_constraint_generation_rounds",
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
_ENVIRONMENT_KEYS = {"available", "environment_id", "git_sha", "cpu", "architecture", "os_version"}
_SOLVER_KEYS = {"environments", "manual_interventions", "whole_claim_certificate_bound"}
_SOLVER_ENVIRONMENT_KEYS = {
    "adapter_version", "build_id", "commercial_key_required", "image_id",
    "install_succeeded", "installation_ns", "license_evidence_present", "open_source",
    "python_version", "reuse_succeeded", "run_succeeded", "solver_version",
    "source_evidence_present",
}
_CASE_KEYS = {"model_dimensions", "oracle_limits", "problem_fingerprint", "request_fingerprint", "request_id"}
_DIMENSION_KEYS = {
    "action_count", "contract_count", "cost_slice_count", "joint_state_count",
    "quantity_domain_size", "relationship_count", "terminal_atom_count",
}
_ORACLE_LIMIT_KEYS = {"max_joint_states", "max_quantity_vectors", "max_support_rechecks"}


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


_FATAL_TERMINATIONS = {
    TerminationReason.MEMORY_LIMIT.value,
    TerminationReason.CRASH.value,
    TerminationReason.INVALID_OUTPUT.value,
    TerminationReason.PROTOCOL_MISMATCH.value,
}


def _hard_elimination_record(record: Mapping[str, object]) -> bool:
    return bool(record["check_hard_failure"]) or record["solver_run"]["termination_reason"] in _FATAL_TERMINATIONS


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
            "operational_evidence": manifest_value["solvers"][solver],
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
            "hard_time_limit_ms": manifest_value["hard_time_limit_ms"],
            "memory_limit_bytes": manifest_value["memory_limit_bytes"],
            "max_constraint_generation_rounds": manifest_value["max_constraint_generation_rounds"],
            "oracle_limits": {
                case_id: manifest_value["cases"][case_id]["oracle_limits"]
                for case_id in sorted(manifest_value["required_case_ids"])
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
            "soft_time_limit_ms": manifest_value["soft_time_limit_ms"],
        },
        "measured_dimensions": {
            "case_ids": list(manifest_value["required_case_ids"]),
            "cold_probe_case_ids": list(manifest_value["cold_probe_case_ids"]),
            "first_qualified_case_ids": list(manifest_value["first_qualified_case_ids"]),
            "measured_samples": manifest_value["measured_samples"],
            "model_dimensions": {
                case_id: manifest_value["cases"][case_id]["model_dimensions"]
                for case_id in sorted(manifest_value["required_case_ids"])
            },
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
    sample_counts = _sample_counts(tuple(_validated_benchmark_record(record, manifest_value) for record in records), manifest_value)
    report = _markdown_report(summary, sample_counts)
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
    artifact_names = {"summary.json", "production_envelope.json", "report.md"}
    input_paths = jsonl_paths if isinstance(jsonl_paths, list | tuple) else ()
    expected_names = artifact_names | {
        path.name
        for path in (Path(manifest_path), *(Path(item) for item in input_paths))
        if path.parent == output
    }
    actual_names = {path.name for path in output.iterdir()} if output.is_dir() else set()
    if actual_names != expected_names:
        raise ValueError(f"generated artifact names changed: missing={sorted(expected_names - actual_names)} extra={sorted(actual_names - expected_names)}")
    with tempfile.TemporaryDirectory() as directory:
        regenerated = Path(directory)
        generate_benchmark_report(jsonl_paths, manifest_path, regenerated)
        for name in sorted(artifact_names):
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
    memory_limit = _nonnegative_integer(value["memory_limit_bytes"], "memory_limit_bytes")
    round_limit = _positive_integer(value["max_constraint_generation_rounds"], "max_constraint_generation_rounds")
    soft_limit = _positive_integer(value["soft_time_limit_ms"], "soft_time_limit_ms")
    hard_limit = _positive_integer(value["hard_time_limit_ms"], "hard_time_limit_ms")
    if hard_limit < soft_limit:
        raise ValueError("hard_time_limit_ms must be at least soft_time_limit_ms")
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
        for field in ("environment_id", "cpu", "architecture", "os_version"):
            _text(evidence[field], f"environments.{environment}.{field}")
        _git_sha(evidence["git_sha"], f"environments.{environment}.git_sha")
        environments[environment] = evidence
    cases = _strict_named_objects(value["cases"], "cases", required_case_ids)
    for case_id, evidence in cases.items():
        evidence = _strict_object(evidence, f"cases.{case_id}", _CASE_KEYS)
        for field in ("problem_fingerprint", "request_fingerprint"):
            _sha(evidence[field], f"cases.{case_id}.{field}")
        _text(evidence["request_id"], f"cases.{case_id}.request_id")
        dimensions = _strict_object(evidence["model_dimensions"], f"cases.{case_id}.model_dimensions", _DIMENSION_KEYS)
        for field in _DIMENSION_KEYS:
            dimensions[field] = _nonnegative_integer(dimensions[field], f"cases.{case_id}.model_dimensions.{field}")
        oracle_limits = _strict_object(evidence["oracle_limits"], f"cases.{case_id}.oracle_limits", _ORACLE_LIMIT_KEYS)
        for field in _ORACLE_LIMIT_KEYS:
            oracle_limits[field] = _positive_integer(oracle_limits[field], f"cases.{case_id}.oracle_limits.{field}")
        cases[case_id] = {**evidence, "model_dimensions": dimensions, "oracle_limits": oracle_limits}
    solvers = _strict_named_objects(value["solvers"], "solvers", required_solvers)
    for solver, evidence in solvers.items():
        evidence = _strict_object(evidence, f"solvers.{solver}", _SOLVER_KEYS)
        if not isinstance(evidence["whole_claim_certificate_bound"], bool):
            raise ValueError(f"solvers.{solver}.whole_claim_certificate_bound must be a bool")
        _nonnegative_integer(evidence["manual_interventions"], f"solvers.{solver}.manual_interventions")
        solver_environments = _strict_named_objects(evidence["environments"], f"solvers.{solver}.environments", required_environments)
        for environment, environment_evidence in solver_environments.items():
            environment_evidence = _strict_object(environment_evidence, f"solvers.{solver}.environments.{environment}", _SOLVER_ENVIRONMENT_KEYS)
            for field in (
                "commercial_key_required", "install_succeeded", "license_evidence_present",
                "open_source", "reuse_succeeded", "run_succeeded", "source_evidence_present",
            ):
                if not isinstance(environment_evidence[field], bool):
                    raise ValueError(f"solvers.{solver}.environments.{environment}.{field} must be a bool")
            for field in ("adapter_version", "build_id", "image_id", "python_version", "solver_version"):
                _text(environment_evidence[field], f"solvers.{solver}.environments.{environment}.{field}")
            _nonnegative_integer(environment_evidence["installation_ns"], f"solvers.{solver}.environments.{environment}.installation_ns")
            solver_environments[environment] = environment_evidence
        if evidence["whole_claim_certificate_bound"]:
            raise ValueError("current certificate evidence is unbound and cannot gain proof credit")
        solvers[solver] = {**evidence, "environments": solver_environments}
    _sha(value["corpus_manifest_sha256"], "corpus_manifest_sha256")
    _sha(value["license_manifest_sha256"], "license_manifest_sha256")
    return {
        **value,
        "approved_case_count": approved,
        "cases": cases,
        "hard_time_limit_ms": hard_limit,
        "max_constraint_generation_rounds": round_limit,
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
        "memory_limit_bytes": memory_limit,
        "soft_time_limit_ms": soft_limit,
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
    if run["request_id"] != manifest["cases"][case_id]["request_id"]:
        raise ValueError("solver_run request_id does not match manifest case")
    for field in ("solver_name", "solver_version", "adapter_version", "worker_id"):
        if run[field] != value[field]:
            raise ValueError(f"solver_run {field} does not match record")
    environment_evidence = manifest["environments"][environment]
    if run["environment_id"] != environment_evidence["environment_id"]:
        raise ValueError("solver_run environment_id does not match manifest")
    for field in ("git_sha", "cpu", "architecture", "os_version"):
        if value[field] != environment_evidence[field]:
            raise ValueError(f"record {field} does not match environment evidence")
    solver_environment_evidence = manifest["solvers"][solver]["environments"][environment]
    for field in ("build_id", "image_id", "python_version", "solver_version", "adapter_version"):
        if value[field] != solver_environment_evidence[field]:
            raise ValueError(f"record {field} does not match solver environment evidence")
    case_evidence = manifest["cases"][case_id]
    for field in ("request_fingerprint", "problem_fingerprint"):
        if value[field] != case_evidence[field]:
            raise ValueError(f"record {field} does not match manifest")
    if value["corpus_manifest_sha256"] != manifest["corpus_manifest_sha256"] or value["license_manifest_sha256"] != manifest["license_manifest_sha256"]:
        raise ValueError("record manifest fingerprint mismatch")
    for field in ("request_fingerprint", "problem_fingerprint", "corpus_manifest_sha256", "license_manifest_sha256", "semantic_fingerprint"):
        _sha(value[field], field)
    _git_sha(value["git_sha"], "git_sha")
    for field in ("truth_method", "cpu", "architecture", "os_version", "container_id", "build_id", "image_id", "python_version", "corpus_version"):
        _text(value[field], field)
    if value["protocol_version"] != BENCHMARK_PROTOCOL_V1:
        raise ValueError("unsupported record protocol")
    for field in ("sample_index", "worker_start_count", "worker_rebuild_count", "request_wall_ns", "peak_process_group_rss_bytes", "peak_aggregate_rss_bytes"):
        _nonnegative_integer(value[field], field)
    for field in ("worker_count", "completed_requests"):
        _positive_integer(value[field], field)
    _nonnegative_integer(value["memory_limit_bytes"], "memory_limit_bytes")
    if value["memory_limit_bytes"] != manifest["memory_limit_bytes"]:
        raise ValueError("record memory_limit_bytes does not match manifest")
    for field in ("cleanup_proven", "check_hard_failure"):
        if not isinstance(value[field], bool):
            raise ValueError(f"{field} must be a bool")
    if value["check_failure_reason"] is not None:
        _choice(value["check_failure_reason"], "check_failure_reason", {item.value for item in CheckFailureReason})
    if value["check_hard_failure"] != (value["check_failure_reason"] is not None):
        raise ValueError("check_hard_failure and check_failure_reason must agree")
    if sample_kind == "throughput" and not value["request_wall_ns"]:
        raise ValueError("throughput request_wall_ns must be positive")
    if sample_kind == "cold" and value["worker_start_count"] < 1:
        raise ValueError("cold worker_start_count must be at least one")
    if sample_kind == "rebuild" and value["worker_rebuild_count"] < 1 and not _hard_elimination_record(value):
        raise ValueError("rebuild worker_rebuild_count must be at least one")
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
    if run["business_status"] == "QUALIFIED_FEASIBLE" and (
        run["solve_status"] != "FEASIBLE"
        or run["optimality_status"] != ("OPTIMAL" if run["objective_bounds"]["closed"] else "NOT_PROVEN")
    ):
        raise ValueError("qualified run status must match objective closure")
    if run["business_status"] in {"NO_QUALIFIED_OPPORTUNITY", "NO_ARBITRAGE"} and (
        run["solve_status"] != "INFEASIBLE" or run["optimality_status"] != "NOT_APPLICABLE"
    ):
        raise ValueError("negative run status must be INFEASIBLE and not optimal")
    if run["business_status"] == "UNKNOWN" and (
        run["solve_status"] != "UNKNOWN" or run["optimality_status"] != "NOT_APPLICABLE"
    ):
        raise ValueError("unknown run status must use UNKNOWN and NOT_APPLICABLE")
    _positive_or_zero(run["peak_rss_bytes"], "peak_rss_bytes")
    timings = _named_nonnegative_pairs(run["phase_timings_ns"], "phase_timings_ns")
    if set(timings) != _PHASE_NAMES:
        raise ValueError("phase_timings_ns must contain the exact benchmark phases")
    diagnostics = run["diagnostics"]
    if not isinstance(diagnostics, list) or any(not isinstance(item, list) or len(item) != 2 or not all(isinstance(part, str) for part in item) for item in diagnostics):
        raise ValueError("diagnostics must be string pairs")
    if len({item[0] for item in diagnostics}) != len(diagnostics):
        raise ValueError("diagnostics names must be unique")
    evidence = None if run["evidence"] is None else _validated_evidence_payload(run["evidence"])
    if run["classification"] == BenchmarkClassification.CERTIFICATE_CHECKED.value:
        raise ValueError("current certificate evidence is unbound and cannot be CERTIFICATE_CHECKED")
    if evidence is not None and run["classification"] != BenchmarkClassification.UNKNOWN.value:
        if run["objective_bounds"] != evidence["objective_bounds"]:
            raise ValueError("solver_run objective_bounds must match evidence")
        qualified = run["business_status"] == "QUALIFIED_FEASIBLE"
        if qualified != (evidence["candidate"] is not None):
            raise ValueError("solver_run business_status must match evidence candidate")
        if evidence["candidate"] is not None:
            payout = evidence["payout_lower_bound_units"]
            cost = evidence["cost_upper_bound_units"]
            profit = evidence["guaranteed_profit_units"]
            if (
                any(item is None for item in (payout, cost, profit, evidence["conservative_capital_release_at"], evidence["worst_scenario"]))
                or evidence["candidate"]["claimed_guaranteed_profit_units"] != profit
                or evidence["objective_bounds"]["lower_bound_units"] != profit
                or payout - cost != profit
                or not evidence["fixed_portfolio_closed"]
            ):
                raise ValueError("candidate payout, cost, profit, release, and closure must match evidence")
        if qualified and evidence["global_search_closed"] != run["objective_bounds"]["closed"]:
            raise ValueError("qualified global closure must match objective bounds")
        if run["business_status"] in {"NO_QUALIFIED_OPPORTUNITY", "NO_ARBITRAGE"} and not evidence["global_search_closed"]:
            raise ValueError("negative status requires closed global search")
        if run["business_status"] == "UNKNOWN" and evidence["global_search_closed"]:
            raise ValueError("UNKNOWN status cannot close global search")
    return {**run, "evidence": evidence, "phase_timings_ns": timings}


def _validated_evidence_payload(payload: object) -> dict[str, object]:
    return canonical_payload(solver_evidence_from_payload(payload))


def _ordered_sample_keys(manifest: Mapping[str, object]) -> dict[tuple[str, str], tuple[tuple[object, ...], ...]]:
    canonical_case_ids = tuple(case.case_id for case in _load_full_cases()) if manifest["profile"] == "full" else ()
    if manifest["profile"] == "full" and tuple(manifest["required_case_ids"]) == canonical_case_ids:
        plan = _full_sample_plan(_load_full_cases())
    else:
        plan = [
            (case_id, kind, index, 1)
            for case_id in manifest["required_case_ids"]
            for kind, samples in (("warmup", manifest["warmup_samples"]), ("warm", manifest["measured_samples"]))
            for index in range(samples)
        ]
        for case_id in manifest["throughput_probe_case_ids"]:
            for count in manifest["worker_counts"]:
                plan.extend((case_id, "throughput", index, count) for index in range(manifest["measured_samples"]))
        for kind, case_ids in (("cold", manifest["cold_probe_case_ids"]), ("rebuild", manifest["rebuild_probe_case_ids"])):
            for case_id in case_ids:
                plan.extend((case_id, kind, index, 1) for index in range(manifest["measured_samples"]))
        plan = tuple(plan)
    return {
        (environment, solver): tuple((environment, solver, *key) for key in plan)
        for environment in sorted(manifest["required_environments"])
        if manifest["environments"][environment]["available"]
        for solver in manifest["required_solvers"]
    }


def _sample_counts(records: tuple[dict[str, object], ...], manifest: Mapping[str, object]) -> dict[str, tuple[int, int]]:
    expected = _ordered_sample_keys(manifest)
    return {
        solver: (
            sum(len(keys) for (environment, name), keys in expected.items() if name == solver),
            sum(record["solver_name"] == solver for record in records),
        )
        for solver in manifest["required_solvers"]
    }


def _require_sample_matrix(records: tuple[dict[str, object], ...], manifest: Mapping[str, object]) -> None:
    if any(not record["cleanup_proven"] for record in records):
        raise ValueError("CLEANUP_UNPROVEN")
    expected = _ordered_sample_keys(manifest)
    actual_keys = [
        (record["environment"], record["solver_name"], record["case_id"], record["sample_kind"], record["sample_index"], record["worker_count"])
        for record in records
    ]
    if len(actual_keys) != len(set(actual_keys)):
        raise ValueError("duplicate structural sample")
    if any((record["environment"], record["solver_name"]) not in expected for record in records):
        raise ValueError("unexpected structural sample")
    for pair, plan in expected.items():
        pair_records = [record for record in records if (record["environment"], record["solver_name"]) == pair]
        actual = tuple(
            (record["environment"], record["solver_name"], record["case_id"], record["sample_kind"], record["sample_index"], record["worker_count"])
            for record in pair_records
        )
        if any(_hard_elimination_record(record) for record in pair_records[:-1]):
            raise ValueError("terminal solver failure must be the last observed record")
        if actual == plan:
            continue
        if actual != plan[:len(actual)]:
            raise ValueError("structural samples must be an ordered plan prefix")
        if not actual or not _hard_elimination_record(pair_records[-1]):
            raise ValueError("partial structural samples require a terminal solver failure")


def _hard_gate_failures(
    records: tuple[dict[str, object], ...],
    manifest: Mapping[str, object],
    solver: str,
) -> list[str]:
    failures = set()
    if any(
        manifest["environments"][environment]["available"]
        and not any(
            record["environment"] == environment
            and record["solver_run"]["termination_reason"] == TerminationReason.COMPLETED.value
            for record in records
        )
        and not any(
            record["environment"] == environment and _hard_elimination_record(record)
            for record in records
        )
        for environment in manifest["required_environments"]
    ):
        failures.add("NO_COMPLETED_RUN")
    eliminations = tuple(record for record in records if _hard_elimination_record(record))
    if any(record["check_hard_failure"] for record in eliminations):
        failures.add("CHECK_HARD_FAILURE")
    if any(record["solver_run"]["termination_reason"] in _FATAL_TERMINATIONS for record in eliminations):
        failures.add("FATAL_WORKER_TERMINATION")
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
    if any(
        manifest["environments"][environment]["available"]
        and not all(solver_evidence["environments"][environment][field] for field in ("install_succeeded", "run_succeeded", "reuse_succeeded"))
        for environment in manifest["required_environments"]
    ):
        failures.add("ENVIRONMENT_EVIDENCE_FAILED")
    if any(
        manifest["environments"][environment]["available"]
        and (
            not all(solver_evidence["environments"][environment][field] for field in ("open_source", "license_evidence_present", "source_evidence_present"))
            or solver_evidence["environments"][environment]["commercial_key_required"]
        )
        for environment in manifest["required_environments"]
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
            "certificate": None
            if evidence["certificate"] is None
            else {
                field: evidence["certificate"][field]
                for field in (
                    "certificate_sha256", "certificate_size_bytes",
                    "completed_certificate_sha256", "completed_certificate_size_bytes",
                    "checker_name", "checker_version", "checker_exit_code", "checker_succeeded",
                )
            },
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
            if not cell:
                continue
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
                result["first_qualified_quality"] = _phase_quality(cell, "first_qualified")
            if case_id in manifest["optimal_case_ids"]:
                result["optimal_ns"] = _summary(item["solver_run"]["phase_timings_ns"]["optimal"] for item in cell)
                result["optimal_quality"] = _phase_quality(cell, "optimal")
            warm.append(result)
    metrics["warm"] = warm
    throughput = []
    for environment in manifest["required_environments"]:
        if not manifest["environments"][environment]["available"]:
            continue
        for case_id in manifest["throughput_probe_case_ids"]:
            for worker_count in manifest["worker_counts"]:
                cell = [record for record in records if record["solver_name"] == solver and record["environment"] == environment and record["case_id"] == case_id and record["sample_kind"] == "throughput" and record["worker_count"] == worker_count]
                if not cell:
                    continue
                throughput.append(
                    {
                        "case_id": case_id,
                        "environment": environment,
                        "peak_aggregate_rss_bytes": _summary(item["peak_aggregate_rss_bytes"] for item in cell),
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
                if not selected:
                    continue
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


def _phase_quality(records: list[dict[str, object]], phase: str) -> dict[str, object]:
    achieved = [
        record for record in records
        if (
            record["solver_run"]["business_status"] == "QUALIFIED_FEASIBLE"
            if phase == "first_qualified"
            else record["solver_run"]["optimality_status"] == "OPTIMAL"
        )
    ]
    return {
        "achieved_samples": len(achieved),
        "timing_ns": None if not achieved else _summary(record["solver_run"]["phase_timings_ns"][phase] for record in achieved),
        "unachieved_samples": len(records) - len(achieved),
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
    if len(active) == 1:
        return {
            "status": "SELECTED",
            "reason": "ONLY_SURVIVOR_AFTER_HARD_GATES",
            "selected_solver": active[0],
            "decisive_stage": "HARD_GATE_ELIMINATION",
            "contributing_cells": [],
        }

    stages = (
        ("FIRST_QUALIFIED_P95", lambda solver: _phase_vector(solvers[solver]["metrics"]["warm"], "first_qualified", "p95", manifest["first_qualified_case_ids"])),
        ("FIRST_QUALIFIED_WORST", lambda solver: _phase_vector(solvers[solver]["metrics"]["warm"], "first_qualified", "worst", manifest["first_qualified_case_ids"])),
        ("PEAK_RSS_P95", lambda solver: _cell_vector(solvers[solver]["metrics"]["warm"] + solvers[solver]["metrics"]["throughput"], "peak_aggregate_rss_bytes", "p95")),
        ("PEAK_RSS_WORST", lambda solver: _cell_vector(solvers[solver]["metrics"]["warm"] + solvers[solver]["metrics"]["throughput"], "peak_aggregate_rss_bytes", "worst")),
        ("THROUGHPUT_WORST", lambda solver: tuple(-value for value in _cell_vector(solvers[solver]["metrics"]["throughput"], "requests_per_second", "worst", descending=False))),
        ("REBUILD_P95", lambda solver: _cell_vector(solvers[solver]["metrics"]["rebuild"], "rebuild_ns", "p95")),
        ("REBUILD_WORST", lambda solver: _cell_vector(solvers[solver]["metrics"]["rebuild"], "rebuild_ns", "worst")),
        ("MANUAL_INTERVENTIONS", lambda solver: (Fraction(manifest["solvers"][solver]["manual_interventions"]),)),
        ("INSTALLATION_DURATION", lambda solver: tuple(sorted(
            (Fraction(item["installation_ns"]) for item in manifest["solvers"][solver]["environments"].values()),
            reverse=True,
        ))),
        ("COLD_START_P95", lambda solver: _cell_vector(solvers[solver]["metrics"]["cold"], "cold_ns", "p95")),
        ("COLD_START_WORST", lambda solver: _cell_vector(solvers[solver]["metrics"]["cold"], "cold_ns", "worst")),
        ("TIME_TO_OPTIMAL_P95", lambda solver: _phase_vector(solvers[solver]["metrics"]["warm"], "optimal", "p95", manifest["optimal_case_ids"])),
        ("TIME_TO_OPTIMAL_WORST", lambda solver: _phase_vector(solvers[solver]["metrics"]["warm"], "optimal", "worst", manifest["optimal_case_ids"])),
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


def _phase_vector(
    cells: list[dict[str, object]],
    phase: str,
    statistic: str,
    case_ids: tuple[str, ...],
) -> tuple[Fraction, ...]:
    selected = [cell for cell in cells if cell["case_id"] in case_ids]
    nonachievement = sorted(
        (Fraction(cell[f"{phase}_quality"]["unachieved_samples"]) for cell in selected),
        reverse=True,
    )
    timings = sorted(
        (
            _fraction(cell[f"{phase}_quality"]["timing_ns"][statistic])
            for cell in selected
            if cell[f"{phase}_quality"]["timing_ns"] is not None
        ),
        reverse=True,
    )
    return tuple(nonachievement + timings)


def _gap_vector(cells: list[dict[str, object]], case_ids: tuple[str, ...]) -> tuple[tuple[Fraction, Fraction], ...]:
    values = []
    for cell in cells:
        if cell["case_id"] not in case_ids:
            continue
        quality = cell["objective_gap_quality"]
        total = quality["known_gap_samples"] + quality["unknown_gap_samples"]
        rank = 0 if quality["closed_samples"] == total else 1 if quality["known_gap_samples"] else 2
        gap = 0 if quality["gap_units"] is None else _fraction(quality["gap_units"]["worst"])
        values.append((f"{cell['environment']}/{cell['case_id']}", Fraction(rank), gap))
    values.sort(key=lambda item: (item[1], item[2]), reverse=True)
    return tuple((rank, gap) for _, rank, gap in values)


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
    elif "RSS" in stage:
        cells = metrics["warm"] + metrics["throughput"]
    else:
        cells = metrics["warm"] if "CERTIFICATE" in stage else []
    return sorted(f"{cell['environment']}/{cell['case_id']}" + (f"/workers-{cell['worker_count']}" if "worker_count" in cell else "") for cell in cells)


def _summary(values: object) -> dict[str, object]:
    ordered = [Fraction(value) for value in values]
    return {
        "p50": _exact_json(statistics.median(ordered)),
        "p95": _exact_json(ordered[0] if len(ordered) == 1 else statistics.quantiles(ordered, n=100, method="inclusive")[94]),
        "worst": _exact_json(max(ordered)),
    }


def _throughput_summary(values: object) -> dict[str, object]:
    ordered = [Fraction(value) for value in values]
    return {
        "p50": _exact_json(statistics.median(ordered)),
        "p95": _exact_json(ordered[0] if len(ordered) == 1 else statistics.quantiles(ordered, n=100, method="inclusive")[94]),
        "worst": _exact_json(min(ordered)),
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


def _signed_int64(value: object, name: str) -> int:
    number = _integer(value, name)
    if not INT64_MIN <= number <= INT64_MAX:
        raise ValueError(f"{name} must be a signed 64-bit integer")
    return number


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


def _git_sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 40 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be lowercase hexadecimal")
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
            _signed_int64(bounds[field], field)
    if not isinstance(bounds["closed"], bool):
        raise ValueError("objective_bounds.closed must be a bool")
    lower = bounds["lower_bound_units"]
    upper = bounds["upper_bound_units"]
    gap = bounds["gap_units"]
    if bounds["closed"]:
        if lower is None or upper is None or lower != upper or gap != 0:
            raise ValueError("closed objective_bounds require equal bounds and zero gap")
    elif lower is not None and upper is not None:
        if lower >= upper or gap != upper - lower:
            raise ValueError("open objective_bounds require ordered bounds and their exact gap")
    elif gap is not None:
        raise ValueError("objective_bounds gap requires both bounds")


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


def _markdown_report(summary: Mapping[str, object], sample_counts: Mapping[str, tuple[int, int]] | None = None) -> str:
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
        lines.extend((f"### {solver}", "", f"Hard-gate failures: {failures}"))
        if sample_counts is not None:
            planned, observed = sample_counts[solver]
            lines.append(f"Samples: {observed} observed / {planned} planned")
        lines.extend(("", "| metric | cell | p95 | worst |", "| --- | --- | ---: | ---: |"))
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


def _load_synthetic_cases(*, include_measurement_only: bool) -> tuple[BenchmarkCase, ...]:
    synthetic_bytes = _SYNTHETIC_CORPUS.read_bytes()
    if synthetic_bytes != generate_synthetic_corpus(4901):
        raise ValueError("committed synthetic corpus changed")
    payload = _strict_json(synthetic_bytes, "synthetic corpus")
    value = _strict_object(
        payload,
        "synthetic corpus",
        {"case_count", "cases", "manifest_sha256", "schema_version", "seed"},
    )
    if value["schema_version"] != _SYNTHETIC_SCHEMA_V1 or value["seed"] != 4901 or value["case_count"] != 24:
        raise ValueError("synthetic corpus identity changed")
    if not isinstance(value["cases"], list):
        raise ValueError("synthetic corpus cases must be an array")
    decoded = []
    for item in value["cases"]:
        case = _strict_object(
            item,
            "synthetic case",
            {"case_id", "expected_result", "request", "request_fingerprint", "result_fingerprint", "truth_method"}
            if isinstance(item, Mapping) and item.get("truth_method") == "exact_oracle_v1"
            else {"case_id", "declared_search_surface", "expected_result", "request", "request_fingerprint", "result_fingerprint", "truth_method"},
        )
        if case["truth_method"] == "measurement_only" and not include_measurement_only:
            continue
        request = request_from_payload(_mapping(case["request"], "request"))
        case_id = _text(case["case_id"], "case_id")
        request_fingerprint = _sha(case["request_fingerprint"], "request_fingerprint")
        if canonical_payload(request) != case["request"] or fingerprint(request) != request_fingerprint:
            raise ValueError(f"synthetic case request changed: {case_id}")
        if case["truth_method"] == "measurement_only":
            if case["expected_result"] is not None or case["result_fingerprint"] is not None:
                raise ValueError(f"synthetic measurement-only case truth changed: {case_id}")
            decoded.append(
                BenchmarkCase(case_id, request, None, request_fingerprint, None, request.budget, "measurement_only")
            )
            continue
        if case["truth_method"] != "exact_oracle_v1":
            raise ValueError(f"synthetic case truth method changed: {case_id}")
        result = result_from_payload(_mapping(case["expected_result"], "expected_result"))
        result_fingerprint = _sha(case["result_fingerprint"], "result_fingerprint")
        if (
            canonical_payload(result) != case["expected_result"]
            or fingerprint(result) != result_fingerprint
            or result != _oracle_result(request)
        ):
            raise ValueError(f"synthetic case truth changed: {case_id}")
        decoded.append(
            BenchmarkCase(
                case_id,
                request,
                result,
                request_fingerprint,
                result_fingerprint,
                request.budget,
                "exact_oracle_v1",
            )
        )
    expected_count = 24 if include_measurement_only else 18
    if len(decoded) != expected_count:
        raise ValueError(f"synthetic corpus must contain exactly {expected_count} selected cases")
    return tuple(decoded)


def _load_quick_cases() -> tuple[BenchmarkCase, ...]:
    return (*load_canonical_cases(), *_load_synthetic_cases(include_measurement_only=False))


def _load_full_cases() -> tuple[BenchmarkCase, ...]:
    approved_cases = _load_approved_corpus(_APPROVED_CORPUS)["cases"]
    if len(approved_cases) != 1:
        raise ValueError("full corpus must contain exactly one approved case")
    approved = _strict_object(
        approved_cases[0],
        "approved case",
        {
            "anonymized_problem_fingerprint", "approval_id", "approved_at", "approver", "captured_at",
            "case_id", "generation_id", "problem", "source_alias", "source_problem_fingerprint",
        },
    )
    approved_problem = problem_from_payload(_mapping(approved["problem"], "approved problem"))
    approved_problem_fingerprint = "sha256:a1b63df2776a88522c3a00ed535489b777b18be827445b0419bf7d8359f127c4"
    approved_case_id = "approved:a1b63df2776a88522c3a00ed"
    if (
        fingerprint(approved_problem) != approved_problem_fingerprint
        or _sha(approved["anonymized_problem_fingerprint"], "anonymized_problem_fingerprint") != approved_problem_fingerprint
        or _sha(approved["source_problem_fingerprint"], "source_problem_fingerprint")
        != "sha256:c6c5a2955c6f96b03d6c8f10deccd96b1a54d713cea3ec0391a1590801ced59f"
        or _text(approved["case_id"], "case_id") != approved_case_id
    ):
        raise ValueError("approved corpus identity changed")
    # ponytail: Task 10 v1 has one approved case; add per-case budget metadata when the approved corpus grows.
    request = OracleRequest(
        REQUEST_SCHEMA_V1,
        SearchMode.ADMISSION,
        approved_problem,
        OracleBudget(289, 9, 64),
    )
    result = _oracle_result(request)
    if (
        result.proof_status != ProofStatus.PROVEN
        or result.business_status != BusinessStatus.NO_QUALIFIED_OPPORTUNITY
    ):
        raise ValueError("approved corpus oracle truth changed")
    approved_case = BenchmarkCase(
        approved_case_id,
        request,
        result,
        fingerprint(request),
        fingerprint(result),
        request.budget,
        "exact_oracle_v1",
    )
    return (*load_canonical_cases(), *_load_synthetic_cases(include_measurement_only=True), approved_case)


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
    if request.mode == SearchMode.ADMISSION and evaluation.quantities != exact_solution.quantities:
        return DifferentialCheck(BenchmarkClassification.MEASUREMENT_ONLY, None, False, None)
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


def _atomic_write_json(path: Path, payload: dict[str, object], *, overwrite: bool = True) -> None:
    _atomic_write_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        overwrite=overwrite,
    )


def _atomic_write_bytes(path: Path, data: bytes, *, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        if overwrite:
            os.replace(temporary, path)
            temporary = None
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                raise ValueError(f"benchmark evidence already exists: {path}") from error
            temporary.unlink()
            temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _current_git_sha(root: Path = _ROOT) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env=_native_subprocess_env(Path(sys.executable)),
    )
    return _git_sha(completed.stdout.strip(), "git_sha")


def _expected_build_key(solver: str) -> str:
    requirements = _BENCHMARK_ROOT / "requirements" / f"{solver}.txt"
    dockerfile = _BENCHMARK_ROOT / ("Dockerfile.scip" if solver == "scip" else "Dockerfile.python")
    material = (
        f"python={sys.version_info.major}.{sys.version_info.minor}\n"
        f"os={platform.system()}\n"
        f"architecture={platform.machine()}\n"
        f"protocol={BENCHMARK_PROTOCOL_V1}\n"
    ).encode() + requirements.read_bytes() + dockerfile.read_bytes()
    return hashlib.sha256(material).hexdigest()


def _venv_python_version(path: Path) -> str:
    configuration = path / "pyvenv.cfg"
    if configuration.is_file():
        for line in configuration.read_text().splitlines():
            if line.startswith("version = "):
                return _text(line.removeprefix("version = ").strip(), "python_version")
    return platform.python_version()


def _native_subprocess_env(python: Path) -> dict[str, str]:
    return {
        "PATH": f"{python.parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(_ROOT / "src"),
        "PYTHONSAFEPATH": "1",
    }


def _docker_run_command(image: str, program: list[str], cidfile: Path) -> list[str]:
    return [
        "docker", "run", "--rm", "--interactive", "--network", "none",
        "--cidfile", str(cidfile),
        "--volume", f"{_ROOT}:/workspace:ro", "--workdir", "/workspace",
        "--env", "PYTHONPATH=/workspace/src", "--env", "PYTHONSAFEPATH=1",
        "--env", "PYTHONNOUSERSITE=1", image, *program,
    ]


def _docker_completed(image: str, program: list[str]) -> subprocess.CompletedProcess[str] | None:
    with tempfile.TemporaryDirectory(prefix="open-trader-discovery-") as directory:
        cidfile = Path(directory) / "container-id"
        try:
            completed = subprocess.run(
                _docker_run_command(image, program, cidfile),
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        container_id = _validated_container_id(cidfile)
        _prove_container_cleanup(container_id)
        return completed if completed is not None and completed.returncode == 0 else None


def _docker_json_self_check(image: str, program: list[str], name: str) -> dict[str, object] | None:
    completed = _docker_completed(image, program)
    if completed is None:
        return None
    lines = completed.stdout.splitlines() if isinstance(completed.stdout, str) else ()
    if len(lines) != 1:
        return None
    try:
        return _mapping(_strict_json(lines[0].encode(), name), name)
    except (TypeError, ValueError):
        return None


def _docker_image_id(image: str) -> str | None:
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    image_id = completed.stdout.strip() if completed.returncode == 0 and isinstance(completed.stdout, str) else ""
    if len(image_id) != 71 or not image_id.startswith("sha256:") or any(char not in "0123456789abcdef" for char in image_id[7:]):
        return None
    return image_id


_SCIP_EXACT_SELF_CHECK_KEYS = {
    "adapter", "solver", "version", "status", "certificate_sha256", "certificate_size_bytes",
    "completed_certificate_sha256", "completed_certificate_size_bytes", "generation_ns", "completion_ns",
    "check_ns", "corrupt_checker_exit_code", "corrupt_check_ns", "constrained_status",
    "equality_status", "ranged_status", "infeasible_status", "lossy_status", "lossy_native_status",
}


def _scip_exact_self_check_succeeded(payload: Mapping[str, object]) -> bool:
    try:
        value = _strict_object(payload, "scip-exact", _SCIP_EXACT_SELF_CHECK_KEYS)
        if value["adapter"] != "ScipBackend" or value["solver"] != "SCIP+VIPR" or value["version"] != _SOLVER_VERSIONS["scip"]:
            return False
        if value["status"] != "OPTIMAL" or any(value[field] != "OPTIMAL" for field in ("constrained_status", "equality_status", "ranged_status")):
            return False
        if value["infeasible_status"] != "INFEASIBLE" or value["lossy_status"] != "UNKNOWN":
            return False
        if "PROOF_UNCLOSED" not in _text(value["lossy_native_status"], "lossy_native_status"):
            return False
        if _integer(value["corrupt_checker_exit_code"], "corrupt_checker_exit_code") == 0:
            return False
        for field in ("certificate_sha256", "completed_certificate_sha256"):
            _sha(value[field], field)
        for field in (
            "certificate_size_bytes", "completed_certificate_size_bytes", "generation_ns", "completion_ns",
            "check_ns", "corrupt_check_ns",
        ):
            _positive_integer(value[field], field)
    except (TypeError, ValueError):
        return False
    return True


def _discover_linux_environment() -> tuple[dict[str, object], dict[str, dict[str, object]]] | None:
    artifacts: dict[str, dict[str, object]] = {}
    self_check_identities = {
        "highs": ("HighsBackend", "highspy"),
        "scip": ("ScipBackend", "pyscipopt"),
        "cp_sat": ("CpSatBackend", "ortools"),
    }
    for solver in _SOLVERS:
        key = _expected_build_key(solver)
        image = f"open-trader-prediction-solver-{solver}:{key}"
        image_id = _docker_image_id(image)
        if image_id is None:
            return None
        payload = _docker_json_self_check(
            image_id,
            ["python", "-m", "open_trader.prediction_solver_backends", "--self-check", solver],
            f"{solver} docker self-check",
        )
        adapter, native_solver = self_check_identities[solver]
        if payload != {
            "adapter": adapter,
            "solver": native_solver,
            "version": _SOLVER_VERSIONS[solver],
            "status": "OPTIMAL",
        }:
            return None
        if solver == "scip":
            exact = _docker_json_self_check(
                image_id,
                ["python", "-m", "open_trader.prediction_solver_backends", "--self-check", "scip-exact"],
                "scip-exact",
            )
            if exact is None or not _scip_exact_self_check_succeeded(exact):
                return None
        artifacts[solver] = {
            "adapter_version": WORKER_VERSION,
            "build_id": image,
            "commercial_key_required": False,
            "image_id": image_id,
            "install_succeeded": True,
            "installation_ns": 0,
            "license_evidence_present": True,
            "open_source": True,
            "python_version": "unavailable",
            "reuse_succeeded": True,
            "run_succeeded": True,
            "solver_version": _SOLVER_VERSIONS[solver],
            "source_evidence_present": True,
        }
    probe = _docker_json_self_check(
        artifacts["highs"]["image_id"],
        ["python", "-c", "import json, platform; print(json.dumps({'python_version': platform.python_version(), 'architecture': platform.machine(), 'cpu': platform.processor() or platform.machine(), 'os_version': platform.platform(), 'probe': 'linux-platform-highs'}, sort_keys=True))"],
        "linux platform probe",
    )
    if probe is None or set(probe) != {"python_version", "architecture", "cpu", "os_version", "probe"} or probe["probe"] != "linux-platform-highs":
        return None
    try:
        python_version = _text(probe["python_version"], "linux python_version")
        architecture = _text(probe["architecture"], "linux architecture")
        cpu = _text(probe["cpu"], "linux cpu")
        os_version = _text(probe["os_version"], "linux os_version")
        git_sha = _current_git_sha()
    except ValueError:
        return None
    for artifact in artifacts.values():
        artifact["python_version"] = python_version
    environment_id = "linux:" + hashlib.sha256(
        json.dumps(
            {"architecture": architecture, "cpu": cpu, "git_sha": git_sha, "os_version": os_version, "python_version": python_version},
            sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "available": True,
        "architecture": architecture,
        "cpu": cpu,
        "environment_id": environment_id,
        "git_sha": git_sha,
        "os_version": os_version,
    }, artifacts


def _validated_container_id(path: Path) -> str:
    try:
        container_id = path.read_text().strip()
    except OSError as error:
        raise RuntimeError("CONTAINER_CLEANUP_UNPROVEN") from error
    if len(container_id) != 64 or any(char not in "0123456789abcdef" for char in container_id):
        raise RuntimeError("CONTAINER_CLEANUP_UNPROVEN")
    return container_id


def _container_state(container_id: str) -> str:
    try:
        completed = subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode == 0:
        return "present"
    if (
        completed.returncode == 1
        and (completed.stdout, completed.stderr) in {
            ("", f"Error: No such object: {container_id}\n"),
            ("[]\n", f"Error: No such object: {container_id}\n"),
            ("[]\n", f"Error response from daemon: No such container: {container_id}\n"),
        }
    ):
        return "absent"
    return "unknown"


def _container_is_absent(container_id: str) -> bool:
    return _container_state(container_id) == "absent"


def _prove_container_cleanup(container_id: str) -> None:
    state = _container_state(container_id)
    if state == "absent":
        return
    if state != "present":
        raise RuntimeError("CONTAINER_CLEANUP_UNPROVEN")
    try:
        subprocess.run(
            ["docker", "rm", "--force", container_id],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("CONTAINER_CLEANUP_UNPROVEN") from error
    if _container_state(container_id) != "absent":
        raise RuntimeError("CONTAINER_CLEANUP_UNPROVEN")
    raise RuntimeError("CONTAINER_CLEANUP_UNPROVEN")


class _DockerHarness:
    def __init__(self, image: str, solver: str) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="open-trader-solver-")
        self._cidfile = Path(self._directory.name) / "container-id"
        self._worker = WorkerHarness(
            _docker_run_command(
                image,
                ["python", "-m", "open_trader.prediction_solver_worker", "--backend", solver],
                self._cidfile,
            ),
            request_timeout_ms=20_000,
            startup_timeout_ms=5_000,
        )
        self.container_id = "unavailable"
        self._submitted = False

    def __enter__(self) -> "_DockerHarness":
        self._worker.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            self._worker.__exit__(exc_type, exc, tb)
        finally:
            try:
                if self._submitted:
                    self.container_id = _validated_container_id(self._cidfile)
                    _prove_container_cleanup(self.container_id)
            finally:
                self._directory.cleanup()

    def submit(self, request: WorkerRequest) -> WorkerOutcome:
        self._submitted = True
        outcome = self._worker.submit(request)
        self.container_id = _validated_container_id(self._cidfile)
        return outcome

    def __getattr__(self, name: str):
        return getattr(self._worker, name)


def _docker_harness(image: str, solver: str) -> _DockerHarness:
    return _DockerHarness(image, solver)


def _discover_quick_environment(env_root: Path) -> tuple[dict[str, object], dict[str, dict[str, object]]] | None:
    artifacts: dict[str, dict[str, object]] = {}
    self_check_identities = {
        "highs": ("HighsBackend", "highspy"),
        "scip": ("ScipBackend", "pyscipopt"),
        "cp_sat": ("CpSatBackend", "ortools"),
    }
    for solver in _SOLVERS:
        environment = env_root / solver
        key_path = environment / ".build-key"
        python = environment / "bin" / "python"
        try:
            key = key_path.read_text().strip()
            executable = python.is_file() and python.stat().st_size > 0 and os.access(python, os.X_OK)
        except (OSError, UnicodeError):
            return None
        if (
            key != _expected_build_key(solver)
            or len(key) != 64
            or any(character not in "0123456789abcdef" for character in key)
            or not executable
        ):
            return None
        try:
            smoke = subprocess.run(
                [str(python), "-m", "open_trader.prediction_solver_backends", "--self-check", solver],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                env=_native_subprocess_env(python),
            )
            lines = smoke.stdout.splitlines() if isinstance(smoke.stdout, str) else ()
            if smoke.returncode != 0 or len(lines) != 1:
                return None
            payload = _strict_object(
                _strict_json(lines[0].encode(), f"{solver} self-check"),
                f"{solver} self-check",
                {"adapter", "solver", "version", "status"},
            )
            adapter, native_solver = self_check_identities[solver]
            if payload != {
                "adapter": adapter,
                "solver": native_solver,
                "version": _SOLVER_VERSIONS[solver],
                "status": "OPTIMAL",
            }:
                return None
        except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
            return None
        artifacts[solver] = {
            "adapter_version": WORKER_VERSION,
            "build_id": f"sha256:{key}",
            "commercial_key_required": False,
            "image_id": "none",
            "install_succeeded": True,
            "installation_ns": 0,
            "license_evidence_present": True,
            "open_source": True,
            "python_version": _venv_python_version(environment),
            "reuse_succeeded": True,
            "run_succeeded": True,
            "solver_version": _SOLVER_VERSIONS[solver],
            "source_evidence_present": True,
        }
    git_sha = _current_git_sha()
    cpu = platform.processor() or platform.machine()
    architecture = platform.machine()
    os_version = platform.platform()
    environment_id = "macos:" + hashlib.sha256(
        json.dumps(
            {"architecture": architecture, "cpu": cpu, "git_sha": git_sha, "os_version": os_version},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return (
        {
            "available": True,
            "architecture": architecture,
            "cpu": cpu,
            "environment_id": environment_id,
            "git_sha": git_sha,
            "os_version": os_version,
        },
        artifacts,
    )


def _model_dimensions(problem: ArbitrageProblem) -> dict[str, int]:
    joint_states = 1
    for state in problem.terminal_state_sets:
        joint_states *= len(state.atoms)
    quantity_domain = 1
    for action in problem.actions:
        quantity_domain *= 1 + action.max_quantity_lots - action.min_quantity_lots + 1
    return {
        "action_count": len(problem.actions),
        "contract_count": len(problem.terminal_state_sets),
        "cost_slice_count": sum(len(action.cost_slices) for action in problem.actions),
        "joint_state_count": joint_states,
        "quantity_domain_size": quantity_domain,
        "relationship_count": len(problem.constraint_model.relations) + len(problem.constraint_model.forbidden_atom_combinations),
        "terminal_atom_count": sum(len(state.atoms) for state in problem.terminal_state_sets),
    }


def _quick_manifest(
    cases: tuple[BenchmarkCase, ...],
    environment: Mapping[str, object],
    artifacts: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    corpus_digest = hashlib.sha256(_CANONICAL_FIXTURE.read_bytes() + _SYNTHETIC_CORPUS.read_bytes()).hexdigest()
    license_digest = hashlib.sha256(_LICENSE_MANIFEST.read_bytes()).hexdigest()
    return {
        "approved_case_count": len(_load_approved_corpus(_APPROVED_CORPUS)["cases"]),
        "cases": {
            case.case_id: {
                "model_dimensions": _model_dimensions(case.request.problem),
                "oracle_limits": canonical_payload(case.budget),
                "problem_fingerprint": fingerprint(case.request.problem),
                "request_fingerprint": case.request_fingerprint,
                "request_id": case.case_id,
            }
            for case in cases
        },
        "cold_probe_case_ids": [],
        "corpus_manifest_sha256": f"sha256:{corpus_digest}",
        "environments": {"macos": dict(environment)},
        "first_qualified_case_ids": [
            case.case_id
            for case in cases
            if case.expected_result is not None and case.expected_result.business_status == BusinessStatus.QUALIFIED_FEASIBLE
        ],
        "hard_time_limit_ms": QUICK_HARD_TIME_LIMIT_MS,
        "license_manifest_sha256": f"sha256:{license_digest}",
        "measured_samples": 1,
        "memory_limit_bytes": QUICK_MEMORY_LIMIT_BYTES,
        "max_constraint_generation_rounds": QUICK_MAX_CONSTRAINT_GENERATION_ROUNDS,
        "optimal_case_ids": [
            case.case_id
            for case in cases
            if case.expected_result is not None and case.expected_result.optimality_status == OptimalityStatus.OPTIMAL
        ],
        "profile": "quick",
        "rebuild_probe_case_ids": [],
        "required_case_ids": [case.case_id for case in cases],
        "required_environments": ["macos"],
        "required_solvers": list(_SOLVERS),
        "schema_version": _MANIFEST_SCHEMA_V1,
        "soft_time_limit_ms": QUICK_SOFT_TIME_LIMIT_MS,
        "solvers": {
            solver: {
                "environments": {"macos": dict(artifacts[solver])},
                "manual_interventions": 0,
                "whole_claim_certificate_bound": False,
            }
            for solver in _SOLVERS
        },
        "throughput_probe_case_ids": [],
        "warmup_samples": 0,
        "worker_counts": [1],
    }


def _full_manifest(
    cases: tuple[BenchmarkCase, ...],
    environments: Mapping[str, Mapping[str, object]],
    solver_environments: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> dict[str, object]:
    corpus_digest = hashlib.sha256(
        _CANONICAL_FIXTURE.read_bytes() + _SYNTHETIC_CORPUS.read_bytes() + _APPROVED_CORPUS.read_bytes()
    ).hexdigest()
    license_digest = hashlib.sha256(_LICENSE_MANIFEST.read_bytes()).hexdigest()
    probe_case_id = "single_contract_complement"
    return {
        "approved_case_count": 1,
        "cases": {
            case.case_id: {
                "model_dimensions": _model_dimensions(case.request.problem),
                "oracle_limits": canonical_payload(case.budget),
                "problem_fingerprint": fingerprint(case.request.problem),
                "request_fingerprint": case.request_fingerprint,
                "request_id": case.case_id,
            }
            for case in cases
        },
        "cold_probe_case_ids": [probe_case_id],
        "corpus_manifest_sha256": f"sha256:{corpus_digest}",
        "environments": {environment: dict(environments[environment]) for environment in ("macos", "linux")},
        "first_qualified_case_ids": [
            case.case_id
            for case in cases
            if case.expected_result is not None and case.expected_result.business_status == BusinessStatus.QUALIFIED_FEASIBLE
        ],
        "hard_time_limit_ms": QUICK_HARD_TIME_LIMIT_MS,
        "license_manifest_sha256": f"sha256:{license_digest}",
        "measured_samples": 30,
        "memory_limit_bytes": QUICK_MEMORY_LIMIT_BYTES,
        "max_constraint_generation_rounds": QUICK_MAX_CONSTRAINT_GENERATION_ROUNDS,
        "optimal_case_ids": [
            case.case_id
            for case in cases
            if case.expected_result is not None and case.expected_result.optimality_status == OptimalityStatus.OPTIMAL
        ],
        "profile": "full",
        "rebuild_probe_case_ids": [probe_case_id],
        "required_case_ids": [case.case_id for case in cases],
        "required_environments": ["macos", "linux"],
        "required_solvers": list(_SOLVERS),
        "schema_version": _MANIFEST_SCHEMA_V1,
        "soft_time_limit_ms": QUICK_SOFT_TIME_LIMIT_MS,
        "solvers": {
            solver: {
                "environments": {
                    environment: dict(solver_environments[solver][environment])
                    for environment in ("macos", "linux")
                },
                "manual_interventions": 0,
                "whole_claim_certificate_bound": False,
            }
            for solver in _SOLVERS
        },
        "throughput_probe_case_ids": [probe_case_id],
        "warmup_samples": 5,
        "worker_counts": [1, 2, 4],
    }


def _full_sample_plan(cases: tuple[BenchmarkCase, ...]) -> tuple[tuple[str, str, int, int], ...]:
    plan = [
        (case.case_id, kind, index, 1)
        for case in cases
        for kind, samples in (("warmup", 5), ("warm", 30))
        for index in range(samples)
    ]
    for worker_count in (1, 2, 4):
        plan.extend(("single_contract_complement", "throughput", index, worker_count) for index in range(30))
    for kind in ("cold", "rebuild"):
        plan.extend(("single_contract_complement", kind, index, 1) for index in range(30))
    if len(plan) != 1_585 or len(set(plan)) != len(plan):
        raise ValueError("full sample plan must have exactly 1,585 unique structural keys")
    return tuple(plan)


def _unknown_run(
    case: BenchmarkCase,
    solver: str,
    outcome: WorkerOutcome,
    environment: Mapping[str, object],
    artifact: Mapping[str, object],
    termination: TerminationReason,
    *,
    evidence: SolverEvidence | None = None,
    phase_timings: Mapping[str, int] | None = None,
    independent_check_ns: int = 0,
) -> SolverRun:
    timings = {name: 0 for name in WORKER_PHASE_NAMES}
    if phase_timings is not None:
        timings.update(phase_timings)
    timings["independent_check"] = independent_check_ns
    return SolverRun(
        BENCHMARK_PROTOCOL_V1,
        case.case_id,
        solver,
        _text(artifact["solver_version"], "solver_version"),
        _text(artifact["adapter_version"], "adapter_version"),
        f"pid-{outcome.worker_pid or 'unavailable'}",
        _text(environment["environment_id"], "environment_id"),
        SolveStatus.UNKNOWN,
        ProofStatus.UNKNOWN,
        BusinessStatus.UNKNOWN,
        OptimalityStatus.NOT_APPLICABLE,
        ObjectiveBounds(None, None, None, False),
        BenchmarkClassification.UNKNOWN,
        termination,
        evidence,
        None,
        tuple(timings.items()),
        outcome.peak_rss_kib * 1024,
        tuple((f"worker_{index}", text) for index, text in enumerate(outcome.response.diagnostics if outcome.response else ())),
    )


def _solver_run_from_outcome(
    case: BenchmarkCase,
    solver: str,
    outcome: WorkerOutcome,
    environment: Mapping[str, object],
    artifact: Mapping[str, object],
) -> tuple[SolverRun, DifferentialCheck]:
    empty_check = DifferentialCheck(BenchmarkClassification.UNKNOWN, None, False, None)
    if outcome.status != "OK" or outcome.response is None:
        termination = TerminationReason.CLEANUP_UNPROVEN if not outcome.cleanup_proven else (
            TerminationReason(outcome.termination)
            if outcome.termination in {item.value for item in TerminationReason}
            else TerminationReason.PROTOCOL_MISMATCH
        )
        return _unknown_run(case, solver, outcome, environment, artifact, termination), empty_check
    timings = dict(outcome.response.phase_timings_ns)
    check_started_ns = time.perf_counter_ns()
    try:
        evidence = solver_evidence_from_payload(outcome.response.evidence)
    except (TypeError, ValueError):
        check_ns = time.perf_counter_ns() - check_started_ns
        failure = _check_failure(CheckFailureReason.CLAIM_MISMATCH)
        return _unknown_run(
            case, solver, outcome, environment, artifact, TerminationReason.INVALID_OUTPUT,
            phase_timings=timings, independent_check_ns=check_ns,
        ), failure
    checked = check_solver_claim(
        case.request,
        evidence,
        claimed_problem_fingerprint=fingerprint(case.request.problem),
        truth_method=case.truth_method,
    )
    check_ns = time.perf_counter_ns() - check_started_ns
    if checked.hard_failure:
        return _unknown_run(
            case, solver, outcome, environment, artifact, TerminationReason.COMPLETED,
            evidence=evidence, phase_timings=timings, independent_check_ns=check_ns,
        ), checked
    phase_pairs = tuple({**timings, "independent_check": check_ns}.items())
    common = {
        "schema_version": BENCHMARK_PROTOCOL_V1,
        "request_id": case.case_id,
        "solver_name": solver,
        "solver_version": _text(artifact["solver_version"], "solver_version"),
        "adapter_version": _text(artifact["adapter_version"], "adapter_version"),
        "worker_id": f"pid-{outcome.worker_pid or 'unavailable'}",
        "environment_id": _text(environment["environment_id"], "environment_id"),
        "termination_reason": TerminationReason.COMPLETED,
        "evidence": evidence,
        "phase_timings_ns": phase_pairs,
        "peak_rss_bytes": outcome.peak_rss_kib * 1024,
        "diagnostics": tuple((f"worker_{index}", text) for index, text in enumerate(outcome.response.diagnostics)),
    }
    if checked.classification == BenchmarkClassification.CHECKED:
        assert checked.canonical_result is not None
        result = checked.canonical_result
        return SolverRun(
            **common,
            solve_status=result.solve_status,
            proof_status=result.proof_status,
            business_status=result.business_status,
            optimality_status=result.optimality_status,
            objective_bounds=result.objective_bounds,
            classification=checked.classification,
            canonical_result=result,
        ), checked
    if evidence.candidate is not None:
        return SolverRun(
            **common,
            solve_status=SolveStatus.FEASIBLE,
            proof_status=ProofStatus.UNKNOWN,
            business_status=BusinessStatus.QUALIFIED_FEASIBLE,
            optimality_status=OptimalityStatus.OPTIMAL if evidence.objective_bounds.closed else OptimalityStatus.NOT_PROVEN,
            objective_bounds=evidence.objective_bounds,
            classification=checked.classification,
            canonical_result=None,
        ), checked
    return _unknown_run(
        case, solver, outcome, environment, artifact, TerminationReason.COMPLETED,
        evidence=evidence, phase_timings=timings, independent_check_ns=check_ns,
    ), checked


def _benchmark_record(
    case: BenchmarkCase,
    solver: str,
    outcome: WorkerOutcome,
    run: SolverRun,
    checked: DifferentialCheck,
    manifest: Mapping[str, object],
    environment: Mapping[str, object],
    artifact: Mapping[str, object],
    wall_ns: int,
    start_count: int,
    rebuild_count: int,
    *,
    profile: str,
    environment_name: str,
    corpus_version: str,
    sample_kind: str,
    sample_index: int,
    worker_count: int,
    completed_requests: int,
    container_id: str,
    peak_process_group_rss_bytes: int,
    peak_aggregate_rss_bytes: int,
) -> dict[str, object]:
    record = {
        "adapter_version": artifact["adapter_version"],
        "architecture": environment["architecture"],
        "build_id": artifact["build_id"],
        "case_id": case.case_id,
        "check_failure_reason": None if checked.failure_reason is None else checked.failure_reason.value,
        "check_hard_failure": checked.hard_failure,
        "cleanup_proven": outcome.cleanup_proven,
        "completed_requests": completed_requests,
        "container_id": container_id,
        "corpus_manifest_sha256": manifest["corpus_manifest_sha256"],
        "corpus_version": corpus_version,
        "cpu": environment["cpu"],
        "environment": environment_name,
        "git_sha": environment["git_sha"],
        "image_id": artifact["image_id"],
        "license_manifest_sha256": manifest["license_manifest_sha256"],
        "memory_limit_bytes": manifest["memory_limit_bytes"],
        "os_version": environment["os_version"],
        "peak_aggregate_rss_bytes": peak_aggregate_rss_bytes,
        "peak_process_group_rss_bytes": peak_process_group_rss_bytes,
        "problem_fingerprint": fingerprint(case.request.problem),
        "profile": profile,
        "protocol_version": BENCHMARK_PROTOCOL_V1,
        "python_version": artifact["python_version"],
        "request_fingerprint": case.request_fingerprint,
        "request_wall_ns": wall_ns,
        "sample_index": sample_index,
        "sample_kind": sample_kind,
        "schema_version": _RECORD_SCHEMA_V1,
        "semantic_fingerprint": "sha256:" + "0" * 64,
        "solver_name": solver,
        "solver_run": canonical_payload(run),
        "solver_version": artifact["solver_version"],
        "truth_method": case.truth_method,
        "worker_count": worker_count,
        "worker_id": run.worker_id,
        "worker_rebuild_count": rebuild_count,
        "worker_start_count": start_count,
    }
    record["semantic_fingerprint"] = _semantic_fingerprint(record)
    return record


@contextmanager
def _full_harness(harness_factory, solver: str):
    with harness_factory(solver) as harness:
        yield harness


def _run_full_environment(
    cases: tuple[BenchmarkCase, ...],
    manifest: Mapping[str, object],
    environment_name: str,
    harness_factory,
    *,
    progress=print,
    monotonic=time.monotonic,
) -> list[dict[str, object]]:
    cases_by_id = {case.case_id: case for case in cases}
    if len(cases_by_id) != len(cases):
        raise ValueError("full benchmark cases must have unique IDs")
    environment = manifest["environments"][environment_name]
    limits = BenchmarkLimits(
        manifest["soft_time_limit_ms"],
        manifest["hard_time_limit_ms"],
        manifest["memory_limit_bytes"],
        manifest["max_constraint_generation_rounds"],
    )
    records: list[dict[str, object]] = []
    total_samples = len(_full_sample_plan(cases)) * len(_SOLVERS)
    progress_started = monotonic()
    next_progress = progress_started + 60
    peak_rss_bytes = 0

    def checkpoint(case_id: str, sample_kind: str, sample_index: int, current_rss_bytes: int) -> None:
        nonlocal next_progress, peak_rss_bytes
        peak_rss_bytes = max(peak_rss_bytes, current_rss_bytes)
        now = monotonic()
        if now < next_progress:
            return
        progress(
            f"full benchmark progress phase={sample_kind} solver={solver} case={case_id} "
            f"sample={len(records)}/{total_samples} elapsed_seconds={int(now - progress_started)} "
            f"current_rss_bytes={current_rss_bytes} peak_rss_bytes={peak_rss_bytes}"
        )
        next_progress = now + 60

    for solver in _SOLVERS:
        artifact = manifest["solvers"][solver]["environments"][environment_name]
        container_id = "none" if environment_name == "macos" else artifact["image_id"]

        def submit(harness, case: BenchmarkCase, sample_kind: str, sample_index: int, worker_count: int, slot: int, request_limits: BenchmarkLimits):
            request_id = ":".join(
                ("full", environment_name, solver, sample_kind, str(worker_count), str(sample_index), case.case_id, str(slot))
            )
            outcome = harness.submit(WorkerRequest(request_id, solver, case.request, request_limits))
            run, checked = _solver_run_from_outcome(case, solver, outcome, environment, artifact)
            if not outcome.cleanup_proven:
                raise RuntimeError("CLEANUP_UNPROVEN")
            return outcome, run, checked

        def record(
            case: BenchmarkCase,
            outcome: WorkerOutcome,
            run: SolverRun,
            checked: DifferentialCheck,
            sample_kind: str,
            sample_index: int,
            worker_count: int,
            completed_requests: int,
            wall_ns: int,
            start_count: int,
            rebuild_count: int,
            process_group_rss_bytes: int,
            aggregate_rss_bytes: int,
            harness,
        ) -> dict[str, object]:
            return _benchmark_record(
                case, solver, outcome, run, checked, manifest, environment, artifact,
                wall_ns, start_count, rebuild_count,
                profile="full",
                environment_name=environment_name,
                corpus_version="canonical-48-v1+synthetic-v1+approved-v1",
                sample_kind=sample_kind,
                sample_index=sample_index,
                worker_count=worker_count,
                completed_requests=completed_requests,
                container_id=getattr(harness, "container_id", container_id),
                peak_process_group_rss_bytes=process_group_rss_bytes,
                peak_aggregate_rss_bytes=aggregate_rss_bytes,
            )

        with ExitStack() as stack:
            warm_harness = stack.enter_context(_full_harness(harness_factory, solver))
            throughput_harnesses = {}
            for case_id, sample_kind, sample_index, worker_count in _full_sample_plan(cases):
                case = cases_by_id[case_id]
                if sample_kind in {"warmup", "warm"}:
                    started_ns = time.perf_counter_ns()
                    outcome, run, checked = submit(
                        warm_harness, case, sample_kind, sample_index, worker_count, 0, limits
                    )
                    wall_ns = time.perf_counter_ns() - started_ns
                    peak = outcome.peak_rss_kib * 1024
                    records.append(
                        record(
                            case, outcome, run, checked, sample_kind, sample_index, worker_count, 1,
                            wall_ns, warm_harness.start_count, warm_harness.rebuild_count, peak, peak, warm_harness,
                        )
                    )
                    checkpoint(case.case_id, sample_kind, sample_index, peak)
                elif sample_kind == "throughput":
                    harnesses = []
                    for slot in range(worker_count):
                        harness = throughput_harnesses.get(slot)
                        if harness is None:
                            harness = stack.enter_context(_full_harness(harness_factory, solver))
                            throughput_harnesses[slot] = harness
                        harnesses.append(harness)
                    started_ns = time.perf_counter_ns()
                    with ThreadPoolExecutor(max_workers=worker_count) as executor:
                        batch = list(
                            executor.map(
                                lambda slot: submit(
                                    harnesses[slot], case, sample_kind, sample_index, worker_count, slot, limits
                                ),
                                range(worker_count),
                            )
                    )
                    wall_ns = time.perf_counter_ns() - started_ns
                    terminal_index = next(
                        (index for index, item in enumerate(batch) if item[2].hard_failure or item[1].termination_reason.value in _FATAL_TERMINATIONS),
                        None,
                    )
                    if terminal_index is None:
                        fingerprints = {
                            _semantic_fingerprint(
                                {
                                    "solver_run": canonical_payload(run),
                                    "check_hard_failure": checked.hard_failure,
                                    "check_failure_reason": None if checked.failure_reason is None else checked.failure_reason.value,
                                }
                            )
                            for _, run, checked in batch
                        }
                        if len(fingerprints) != 1:
                            raise RuntimeError("SEMANTIC_NONDETERMINISM")
                    selected_index = terminal_index if terminal_index is not None else 0
                    outcome, run, checked = batch[selected_index]
                    peaks = [item[0].peak_rss_kib * 1024 for item in batch]
                    records.append(
                        record(
                            case, outcome, run, checked, sample_kind, sample_index, worker_count, worker_count,
                            wall_ns, max(harness.start_count for harness in harnesses),
                            max(harness.rebuild_count for harness in harnesses), max(peaks), sum(peaks), harnesses[selected_index],
                        )
                    )
                    checkpoint(case.case_id, sample_kind, sample_index, sum(peaks))
                elif sample_kind == "cold":
                    with _full_harness(harness_factory, solver) as harness:
                        started_ns = time.perf_counter_ns()
                        outcome, run, checked = submit(
                            harness, case, sample_kind, sample_index, worker_count, 0, limits
                        )
                        wall_ns = time.perf_counter_ns() - started_ns
                    peak = outcome.peak_rss_kib * 1024
                    records.append(
                        record(
                            case, outcome, run, checked, sample_kind, sample_index, worker_count, 1,
                            wall_ns, harness.start_count, harness.rebuild_count, peak, peak, harness,
                        )
                    )
                    checkpoint(case.case_id, sample_kind, sample_index, peak)
                elif sample_kind == "rebuild":
                    with _full_harness(harness_factory, solver) as harness:
                        prime_outcome, prime_run, prime_checked = submit(
                            harness, case, "rebuild-prime", sample_index, worker_count, 0,
                            BenchmarkLimits(
                                limits.soft_time_limit_ms,
                                limits.hard_time_limit_ms,
                                (1 << 40) - 1,
                                limits.max_constraint_generation_rounds,
                            ),
                        )
                        if prime_checked.hard_failure or prime_run.termination_reason.value in _FATAL_TERMINATIONS:
                            outcome, run, checked = prime_outcome, prime_run, prime_checked
                            wall_ns = 0
                        else:
                            started_ns = time.perf_counter_ns()
                            outcome, run, checked = submit(
                                harness, case, sample_kind, sample_index, worker_count, 0, limits
                            )
                            wall_ns = time.perf_counter_ns() - started_ns
                    if harness.rebuild_count < 1 and not (checked.hard_failure or run.termination_reason.value in _FATAL_TERMINATIONS):
                        raise RuntimeError("WORKER_REBUILD_UNPROVEN")
                    peak = outcome.peak_rss_kib * 1024
                    records.append(
                        record(
                            case, outcome, run, checked, sample_kind, sample_index, worker_count, 1,
                            wall_ns, harness.start_count, harness.rebuild_count, peak, peak, harness,
                        )
                    )
                    checkpoint(case.case_id, sample_kind, sample_index, peak)
                else:
                    raise ValueError(f"unsupported full sample kind: {sample_kind}")
                if _hard_elimination_record(records[-1]):
                    break
    return records


def _quick_replay_identity(manifest: Mapping[str, object]) -> object:
    return {
        "corpus_manifest_sha256": manifest["corpus_manifest_sha256"],
        "environments": manifest["environments"],
        "required_case_ids": manifest["required_case_ids"],
        "solvers": manifest["solvers"],
    }


def _quick_semantics(records: list[dict[str, object]]) -> dict[tuple[str, str], tuple[object, ...]]:
    return {
        (record["solver_name"], record["case_id"]): (
            record["semantic_fingerprint"],
            record["solver_run"]["termination_reason"],
            record["check_hard_failure"],
            record["check_failure_reason"],
        )
        for record in records
    }


def _run_quick_benchmark(output_root: Path = _QUICK_OUTPUT, env_root: Path = _BENCHMARK_ENVS) -> int:
    discovered = _discover_quick_environment(Path(env_root))
    if discovered is None:
        print("BLOCKED_MISSING_ENVIRONMENT")
        return 2
    environment, artifacts = discovered
    cases = _load_quick_cases()
    manifest = _quick_manifest(cases, environment, artifacts)
    limits = BenchmarkLimits(
        QUICK_SOFT_TIME_LIMIT_MS,
        QUICK_HARD_TIME_LIMIT_MS,
        QUICK_MEMORY_LIMIT_BYTES,
        QUICK_MAX_CONSTRAINT_GENERATION_ROUNDS,
    )
    records: list[dict[str, object]] = []
    for solver in _SOLVERS:
        python = Path(env_root) / solver / "bin" / "python"
        command = [str(python), "-m", "open_trader.prediction_solver_worker", "--backend", solver]
        with WorkerHarness(
            command,
            request_timeout_ms=QUICK_HARD_TIME_LIMIT_MS,
            startup_timeout_ms=5_000,
            env=_native_subprocess_env(python),
        ) as harness:
            for case in cases:
                started_ns = time.perf_counter_ns()
                outcome = harness.submit(WorkerRequest(case.case_id, solver, case.request, limits))
                wall_ns = time.perf_counter_ns() - started_ns
                run, checked = _solver_run_from_outcome(case, solver, outcome, environment, artifacts[solver])
                records.append(
                    _benchmark_record(
                        case, solver, outcome, run, checked, manifest, environment,
                        artifacts[solver], wall_ns, harness.start_count, harness.rebuild_count,
                        profile="quick",
                        environment_name="macos",
                        corpus_version="canonical-48-v1+synthetic-v1",
                        sample_kind="warm",
                        sample_index=0,
                        worker_count=1,
                        completed_requests=1,
                        container_id="none",
                        peak_process_group_rss_bytes=outcome.peak_rss_kib * 1024,
                        peak_aggregate_rss_bytes=outcome.peak_rss_kib * 1024,
                    )
                )
                if not outcome.cleanup_proven:
                    raise RuntimeError("CLEANUP_UNPROVEN")
    summary = aggregate_benchmark_records(records, manifest)
    hard_failures = {
        solver: summary["solvers"][solver]["hard_gate_failures"]
        for solver in _SOLVERS
        if summary["solvers"][solver]["hard_gate_failures"]
    }
    if hard_failures:
        raise RuntimeError(f"quick hard gate failed: {hard_failures}")
    output = Path(output_root)
    records_path = output / "records.jsonl"
    manifest_path = output / "manifest.json"
    replayed = False
    if records_path.is_file() or manifest_path.is_file():
        if not records_path.is_file() or not manifest_path.is_file():
            raise ValueError("quick replay baseline is incomplete")
        previous_manifest = _mapping(_strict_json(manifest_path.read_bytes(), "previous quick manifest"), "previous quick manifest")
        previous_records = [
            _mapping(_strict_json(line, f"previous quick record {index}"), f"previous quick record {index}")
            for index, line in enumerate(records_path.read_bytes().splitlines(), 1)
        ]
        aggregate_benchmark_records(previous_records, previous_manifest)
        if _quick_replay_identity(previous_manifest) == _quick_replay_identity(manifest):
            if _quick_semantics([dict(record) for record in previous_records]) != _quick_semantics(records):
                raise ValueError("quick semantic replay changed")
            replayed = True
    records_bytes = b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        for record in records
    )
    _atomic_write_bytes(records_path, records_bytes)
    _atomic_write_json(manifest_path, manifest)
    generate_benchmark_report([records_path], manifest_path, output / "artifacts")
    print("semantic replay PASS" if replayed else "quick baseline PASS")
    return 0


def _unavailable_linux_environment() -> dict[str, object]:
    return {
        "available": False,
        "architecture": "unavailable",
        "cpu": "unavailable",
        "environment_id": "linux:unavailable",
        "git_sha": "0" * 40,
        "os_version": "unavailable",
    }


def _unavailable_linux_artifact() -> dict[str, object]:
    return {
        "adapter_version": WORKER_VERSION,
        "build_id": "unavailable",
        "commercial_key_required": False,
        "image_id": "none",
        "install_succeeded": False,
        "installation_ns": 0,
        "license_evidence_present": False,
        "open_source": False,
        "python_version": "unavailable",
        "reuse_succeeded": False,
        "run_succeeded": False,
        "solver_version": "unavailable",
        "source_evidence_present": False,
    }


def _run_full_macos(output_root: Path = _FINAL_RESULTS, env_root: Path = _BENCHMARK_ENVS) -> int:
    output = Path(output_root)
    if output.is_symlink() or output.exists() and not output.is_dir():
        raise ValueError(f"full macOS output root is unsafe: {output}")
    records_path = output / "macos.jsonl"
    manifest_path = output / "environment_manifest.json"
    stale = [path for path in (records_path, output / "linux.jsonl", manifest_path) if path.exists() or path.is_symlink()]
    if stale:
        raise ValueError(f"full macOS output already exists: {stale[0]}")
    discovered = _discover_quick_environment(Path(env_root))
    if discovered is None:
        print("BLOCKED_MISSING_ENVIRONMENT")
        return 2
    macos_environment, macos_artifacts = discovered
    cases = _load_full_cases()
    linux_environment = _unavailable_linux_environment()
    linux_artifact = _unavailable_linux_artifact()
    manifest = _full_manifest(
        cases,
        {"macos": macos_environment, "linux": linux_environment},
        {
            solver: {"macos": macos_artifacts[solver], "linux": linux_artifact}
            for solver in _SOLVERS
        },
    )

    def harness_factory(solver: str) -> WorkerHarness:
        python = Path(env_root) / solver / "bin" / "python"
        return WorkerHarness(
            [str(python), "-m", "open_trader.prediction_solver_worker", "--backend", solver],
            request_timeout_ms=20_000,
            startup_timeout_ms=5_000,
            env=_native_subprocess_env(python),
        )

    records = _run_full_environment(cases, manifest, "macos", harness_factory)
    aggregate_benchmark_records(records, manifest)
    records_bytes = b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        for record in records
    )
    _atomic_write_bytes(records_path, records_bytes, overwrite=False)
    _atomic_write_json(manifest_path, manifest, overwrite=False)
    return 0


def _read_full_macos_partial(output: Path) -> tuple[dict[str, object], list[dict[str, object]], tuple[BenchmarkCase, ...]]:
    if output.is_symlink() or not output.is_dir():
        raise ValueError("macOS partial output is absent or unsafe")
    records_path = output / "macos.jsonl"
    manifest_path = output / "environment_manifest.json"
    linux_path = output / "linux.jsonl"
    if linux_path.exists() or linux_path.is_symlink():
        raise ValueError("linux benchmark evidence already exists")
    if any(path.is_symlink() or not path.is_file() for path in (records_path, manifest_path)):
        raise ValueError("macOS partial is absent or unsafe")
    try:
        raw_manifest = _mapping(_strict_json(manifest_path.read_bytes(), "macOS partial manifest"), "macOS partial manifest")
        manifest = _validated_run_manifest(raw_manifest)
        records = [
            _mapping(_strict_json(line, f"macOS partial record {index}"), f"macOS partial record {index}")
            for index, line in enumerate(records_path.read_bytes().splitlines(), 1)
        ]
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("macOS partial is invalid") from error
    if manifest["environments"]["linux"]["available"]:
        raise ValueError("macOS partial must not already contain Linux evidence")
    current = _discover_quick_environment(_BENCHMARK_ENVS)
    if current is None:
        raise ValueError("current macOS benchmark environment is unavailable")
    macos_environment, macos_artifacts = current
    cases = _load_full_cases()
    expected = _full_manifest(
        cases,
        {"macos": macos_environment, "linux": _unavailable_linux_environment()},
        {
            solver: {
                "macos": macos_artifacts[solver],
                "linux": _unavailable_linux_artifact(),
            }
            for solver in _SOLVERS
        },
    )
    submitted_identity = copy.deepcopy(raw_manifest)
    current_identity = copy.deepcopy(expected)
    for identity in (submitted_identity, current_identity):
        identity["environments"]["macos"].pop("git_sha")
        identity["environments"]["macos"].pop("environment_id")
    if submitted_identity != current_identity:
        raise ValueError("macOS partial identity does not match the current benchmark")
    try:
        aggregate_benchmark_records(records, raw_manifest)
    except ValueError as error:
        raise ValueError("macOS partial records are invalid") from error
    return raw_manifest, records, cases


def _run_full_linux(output_root: Path = _FINAL_RESULTS) -> int:
    output = Path(output_root)
    partial, macos_records, cases = _read_full_macos_partial(output)
    discovered = _discover_linux_environment()
    if discovered is None:
        print("BLOCKED_MISSING_ENVIRONMENT")
        return 2
    linux_environment, linux_artifacts = discovered
    manifest = copy.deepcopy(partial)
    manifest["environments"]["linux"] = linux_environment
    for solver in _SOLVERS:
        manifest["solvers"][solver]["environments"]["linux"] = linux_artifacts[solver]
    _validated_run_manifest(manifest)

    def harness_factory(solver: str) -> _DockerHarness:
        return _docker_harness(_text(manifest["solvers"][solver]["environments"]["linux"]["image_id"], "docker image ID"), solver)

    linux_records = _run_full_environment(cases, manifest, "linux", harness_factory)
    all_records = macos_records + linux_records
    aggregate_benchmark_records(all_records, manifest)
    records_bytes = b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        for record in linux_records
    )
    _atomic_write_bytes(output / "linux.jsonl", records_bytes, overwrite=False)
    _atomic_write_json(output / "environment_manifest.json", manifest)
    return 0


def _run_full_benchmark(environment: str) -> int:
    if not _load_approved_corpus(_APPROVED_CORPUS)["cases"]:
        print("BLOCKED_REAL_CORPUS_EMPTY")
        return 2
    if environment == "macos":
        return _run_full_macos()
    return _run_full_linux()


def _final_report_paths() -> tuple[list[Path], Path, Path]:
    return (
        [_FINAL_RESULTS / "macos.jsonl", _FINAL_RESULTS / "linux.jsonl"],
        _FINAL_RESULTS / "environment_manifest.json",
        _FINAL_RESULTS,
    )


def _require_final_inputs() -> tuple[list[Path], Path, Path]:
    records, manifest, output = _final_report_paths()
    if output.is_symlink() or any(path.is_symlink() for path in (*records, manifest)):
        raise ValueError("final Task 10 benchmark inputs are unsafe")
    missing = [path for path in (*records, manifest) if not path.is_file()]
    if missing:
        raise ValueError("final Task 10 benchmark inputs are absent")
    return records, manifest, output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prediction-solver-benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("quick")
    full = commands.add_parser("full")
    full.add_argument("--environment", required=True, choices=("macos", "linux"))
    commands.add_parser("report")
    commands.add_parser("verify-report")
    intake = commands.add_parser("import-approved")
    intake.add_argument("inbox", nargs="?", default=str(_BENCHMARK_ROOT / "inbox" / "approved_component.json"))
    intake.add_argument("corpus", nargs="?", default=str(_APPROVED_CORPUS))
    args = parser.parse_args(argv)
    try:
        if args.command == "quick":
            return _run_quick_benchmark()
        if args.command == "full":
            return _run_full_benchmark(args.environment)
        if args.command == "import-approved":
            import_approved_snapshot(args.inbox, args.corpus)
            return 0
        records, manifest, output = _require_final_inputs()
        if args.command == "report":
            generate_benchmark_report(records, manifest, output)
        else:
            verify_benchmark_report(records, manifest, output)
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
