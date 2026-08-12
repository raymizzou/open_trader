from __future__ import annotations

from dataclasses import replace

import pytest

from open_trader.prediction_solver import (
    BackendResult,
    BenchmarkClassification,
    BenchmarkLimits,
    CertificateEvidence,
    INT64_MAX,
    IntVariable,
    LinearConstraint,
    LinearModel,
    LinearObjective,
    NativeSolveStatus,
    SolverEvidence,
    SolverRun,
    TerminationReason,
    UnsafeSolverResult,
    linear_model_fingerprint,
    validate_backend_result,
    validate_linear_model,
)
from open_trader.prediction_n_leg import (
    BusinessStatus,
    ObjectiveBounds,
    OptimalityStatus,
    OracleResult,
    ProofStatus,
    SolveStatus,
    UnknownReason,
)


def valid_linear_model() -> LinearModel:
    return LinearModel(
        variables=(IntVariable("lots", 0, 4), IntVariable("reserve", -2, 3)),
        constraints=(LinearConstraint("budget", (("lots", 3), ("reserve", -2)), -6, 12),),
        objective=LinearObjective("MAX", (("lots", 5), ("reserve", -1))),
    )


@pytest.mark.parametrize(
    ("model", "reason"),
    (
        (
            replace(valid_linear_model(), variables=(IntVariable("lots", 0, 1), IntVariable("lots", 0, 1))),
            "duplicate variable name",
        ),
        (
            replace(valid_linear_model(), constraints=(LinearConstraint("budget", (("missing", 1),), None, None),)),
            "unknown variable reference",
        ),
        (
            replace(valid_linear_model(), variables=(IntVariable("lots", True, 1), IntVariable("reserve", -2, 3))),
            "boolean variable bound",
        ),
        (
            replace(valid_linear_model(), constraints=(LinearConstraint("budget", (("lots", 1),), 2, 1),)),
            "reversed row bounds",
        ),
        (
            replace(valid_linear_model(), constraints=(LinearConstraint("budget", (("lots", INT64_MAX + 1),), None, None),)),
            "out-of-range coefficient",
        ),
        (
            replace(valid_linear_model(), variables=(IntVariable("lots", 0, INT64_MAX + 1), IntVariable("reserve", -2, 3))),
            "out-of-range variable bound",
        ),
        (
            LinearModel((IntVariable("lots", 2, INT64_MAX),), (LinearConstraint("budget", (("lots", 2),), None, None),), None),
            "out-of-range possible row activity",
        ),
    ),
)
def test_linear_model_rejects_unsafe_integer_ir(model: LinearModel, reason: str) -> None:
    with pytest.raises(ValueError):
        validate_linear_model(model)


def test_linear_model_canonicalizes_coefficient_order_for_fingerprint() -> None:
    model = valid_linear_model()
    shuffled = replace(
        model,
        constraints=(LinearConstraint("budget", (("reserve", -2), ("lots", 3)), -6, 12),),
        objective=LinearObjective("MAX", (("reserve", -1), ("lots", 5))),
    )

    validate_linear_model(model)
    validate_linear_model(shuffled)

    assert shuffled.constraints[0].coefficients == model.constraints[0].coefficients
    assert shuffled.objective == model.objective
    assert linear_model_fingerprint(shuffled) == linear_model_fingerprint(model)


def test_backend_result_rejects_non_integer_incomplete_extra_and_infeasible_values() -> None:
    model = valid_linear_model()
    unsafe_results = (
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", True), ("reserve", 0)), 0, None, "feasible", 1),
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", 1),), 0, None, "feasible", 1),
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", 1), ("reserve", 0), ("extra", 0)), 0, None, "feasible", 1),
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", 5), ("reserve", 0)), 0, None, "feasible", 1),
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", 4), ("reserve", -2)), 0, None, "feasible", 1),
        BackendResult(NativeSolveStatus.FEASIBLE, (("lots", 2), ("reserve", -2)), 13, None, "feasible", 1),
    )

    for result in unsafe_results:
        with pytest.raises(UnsafeSolverResult):
            validate_backend_result(model, result)


def test_backend_result_accepts_exact_integer_values_that_satisfy_every_row() -> None:
    model = valid_linear_model()
    result = BackendResult(NativeSolveStatus.FEASIBLE, (("reserve", -2), ("lots", 2)), 12, None, "feasible", 1)

    validate_backend_result(model, result)


def valid_certificate() -> CertificateEvidence:
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


def valid_solver_evidence() -> SolverEvidence:
    return SolverEvidence(
        native_status="optimal",
        candidate=None,
        objective_bounds=ObjectiveBounds(None, None, None, False),
        worst_scenario=None,
        payout_lower_bound_units=None,
        cost_upper_bound_units=None,
        guaranteed_profit_units=None,
        conservative_capital_release_at=None,
        fixed_portfolio_closed=False,
        global_search_closed=False,
        master_rounds=0,
        adversary_rounds=0,
        cuts=(),
        certificate=None,
    )


def valid_solver_run(**changes: object) -> SolverRun:
    values: dict[str, object] = {
        "schema_version": "open_trader.prediction_solver.protocol.v1",
        "request_id": "request-1",
        "solver_name": "test-solver",
        "solver_version": "1.0",
        "adapter_version": "1.0",
        "worker_id": "worker-1",
        "environment_id": "test-env",
        "solve_status": SolveStatus.UNKNOWN,
        "proof_status": ProofStatus.UNKNOWN,
        "business_status": BusinessStatus.UNKNOWN,
        "optimality_status": OptimalityStatus.NOT_APPLICABLE,
        "objective_bounds": ObjectiveBounds(None, None, None, False),
        "classification": BenchmarkClassification.UNKNOWN,
        "termination_reason": TerminationReason.CRASH,
        "evidence": None,
        "canonical_result": None,
        "phase_timings_ns": (("decode", 0),),
        "peak_rss_bytes": 0,
        "diagnostics": (("error", "worker crashed"),),
    }
    values.update(changes)
    return SolverRun(**values)  # type: ignore[arg-type]


def unknown_oracle_result() -> OracleResult:
    return OracleResult(
        SolveStatus.UNKNOWN,
        ProofStatus.UNKNOWN,
        BusinessStatus.UNKNOWN,
        OptimalityStatus.NOT_APPLICABLE,
        ObjectiveBounds(None, None, None, False),
        None,
        None,
        UnknownReason.INVALID_MODEL,
    )


def test_benchmark_limits_and_certificate_evidence_reject_unsafe_values() -> None:
    assert BenchmarkLimits(1, 2, 3, 4).hard_time_limit_ms == 2
    assert valid_certificate().checker_succeeded is True

    with pytest.raises(ValueError):
        BenchmarkLimits(True, 2, 3, 4)
    with pytest.raises(ValueError):
        BenchmarkLimits(2, 1, 3, 4)
    with pytest.raises(ValueError):
        replace(valid_certificate(), certificate_sha256="sha256:" + "A" * 64)
    with pytest.raises(ValueError):
        replace(valid_certificate(), completed_certificate_size_bytes=None)
    with pytest.raises(ValueError):
        replace(valid_certificate(), checker_succeeded=True, checker_exit_code=1)


def test_solver_run_requires_checked_canonical_result_and_matching_axes() -> None:
    with pytest.raises(AssertionError):
        valid_solver_run(
            classification=BenchmarkClassification.CHECKED,
            canonical_result=None,
        )
    with pytest.raises(AssertionError):
        valid_solver_run(
            classification=BenchmarkClassification.MEASUREMENT_ONLY,
            canonical_result=unknown_oracle_result(),
        )
    checked = valid_solver_run(
        classification=BenchmarkClassification.CHECKED,
        canonical_result=unknown_oracle_result(),
    )
    assert checked.canonical_result == unknown_oracle_result()
    with pytest.raises(AssertionError):
        valid_solver_run(
            classification=BenchmarkClassification.CHECKED,
            canonical_result=unknown_oracle_result(),
            solve_status=SolveStatus.FEASIBLE,
        )


def test_solver_run_certificate_and_unknown_classifications_stay_fail_closed() -> None:
    certificate_checked = valid_solver_run(
        classification=BenchmarkClassification.CERTIFICATE_CHECKED,
        termination_reason=TerminationReason.COMPLETED,
        evidence=replace(valid_solver_evidence(), certificate=valid_certificate()),
    )
    assert certificate_checked.evidence is not None
    with pytest.raises(AssertionError):
        valid_solver_run(
            classification=BenchmarkClassification.CERTIFICATE_CHECKED,
            evidence=valid_solver_evidence(),
        )
    with pytest.raises(AssertionError):
        valid_solver_run(
            classification=BenchmarkClassification.UNKNOWN,
            solve_status=SolveStatus.FEASIBLE,
        )
    with pytest.raises(ValueError):
        valid_solver_run(
            classification=BenchmarkClassification.MEASUREMENT_ONLY,
            proof_status=ProofStatus.PROVEN,
        )
