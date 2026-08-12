"""Optional native adapters for the solver benchmark.

The benchmark keeps vendor packages out of the application environment.  This
module therefore imports HiGHS only when an adapter is used.
"""

from __future__ import annotations

import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import importlib
import json
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from open_trader.prediction_solver import (
    BackendResult,
    INT64_MAX,
    INT64_MIN,
    LinearModel,
    NativeSolveStatus,
    UnsafeSolverResult,
    validate_backend_result,
    validate_linear_model,
)


DOUBLE_INT_MIN = -(2**53)
DOUBLE_INT_MAX = 2**53


def _highspy() -> Any:
    return importlib.import_module("highspy")


def _pyscipopt() -> Any:
    return importlib.import_module("pyscipopt")


def _integer_value(value: object, name: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise UnsafeSolverResult(f"HiGHS returned a non-numeric {name}") from exc
    if not math.isfinite(number):
        raise UnsafeSolverResult(f"HiGHS returned a non-finite {name}")
    rounded = round(number)
    if abs(number - rounded) > 1e-6:
        raise UnsafeSolverResult(f"HiGHS returned a non-integral {name}")
    if not INT64_MIN <= rounded <= INT64_MAX:
        raise UnsafeSolverResult(f"HiGHS returned an out-of-range {name}")
    return int(rounded)


def _optional_integer_value(value: object, name: str) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    rounded = round(number)
    if abs(number - rounded) > 1e-6 or not INT64_MIN <= rounded <= INT64_MAX:
        return None
    return int(rounded)


def _native_integer(value: int, name: str) -> float:
    number = float(value)
    if number != value:
        raise UnsafeSolverResult(f"HiGHS cannot represent {name} exactly")
    return number


def _validate_native_precision(model: LinearModel) -> None:
    variables = {variable.name: variable for variable in model.variables}

    def validate_activity(owner: str, terms: tuple[tuple[str, int], ...]) -> None:
        minimum = maximum = 0
        absolute_total = 0
        for name, coefficient in terms:
            variable = variables[name]
            lower_product = coefficient * variable.lower
            upper_product = coefficient * variable.upper
            contribution = max(abs(lower_product), abs(upper_product))
            if contribution > DOUBLE_INT_MAX:
                raise UnsafeSolverResult(f"HiGHS possible {owner} activity term contribution exceeds exact double integer range")
            absolute_total += contribution
            if absolute_total > DOUBLE_INT_MAX:
                raise UnsafeSolverResult(f"HiGHS possible {owner} activity term accumulation exceeds exact double integer range")
            minimum += lower_product if coefficient >= 0 else upper_product
            maximum += upper_product if coefficient >= 0 else lower_product
        if minimum < DOUBLE_INT_MIN or maximum > DOUBLE_INT_MAX:
            raise UnsafeSolverResult(f"HiGHS possible {owner} activity exceeds exact double integer range")

    for variable in model.variables:
        _native_integer(variable.lower, f"{variable.name}.lower")
        _native_integer(variable.upper, f"{variable.name}.upper")
    for constraint in model.constraints:
        for name, coefficient in constraint.coefficients:
            _native_integer(coefficient, f"{constraint.name}.{name}")
        if constraint.lower is not None:
            _native_integer(constraint.lower, f"{constraint.name}.lower")
        if constraint.upper is not None:
            _native_integer(constraint.upper, f"{constraint.name}.upper")
        validate_activity(f"row {constraint.name}", constraint.coefficients)
    if model.objective is not None:
        for name, coefficient in model.objective.coefficients:
            _native_integer(coefficient, f"objective.{name}")
        validate_activity("objective", model.objective.coefficients)


SCIP_VERSION = "10.0.2"
PYSCIPOPT_VERSION = "6.2.1"
VIPR_VERSION = "30f2951d1e90e47afa821bdd1b12b82246656c42"
VIPR_MAX_CERTIFICATE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ViprCheckResult:
    certificate_sha256: str | None
    certificate_size_bytes: int | None
    completed_certificate_sha256: str | None
    completed_certificate_size_bytes: int | None
    checker_name: str
    checker_version: str
    checker_exit_code: int | None
    checker_succeeded: bool
    completion_exit_code: int | None
    completion_ns: int
    check_ns: int
    error: str | None = None
    generation_ns: int = 0


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _artifact_path(artifact_dir: str | os.PathLike[str], path: str | os.PathLike[str]) -> Path:
    root = Path(artifact_dir).resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("certificate path must stay inside the request artifact directory") from exc
    return resolved


def _validate_executable_path(path: str | os.PathLike[str], artifact_dir: Path) -> str:
    raw = os.fspath(path)
    resolved = Path(raw).resolve() if os.sep in raw else Path(shutil.which(raw) or raw).resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(f"checker executable is unavailable: {path}")
    return str(resolved)


def _command_parts(command: str | os.PathLike[str] | tuple[str, ...] | list[str]) -> list[str]:
    if isinstance(command, (str, os.PathLike)):
        return [os.fspath(command)]
    return [os.fspath(part) for part in command]


def _run_vipr_process(command: list[str], *, cwd: Path, timeout_ms: int) -> tuple[int | None, int, str | None]:
    started_ns = time.perf_counter_ns()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return None, max(1, time.perf_counter_ns() - started_ns), str(exc)
    try:
        returncode = process.wait(timeout=timeout_ms / 1_000)
        return returncode, max(1, time.perf_counter_ns() - started_ns), None
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 15)
            process.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return None, max(1, time.perf_counter_ns() - started_ns), "VIPR subprocess timed out"


def _scip_cli() -> str | None:
    configured = os.environ.get("SCIP_BINARY")
    if configured:
        return configured
    scipopt_dir = os.environ.get("SCIPOPTDIR")
    if scipopt_dir:
        candidate = Path(scipopt_dir) / "bin" / "scip"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("scip")


def check_vipr_certificate(
    certificate_path: str | os.PathLike[str],
    artifact_dir: str | os.PathLike[str],
    *,
    viprcomp: str | os.PathLike[str] | tuple[str, ...] | list[str] = "viprcomp",
    viprchk: str | os.PathLike[str] | tuple[str, ...] | list[str] = "viprchk",
    timeout_ms: int = 30_000,
) -> ViprCheckResult:
    """Complete and independently check one request-scoped VIPR certificate."""
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise ValueError("timeout_ms must be a positive integer")
    root = Path(artifact_dir).resolve()
    certificate = _artifact_path(root, certificate_path)
    if not certificate.is_file():
        return ViprCheckResult(None, None, None, None, "viprchk", VIPR_VERSION, None, False, None, 0, 0, "certificate is missing")
    try:
        completion_command = [_validate_executable_path(part, root) if index == 0 else os.fspath(part) for index, part in enumerate(_command_parts(viprcomp))]
        checker_command = [_validate_executable_path(part, root) if index == 0 else os.fspath(part) for index, part in enumerate(_command_parts(viprchk))]
    except ValueError as exc:
        return ViprCheckResult(None, None, None, None, "viprchk", VIPR_VERSION, None, False, None, 0, 0, str(exc))
    certificate_sha256, certificate_size = _sha256_file(certificate)
    completed = _artifact_path(root, certificate.with_name(f"{certificate.stem}_complete{certificate.suffix or '.vipr'}"))
    completed.unlink(missing_ok=True)
    completion_exit_code, completion_ns, completion_error = _run_vipr_process(
        [*completion_command, "--threads=1", str(certificate)], cwd=root, timeout_ms=timeout_ms
    )
    if completion_error is not None or completion_exit_code != 0 or not completed.is_file():
        return ViprCheckResult(
            certificate_sha256,
            certificate_size,
            *_sha256_file(completed) if completed.is_file() else (None, None),
            "viprchk",
            VIPR_VERSION,
            completion_exit_code,
            False,
            completion_exit_code,
            completion_ns,
            0,
            completion_error or "viprcomp failed",
        )

    completed_sha256, completed_size = _sha256_file(completed)
    checker_exit_code, check_ns, checker_error = _run_vipr_process(
        [*checker_command, str(completed)], cwd=root, timeout_ms=timeout_ms
    )
    return ViprCheckResult(
        certificate_sha256,
        certificate_size,
        completed_sha256,
        completed_size,
        "viprchk",
        VIPR_VERSION,
        checker_exit_code,
        checker_error is None and checker_exit_code == 0,
        completion_exit_code,
        completion_ns,
        check_ns,
        checker_error,
    )


@dataclass(frozen=True, slots=True)
class _ViprClaim:
    status: NativeSolveStatus
    values: tuple[tuple[str, int], ...]
    objective_value: int | None
    objective_bound: int | None


def _vipr_fraction(token: str) -> Fraction:
    if re.fullmatch(r"[+-]?\d+(?:/\d+)?", token) is None:
        raise ValueError(f"invalid VIPR rational: {token}")
    try:
        return Fraction(token)
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValueError(f"invalid VIPR rational: {token}") from exc


def _vipr_int(token: str, name: str) -> int:
    try:
        value = int(token)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid VIPR {name}: {token}") from exc
    return value


def _vipr_terms(tokens: list[str], position: int, *, variable_count: int) -> tuple[dict[int, Fraction], int]:
    if position >= len(tokens):
        raise ValueError("VIPR coefficient list is missing")
    count_token = tokens[position]
    position += 1
    if count_token == "OBJ":
        raise ValueError("VIPR coefficient list unexpectedly aliases OBJ")
    count = _vipr_int(count_token, "coefficient count")
    if count < 0:
        raise ValueError("VIPR coefficient count must be nonnegative")
    terms: dict[int, Fraction] = {}
    for _ in range(count):
        if position + 1 >= len(tokens):
            raise ValueError("VIPR coefficient pair is incomplete")
        index = _vipr_int(tokens[position], "coefficient index")
        value = _vipr_fraction(tokens[position + 1])
        position += 2
        if not 0 <= index < variable_count:
            raise ValueError("VIPR coefficient index is out of range")
        if index in terms:
            raise ValueError("VIPR coefficient index is duplicated")
        terms[index] = value
    return terms, position


def _vipr_row(sense: str, rhs: Fraction, terms: dict[int, Fraction]) -> tuple[str, Fraction, tuple[tuple[int, Fraction], ...]]:
    if sense not in {"E", "L", "G"}:
        raise ValueError(f"invalid VIPR constraint sense: {sense}")
    return sense, rhs, tuple(sorted(terms.items()))


def _model_vipr_rows(model: LinearModel) -> tuple[tuple[str, Fraction, tuple[tuple[int, Fraction], ...]], ...]:
    indexes = {variable.name: index for index, variable in enumerate(model.variables)}
    rows: list[tuple[str, Fraction, tuple[tuple[int, Fraction], ...]]] = []
    for constraint in model.constraints:
        terms = {indexes[name]: Fraction(coefficient) for name, coefficient in constraint.coefficients if coefficient}
        if constraint.lower is not None:
            rows.append(_vipr_row("G", Fraction(constraint.lower), terms))
        if constraint.upper is not None:
            rows.append(_vipr_row("L", Fraction(constraint.upper), terms))
    for index, variable in enumerate(model.variables):
        rows.append(_vipr_row("G", Fraction(variable.lower), {index: Fraction(1)}))
        rows.append(_vipr_row("L", Fraction(variable.upper), {index: Fraction(1)}))
    return tuple(rows)


def _parse_vipr_claim(certificate_path: Path, model: LinearModel) -> _ViprClaim:
    try:
        if certificate_path.stat().st_size > VIPR_MAX_CERTIFICATE_BYTES:
            raise ValueError("VIPR certificate exceeds the parser size limit")
        tokens = certificate_path.read_text(encoding="ascii").split()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"VIPR certificate cannot be read: {exc}") from exc
    position = 0

    def take(name: str) -> str:
        nonlocal position
        if position >= len(tokens):
            raise ValueError(f"VIPR {name} is missing")
        token = tokens[position]
        position += 1
        return token

    if take("version marker") != "VER" or take("version") not in {"1.0", "1.1"}:
        raise ValueError("unsupported VIPR certificate version")
    if take("VAR marker") != "VAR":
        raise ValueError("VIPR VAR section is missing")
    variable_count = _vipr_int(take("variable count"), "variable count")
    if variable_count != len(model.variables) or variable_count < 0:
        raise ValueError("VIPR variable count does not match the model")
    names = tuple(take("variable name") for _ in range(variable_count))
    if names != tuple(f"t_v{index}" for index in range(variable_count)):
        raise ValueError("VIPR variable names do not match the internal model")

    if take("INT marker") != "INT":
        raise ValueError("VIPR INT section is missing")
    integer_count = _vipr_int(take("integer count"), "integer count")
    integer_indexes = tuple(_vipr_int(take("integer index"), "integer index") for _ in range(integer_count))
    if integer_count != variable_count or tuple(sorted(integer_indexes)) != tuple(range(variable_count)):
        raise ValueError("VIPR INT section does not mark every variable exactly once")

    if take("OBJ marker") != "OBJ":
        raise ValueError("VIPR OBJ section is missing")
    objective_sense = take("objective sense")
    if objective_sense != "min":
        raise ValueError("VIPR objective sense must be min")
    objective_terms, position = _vipr_terms(tokens, position, variable_count=variable_count)

    if take("CON marker") != "CON":
        raise ValueError("VIPR CON section is missing")
    constraint_count = _vipr_int(take("constraint count"), "constraint count")
    bound_count = _vipr_int(take("bound count"), "bound count")
    if constraint_count < 0 or bound_count < 0:
        raise ValueError("VIPR CON counts must be nonnegative")
    certificate_rows: list[tuple[str, Fraction, tuple[tuple[int, Fraction], ...]]] = []
    for _ in range(constraint_count):
        take("constraint label")
        sense = take("constraint sense")
        rhs = _vipr_fraction(take("constraint rhs"))
        terms, position = _vipr_terms(tokens, position, variable_count=variable_count)
        certificate_rows.append(_vipr_row(sense, rhs, terms))

    if take("RTP marker") != "RTP":
        raise ValueError("VIPR RTP section is missing")
    relation = take("RTP relation")
    lower: Fraction | None = None
    upper: Fraction | None = None
    if relation == "infeas":
        pass
    elif relation == "range":
        lower_token = take("RTP lower bound")
        upper_token = take("RTP upper bound")
        if lower_token != "-inf":
            lower = _vipr_fraction(lower_token)
        if upper_token != "inf":
            upper = _vipr_fraction(upper_token)
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("VIPR RTP bounds are reversed")
    else:
        raise ValueError(f"unsupported VIPR RTP relation: {relation}")

    if take("SOL marker") != "SOL":
        raise ValueError("VIPR SOL section is missing")
    solution_count = _vipr_int(take("solution count"), "solution count")
    if solution_count < 0:
        raise ValueError("VIPR solution count must be nonnegative")
    solutions: list[tuple[Fraction, ...]] = []
    for _ in range(solution_count):
        take("solution label")
        terms, position = _vipr_terms(tokens, position, variable_count=variable_count)
        values = [Fraction(0) for _ in range(variable_count)]
        for index, value in terms.items():
            if value.denominator != 1:
                raise ValueError("VIPR solution contains a noninteger value")
            values[index] = value
        solutions.append(tuple(values))

    if take("DER marker") != "DER":
        raise ValueError("VIPR DER section is missing")
    derivation_count = _vipr_int(take("derivation count"), "derivation count")
    if derivation_count < 0:
        raise ValueError("VIPR derivation count must be nonnegative")

    indexes = {variable.name: index for index, variable in enumerate(model.variables)}
    objective_sign = -1 if model.objective is not None and model.objective.sense == "MAX" else 1
    expected_objective = {
        indexes[name]: Fraction(objective_sign * coefficient)
        for name, coefficient in (model.objective.coefficients if model.objective is not None else ())
        if coefficient
    }
    if objective_terms != expected_objective:
        raise ValueError("VIPR objective does not match the exact model")
    expected_rows = _model_vipr_rows(model)
    if constraint_count != len(expected_rows) or bound_count != 2 * len(model.variables):
        raise ValueError("VIPR constraint counts do not match the exact model")
    if Counter(certificate_rows) != Counter(expected_rows):
        raise ValueError("VIPR constraints do not match the exact model")

    if relation == "infeas":
        if solutions:
            raise ValueError("VIPR infeasibility claim unexpectedly includes a solution")
        return _ViprClaim(NativeSolveStatus.INFEASIBLE, (), None, None)
    if lower is None or upper is None or lower != upper or lower.denominator != 1:
        raise ValueError("VIPR optimality claim must have one finite integral RTP value")
    if not solutions:
        raise ValueError("VIPR optimality claim has no solution")
    target = lower
    for values in solutions:
        if any(value.denominator != 1 for value in values):
            continue
        if any(value < variable.lower or value > variable.upper for value, variable in zip(values, model.variables, strict=True)):
            continue
        if any(
            not (
                (constraint.lower is None or sum(Fraction(coefficient) * values[indexes[name]] for name, coefficient in constraint.coefficients) >= constraint.lower)
                and (constraint.upper is None or sum(Fraction(coefficient) * values[indexes[name]] for name, coefficient in constraint.coefficients) <= constraint.upper)
            )
            for constraint in model.constraints
        ):
            continue
        if sum(coefficient * values[index] for index, coefficient in objective_terms.items()) != target:
            continue
        original_value = int(target) * objective_sign if model.objective is not None else None
        original_bound = original_value
        named_values = tuple((variable.name, int(values[index])) for index, variable in enumerate(model.variables))
        return _ViprClaim(NativeSolveStatus.OPTIMAL, named_values, original_value, original_bound)
    raise ValueError("VIPR solutions do not satisfy the exact model claim")


class HighsBackend:
    """Thin translation layer from the benchmark integer IR to HiGHS."""

    name = "highs"
    version = "1.15.1"

    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        validate_linear_model(model)
        _validate_native_precision(model)
        if isinstance(time_limit_ms, bool) or not isinstance(time_limit_ms, int) or time_limit_ms <= 0:
            raise ValueError("time_limit_ms must be a positive integer")

        highs = _highspy()
        solver = highs.Highs()
        solver.setOptionValue("output_flag", False)
        solver.setOptionValue("log_to_console", False)
        solver.setOptionValue("threads", 1)
        solver.setOptionValue("random_seed", 4901)
        solver.setOptionValue("time_limit", time_limit_ms / 1_000)

        variable_indexes: dict[str, int] = {}
        objective_coefficients = dict(model.objective.coefficients) if model.objective is not None else {}
        for index, variable in enumerate(model.variables):
            solver.addVariable(
                lb=_native_integer(variable.lower, f"{variable.name}.lower"),
                ub=_native_integer(variable.upper, f"{variable.name}.upper"),
                obj=_native_integer(objective_coefficients.get(variable.name, 0), f"objective.{variable.name}"),
                type=highs.HighsVarType.kInteger,
                name=variable.name,
            )
            variable_indexes[variable.name] = index

        infinity = getattr(highs, "kHighsInf", float("inf"))
        for constraint in model.constraints:
            indices = [variable_indexes[name] for name, _ in constraint.coefficients]
            coefficients = [_native_integer(coefficient, f"{constraint.name}.{name}") for name, coefficient in constraint.coefficients]
            lower = -infinity if constraint.lower is None else _native_integer(constraint.lower, f"{constraint.name}.lower")
            upper = infinity if constraint.upper is None else _native_integer(constraint.upper, f"{constraint.name}.upper")
            solver.addRow(lower, upper, len(indices), indices, coefficients)

        if model.objective is not None:
            if model.objective.sense == "MAX":
                solver.setMaximize()
            else:
                solver.setMinimize()

        started_ns = time.perf_counter_ns()
        solver.run()
        solve_ns = time.perf_counter_ns() - started_ns
        model_status = solver.getModelStatus()
        native_status = str(solver.modelStatusToString(model_status))
        solution = solver.getSolution()

        status = NativeSolveStatus.UNKNOWN
        values: tuple[tuple[str, int], ...] = ()
        objective_value: int | None = None
        info = solver.getInfo()
        objective_bound = (
            _optional_integer_value(getattr(info, "mip_dual_bound", None), "objective bound")
            if model.objective is not None
            else None
        )

        if model_status == highs.HighsModelStatus.kInfeasible:
            status = NativeSolveStatus.INFEASIBLE
        elif model_status == highs.HighsModelStatus.kOptimal:
            status = NativeSolveStatus.OPTIMAL
        elif bool(getattr(solution, "value_valid", False)):
            status = NativeSolveStatus.FEASIBLE

        if bool(getattr(solution, "value_valid", False)):
            native_values = tuple(getattr(solution, "col_value", ()))
            if len(native_values) != len(model.variables):
                raise UnsafeSolverResult("HiGHS returned the wrong number of variable values")
            values = tuple(
                (variable.name, _integer_value(value, f"value for {variable.name}"))
                for variable, value in zip(model.variables, native_values, strict=True)
            )
            if model.objective is not None:
                values_by_name = dict(values)
                objective_value = sum(values_by_name[name] * coefficient for name, coefficient in model.objective.coefficients)

        result = BackendResult(status, values, objective_value, objective_bound, native_status, solve_ns)
        validate_backend_result(model, result)
        return result


@dataclass(frozen=True, slots=True)
class ScipBackendResult(BackendResult):
    certificate: ViprCheckResult | None = None


class ScipBackend:
    """Thin SCIP adapter with optional independently checked VIPR evidence."""

    name = "scip"
    version = SCIP_VERSION

    def __init__(self) -> None:
        self.certificate: ViprCheckResult | None = None

    @staticmethod
    def _native_number(value: int, name: str, *, formal: bool) -> int | float:
        return value if formal else _native_integer(value, name)

    @staticmethod
    def _unknown(native_status: str, solve_ns: int, *, certificate: ViprCheckResult | None = None) -> ScipBackendResult:
        return ScipBackendResult(NativeSolveStatus.UNKNOWN, (), None, None, native_status, solve_ns, certificate)

    def solve(
        self,
        model: LinearModel,
        *,
        time_limit_ms: int,
        formal: bool = False,
        artifact_dir: str | os.PathLike[str] | None = None,
        certificate_path: str | os.PathLike[str] | None = None,
        checker_timeout_ms: int = 30_000,
    ) -> BackendResult:
        validate_linear_model(model)
        _validate_native_precision(model)
        if isinstance(time_limit_ms, bool) or not isinstance(time_limit_ms, int) or time_limit_ms <= 0:
            raise ValueError("time_limit_ms must be a positive integer")
        if not isinstance(formal, bool):
            raise ValueError("formal must be a bool")
        self.certificate = None
        if formal:
            return self._solve_formal(
                model,
                time_limit_ms=time_limit_ms,
                artifact_dir=artifact_dir,
                certificate_path=certificate_path,
                checker_timeout_ms=checker_timeout_ms,
            )

        try:
            scip = _pyscipopt()
            solver = scip.Model()
            hide_output = getattr(solver, "hideOutput", None)
            if hide_output is not None:
                hide_output()
            else:
                solver.setParam("display/verblevel", 0)
            solver.setParam("parallel/maxnthreads", 1)
            solver.setParam("randomization/randomseedshift", 4901)
            solver.setParam("limits/time", time_limit_ms / 1_000)
            variable_indexes: dict[str, Any] = {}
            objective_coefficients = dict(model.objective.coefficients) if model.objective is not None else {}
            for variable in model.variables:
                variable_indexes[variable.name] = solver.addVar(
                    name=variable.name,
                    vtype="I",
                    lb=self._native_number(variable.lower, f"{variable.name}.lower", formal=False),
                    ub=self._native_number(variable.upper, f"{variable.name}.upper", formal=False),
                    obj=self._native_number(objective_coefficients.get(variable.name, 0), f"objective.{variable.name}", formal=False),
                )
            for constraint in model.constraints:
                expression: Any = 0
                for name, coefficient in constraint.coefficients:
                    expression += self._native_number(coefficient, f"{constraint.name}.{name}", formal=False) * variable_indexes[name]
                if constraint.lower is not None:
                    solver.addCons(expression >= self._native_number(constraint.lower, f"{constraint.name}.lower", formal=False), name=f"{constraint.name}:lower")
                if constraint.upper is not None:
                    solver.addCons(expression <= self._native_number(constraint.upper, f"{constraint.name}.upper", formal=False), name=f"{constraint.name}:upper")
            if model.objective is not None:
                solver.setMaximize() if model.objective.sense == "MAX" else solver.setMinimize()
        except Exception as exc:
            return self._unknown(f"SCIP_UNAVAILABLE: {exc}", 0)

        started_ns = time.perf_counter_ns()
        try:
            solver.optimize()
            solve_ns = max(1, time.perf_counter_ns() - started_ns)
            native_status = str(solver.getStatus())
        except Exception as exc:
            solve_ns = max(1, time.perf_counter_ns() - started_ns)
            return self._unknown(f"SCIP_EXCEPTION: {exc}", solve_ns)

        if native_status == "infeasible":
            result = ScipBackendResult(NativeSolveStatus.INFEASIBLE, (), None, None, native_status, solve_ns)
        elif native_status == "optimal":
            try:
                solution = solver.getBestSol()
                if solution is None:
                    result = self._unknown(native_status, solve_ns)
                else:
                    values = tuple(
                        (variable.name, _integer_value(solver.getSolVal(solution, variable_indexes[variable.name]), f"value for {variable.name}"))
                        for variable in model.variables
                    )
                    values_by_name = dict(values)
                    objective_value = (
                        sum(values_by_name[name] * coefficient for name, coefficient in model.objective.coefficients)
                        if model.objective is not None
                        else None
                    )
                    objective_bound = (
                        _optional_integer_value(solver.getDualbound(), "objective bound")
                        if model.objective is not None
                        else None
                    )
                    result = ScipBackendResult(NativeSolveStatus.OPTIMAL, values, objective_value, objective_bound, native_status, solve_ns)
            except (KeyError, TypeError, ValueError, UnsafeSolverResult) as exc:
                result = self._unknown(f"INVALID_INTEGER: {exc}", solve_ns)
        else:
            result = self._unknown(native_status, solve_ns)
        validate_backend_result(model, result)
        return result

    def _solve_formal(
        self,
        model: LinearModel,
        *,
        time_limit_ms: int,
        artifact_dir: str | os.PathLike[str] | None,
        certificate_path: str | os.PathLike[str] | None,
        checker_timeout_ms: int,
    ) -> ScipBackendResult:
        if artifact_dir is None:
            return self._unknown("PROOF_UNCLOSED", 0)
        try:
            artifact_root = Path(artifact_dir).resolve()
            artifact_root.mkdir(parents=True, exist_ok=True)
            requested_certificate = certificate_path or f"scip-{time.time_ns()}.vipr"
            certificate = _artifact_path(artifact_root, requested_certificate)
            certificate.unlink(missing_ok=True)
            generated_certificate = _artifact_path(artifact_root, "__scip_exact_certificate.vipr")
            generated_certificate.unlink(missing_ok=True)
            exact_problem = _artifact_path(artifact_root, "scip-formal.mps")
        except (OSError, TypeError, ValueError) as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", 0)

        started_ns = time.perf_counter_ns()
        try:
            scip = _pyscipopt()
            solver = scip.Model()
            hide_output = getattr(solver, "hideOutput", None)
            if hide_output is not None:
                hide_output()
            else:
                solver.setParam("display/verblevel", 0)
            solver.setParam("parallel/maxnthreads", 1)
            solver.setParam("randomization/randomseedshift", 4901)
            solver.setParam("limits/time", time_limit_ms / 1_000)
            variable_indexes: dict[str, Any] = {}
            objective_coefficients = dict(model.objective.coefficients) if model.objective is not None else {}
            for index, variable in enumerate(model.variables):
                internal_name = f"v{index}"
                variable_indexes[variable.name] = solver.addVar(
                    name=internal_name,
                    vtype="I",
                    lb=self._native_number(variable.lower, f"{variable.name}.lower", formal=True),
                    ub=self._native_number(variable.upper, f"{variable.name}.upper", formal=True),
                    obj=self._native_number(objective_coefficients.get(variable.name, 0), f"objective.{variable.name}", formal=True),
                )
            for constraint_index, constraint in enumerate(model.constraints):
                expression: Any = 0
                for name, coefficient in constraint.coefficients:
                    expression += self._native_number(coefficient, f"{constraint.name}.{name}", formal=True) * variable_indexes[name]
                if constraint.lower is not None:
                    solver.addCons(expression >= self._native_number(constraint.lower, f"{constraint.name}.lower", formal=True), name=f"c{constraint_index}:lower")
                if constraint.upper is not None:
                    solver.addCons(expression <= self._native_number(constraint.upper, f"{constraint.name}.upper", formal=True), name=f"c{constraint_index}:upper")
            if model.objective is not None:
                solver.setMaximize() if model.objective.sense == "MAX" else solver.setMinimize()
            solver.writeProblem(str(exact_problem), verbose=False)
        except Exception as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", max(1, time.perf_counter_ns() - started_ns))

        binary = _scip_cli()
        if binary is None:
            return self._unknown("PROOF_UNCLOSED: SCIP CLI is unavailable", max(1, time.perf_counter_ns() - started_ns))
        try:
            binary = _validate_executable_path(binary, artifact_root)
        except ValueError as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", max(1, time.perf_counter_ns() - started_ns))
        exact_exit_code, exact_ns, exact_error = _run_vipr_process(
            [
                binary,
                "-c",
                "set display verblevel 0",
                "-c",
                "set parallel maxnthreads 1",
                "-c",
                "set randomization randomseedshift 4901",
                "-c",
                f"set limits time {time_limit_ms / 1_000}",
                "-c",
                "set exact enable TRUE",
                "-c",
                "set certificate filename __scip_exact_certificate.vipr",
                "-c",
                "read scip-formal.mps",
                "-c",
                "optimize",
                "-c",
                "quit",
            ],
            cwd=artifact_root,
            timeout_ms=time_limit_ms,
        )
        generation_ns = max(1, exact_ns)
        try:
            generated_certificate_ready = generated_certificate.is_file() and generated_certificate.stat().st_size > 0
        except OSError as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", generation_ns)
        if exact_error is not None or exact_exit_code != 0 or not generated_certificate_ready:
            return self._unknown("PROOF_UNCLOSED", generation_ns)
        try:
            certificate.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(generated_certificate, certificate)
        except OSError as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", generation_ns)

        try:
            certificate_result = check_vipr_certificate(certificate, artifact_root, timeout_ms=checker_timeout_ms)
            certificate_result = replace(certificate_result, generation_ns=generation_ns)
        except Exception as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", generation_ns)
        self.certificate = certificate_result
        if not certificate_result.checker_succeeded:
            return self._unknown("PROOF_UNCLOSED", generation_ns, certificate=certificate_result)
        completed = _artifact_path(artifact_root, certificate.with_name(f"{certificate.stem}_complete{certificate.suffix or '.vipr'}"))
        try:
            claim = _parse_vipr_claim(completed, model)
        except Exception as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", generation_ns, certificate=certificate_result)
        result = ScipBackendResult(
            claim.status,
            claim.values,
            claim.objective_value,
            claim.objective_bound,
            f"VIPR_CERTIFICATE:{claim.status.value}",
            generation_ns,
            certificate_result,
        )
        try:
            validate_backend_result(model, result)
        except Exception as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", generation_ns, certificate=certificate_result)
        return result


def _self_check(solver_name: str) -> None:
    if solver_name == "scip":
        from open_trader.prediction_solver import IntVariable, LinearObjective

        module = _pyscipopt()
        if str(getattr(module, "__version__", "")) != PYSCIPOPT_VERSION:
            raise RuntimeError(f"PySCIPOpt version mismatch: expected {PYSCIPOPT_VERSION}, got {getattr(module, '__version__', None)}")
        native = module.Model()
        native_version = ".".join(
            str(getattr(native, name)()) for name in ("getMajorVersion", "getMinorVersion", "getTechVersion")
        )
        if native_version != SCIP_VERSION:
            raise RuntimeError(f"SCIP version mismatch: expected {SCIP_VERSION}, got {native_version}")
        model = LinearModel((IntVariable("x", 0, 1),), (), LinearObjective("MAX", (("x", 1),)))
        result = ScipBackend().solve(model, time_limit_ms=1_000)
        if result.status != NativeSolveStatus.OPTIMAL or dict(result.values) != {"x": 1}:
            raise RuntimeError(f"SCIP self-check failed: {result}")
        print(json.dumps({"adapter": "ScipBackend", "solver": "pyscipopt", "version": native_version, "status": result.status.value}))
        return
    if solver_name == "scip-exact":
        from tempfile import TemporaryDirectory
        from open_trader.prediction_solver import IntVariable, LinearObjective

        model = LinearModel((IntVariable("x", 0, 1),), (), LinearObjective("MAX", (("x", 1),)))
        with TemporaryDirectory(prefix="open-trader-scip-exact-") as artifact_dir:
            result = ScipBackend().solve(model, time_limit_ms=10_000, formal=True, artifact_dir=artifact_dir)
            if result.status != NativeSolveStatus.OPTIMAL or not isinstance(result, ScipBackendResult) or result.certificate is None or not result.certificate.checker_succeeded:
                raise RuntimeError(f"SCIP exact/VIPR self-check failed: {result}")
            completed_files = list(Path(artifact_dir).glob("*_complete.vipr"))
            if len(completed_files) != 1:
                raise RuntimeError(f"SCIP exact/VIPR self-check missing completed certificate: {completed_files}")
            completed_files[0].write_bytes(b"corrupt")
            corrupt_exit_code, corrupt_check_ns, corrupt_error = _run_vipr_process(
                [shutil.which("viprchk") or "viprchk", str(completed_files[0])],
                cwd=Path(artifact_dir),
                timeout_ms=10_000,
            )
            if corrupt_exit_code == 0 or corrupt_error is not None:
                raise RuntimeError(f"SCIP exact/VIPR corrupt-certificate self-check unexpectedly passed: {corrupt_exit_code}, {corrupt_error}")
            lossy_model = LinearModel((IntVariable("x", 0, 1),), (), LinearObjective("MAX", (("x", 2**53),)))
            lossy = ScipBackend().solve(lossy_model, time_limit_ms=10_000, formal=True, artifact_dir=artifact_dir, certificate_path="lossy.vipr")
            if lossy.status != NativeSolveStatus.UNKNOWN or "PROOF_UNCLOSED" not in lossy.native_status:
                raise RuntimeError(f"SCIP exact/VIPR lossy-MPS self-check unexpectedly passed: {lossy}")
        print(json.dumps({"adapter": "ScipBackend", "solver": "SCIP+VIPR", "version": SCIP_VERSION, "status": result.status.value, "certificate_sha256": result.certificate.certificate_sha256, "certificate_size_bytes": result.certificate.certificate_size_bytes, "completed_certificate_sha256": result.certificate.completed_certificate_sha256, "completed_certificate_size_bytes": result.certificate.completed_certificate_size_bytes, "generation_ns": result.certificate.generation_ns, "completion_ns": result.certificate.completion_ns, "check_ns": result.certificate.check_ns, "corrupt_checker_exit_code": corrupt_exit_code, "corrupt_check_ns": corrupt_check_ns, "lossy_status": lossy.status.value, "lossy_native_status": lossy.native_status}))
        return
    if solver_name != "highs":
        raise ValueError(f"unsupported self-check solver: {solver_name}")
    from open_trader.prediction_solver import IntVariable, LinearObjective

    backend = HighsBackend()
    native_version = str(_highspy().Highs().version())
    if native_version != backend.version:
        raise RuntimeError(f"HiGHS version mismatch: expected {backend.version}, got {native_version}")
    model = LinearModel((IntVariable("x", 0, 1),), (), LinearObjective("MAX", (("x", 1),)))
    result = backend.solve(model, time_limit_ms=1_000)
    if result.status != NativeSolveStatus.OPTIMAL or dict(result.values) != {"x": 1}:
        raise RuntimeError(f"HiGHS self-check failed: {result}")
    print(json.dumps({"adapter": "HighsBackend", "solver": "highspy", "version": native_version, "status": result.status.value}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", choices=("highs", "scip", "scip-exact"))
    args = parser.parse_args()
    if args.self_check is not None:
        _self_check(args.self_check)


if __name__ == "__main__":
    main()
