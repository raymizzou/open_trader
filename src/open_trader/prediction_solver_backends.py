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
import signal
import stat
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


def _cp_model() -> Any:
    return importlib.import_module("ortools.sat.python.cp_model")


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


def _cp_sat_integer_value(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsafeSolverResult(f"CP-SAT returned a non-integer {name}")
    if not INT64_MIN <= value <= INT64_MAX:
        raise UnsafeSolverResult(f"CP-SAT returned an out-of-range {name}")
    return value


def _cp_sat_optional_bound(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value if INT64_MIN <= value <= INT64_MAX and abs(value) <= DOUBLE_INT_MAX else None
    if not math.isfinite(value) or abs(value) > DOUBLE_INT_MAX:
        return None
    rounded = round(value)
    if abs(value - rounded) > 1e-6 or not INT64_MIN <= rounded <= INT64_MAX:
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


def _lexical_artifact_path(artifact_dir: str | os.PathLike[str], path: str | os.PathLike[str]) -> Path:
    """Return an artifact path without following any path component symlink."""
    root = Path(artifact_dir).resolve()
    candidate = Path(path)
    lexical = Path(os.path.abspath(os.fspath(candidate if candidate.is_absolute() else root / candidate)))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact path must stay inside the request artifact directory") from exc
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("artifact path must not contain symlinks")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("artifact path parent is not a directory")
    return lexical


def _file_digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _read_contained_regular_file(
    artifact_dir: str | os.PathLike[str], path: str | os.PathLike[str]
) -> tuple[Path, bytes, str, int]:
    """Read a bounded regular artifact through an O_NOFOLLOW descriptor."""
    resolved = _lexical_artifact_path(artifact_dir, path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | nofollow
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ValueError(f"artifact file is unavailable: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("artifact file must be a regular file")
        if metadata.st_size > VIPR_MAX_CERTIFICATE_BYTES:
            raise ValueError("VIPR certificate exceeds the parser size limit")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, VIPR_MAX_CERTIFICATE_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > VIPR_MAX_CERTIFICATE_BYTES:
                raise ValueError("VIPR certificate exceeds the parser size limit")
        data = bytes(payload)
        if len(data) != metadata.st_size:
            raise ValueError("artifact file changed while being read")
    finally:
        os.close(descriptor)
    return resolved, data, _file_digest(data), len(data)


def _remove_contained_artifact(artifact_dir: str | os.PathLike[str], path: str | os.PathLike[str]) -> None:
    resolved = _lexical_artifact_path(artifact_dir, path)
    try:
        os.unlink(resolved)
    except FileNotFoundError:
        pass


def _write_contained_file(
    artifact_dir: str | os.PathLike[str], path: str | os.PathLike[str], data: bytes
) -> Path:
    resolved = _lexical_artifact_path(artifact_dir, path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved = _lexical_artifact_path(artifact_dir, resolved)
    try:
        os.unlink(resolved)
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags, 0o600)
    except OSError as exc:
        raise ValueError(f"cannot create contained artifact: {path}") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
    finally:
        os.close(descriptor)
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
        process_group = process.pid
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(process_group, signal.SIGKILL)
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
    # Preserve the public containment contract for paths outside the request root,
    # then use no-follow descriptor reads for every certificate artifact.
    _artifact_path(root, certificate_path)
    try:
        certificate = _lexical_artifact_path(root, certificate_path)
    except ValueError as exc:
        return ViprCheckResult(None, None, None, None, "viprchk", VIPR_VERSION, None, False, None, 0, 0, str(exc))
    try:
        certificate, original_bytes, certificate_sha256, certificate_size = _read_contained_regular_file(root, certificate)
    except ValueError as exc:
        if not certificate.exists():
            return ViprCheckResult(None, None, None, None, "viprchk", VIPR_VERSION, None, False, None, 0, 0, "certificate is missing")
        return ViprCheckResult(None, None, None, None, "viprchk", VIPR_VERSION, None, False, None, 0, 0, str(exc))
    try:
        completion_command = [_validate_executable_path(part, root) if index == 0 else os.fspath(part) for index, part in enumerate(_command_parts(viprcomp))]
        checker_command = [_validate_executable_path(part, root) if index == 0 else os.fspath(part) for index, part in enumerate(_command_parts(viprchk))]
    except ValueError as exc:
        return ViprCheckResult(None, None, None, None, "viprchk", VIPR_VERSION, None, False, None, 0, 0, str(exc))
    try:
        completed = _lexical_artifact_path(root, certificate.with_name(f"{certificate.stem}_complete{certificate.suffix}"))
        _remove_contained_artifact(root, completed)
    except ValueError as exc:
        return ViprCheckResult(certificate_sha256, certificate_size, None, None, "viprchk", VIPR_VERSION, None, False, None, 0, 0, str(exc))
    completion_exit_code, completion_ns, completion_error = _run_vipr_process(
        [*completion_command, "--threads=1", str(certificate)], cwd=root, timeout_ms=timeout_ms
    )
    try:
        _, original_after_bytes, original_after_sha256, original_after_size = _read_contained_regular_file(root, certificate)
    except ValueError as exc:
        return ViprCheckResult(certificate_sha256, certificate_size, None, None, "viprchk", VIPR_VERSION, None, False, completion_exit_code, completion_ns, 0, str(exc))
    if original_after_bytes != original_bytes or original_after_sha256 != certificate_sha256 or original_after_size != certificate_size:
        return ViprCheckResult(certificate_sha256, certificate_size, None, None, "viprchk", VIPR_VERSION, None, False, completion_exit_code, completion_ns, 0, "certificate changed during completion")
    try:
        _, completed_bytes, completed_sha256, completed_size = _read_contained_regular_file(root, completed)
    except ValueError as exc:
        return ViprCheckResult(certificate_sha256, certificate_size, None, None, "viprchk", VIPR_VERSION, None, False, completion_exit_code, completion_ns, 0, completion_error or str(exc))
    if completion_error is not None or completion_exit_code != 0:
        return ViprCheckResult(
            certificate_sha256,
            certificate_size,
            completed_sha256,
            completed_size,
            "viprchk",
            VIPR_VERSION,
            None,
            False,
            completion_exit_code,
            completion_ns,
            0,
            completion_error or "viprcomp failed",
        )

    checker_exit_code, check_ns, checker_error = _run_vipr_process(
        [*checker_command, str(completed)], cwd=root, timeout_ms=timeout_ms
    )
    try:
        _, completed_after_bytes, completed_after_sha256, completed_after_size = _read_contained_regular_file(root, completed)
    except ValueError as exc:
        return ViprCheckResult(certificate_sha256, certificate_size, completed_sha256, completed_size, "viprchk", VIPR_VERSION, checker_exit_code, False, completion_exit_code, completion_ns, check_ns, str(exc))
    if completed_after_bytes != completed_bytes or completed_after_sha256 != completed_sha256 or completed_after_size != completed_size:
        return ViprCheckResult(certificate_sha256, certificate_size, completed_sha256, completed_size, "viprchk", VIPR_VERSION, checker_exit_code, False, completion_exit_code, completion_ns, check_ns, "completed certificate changed during checking")
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
            row = _vipr_row("G", Fraction(constraint.lower), terms)
            if not (not terms and constraint.lower <= 0):
                rows.append(row)
        if constraint.upper is not None:
            row = _vipr_row("L", Fraction(constraint.upper), terms)
            if not (not terms and constraint.upper >= 0):
                rows.append(row)
    for index, variable in enumerate(model.variables):
        rows.append(_vipr_row("G", Fraction(variable.lower), {index: Fraction(1)}))
        rows.append(_vipr_row("L", Fraction(variable.upper), {index: Fraction(1)}))
    return tuple(rows)


def _parse_vipr_claim(certificate_path: Path, model: LinearModel, certificate_bytes: bytes | None = None) -> _ViprClaim:
    try:
        if certificate_bytes is None:
            _, certificate_bytes, _, _ = _read_contained_regular_file(certificate_path.parent, certificate_path)
        if len(certificate_bytes) > VIPR_MAX_CERTIFICATE_BYTES:
            raise ValueError("VIPR certificate exceeds the parser size limit")
        tokens = certificate_bytes.decode("ascii").split()
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
    certificate_rows = [
        row for row in certificate_rows if not (not row[2] and ((row[0] == "G" and row[1] <= 0) or (row[0] == "L" and row[1] >= 0)))
    ]
    if constraint_count < len(expected_rows) or bound_count != 2 * len(model.variables):
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


def _validate_cp_sat_numeric(model: LinearModel) -> None:
    """Reject expressions CP-SAT could overflow while building or solving."""
    validate_linear_model(model)
    variables = {variable.name: variable for variable in model.variables}

    def validate_activity(owner: str, terms: tuple[tuple[str, int], ...]) -> None:
        minimum = maximum = 0
        cumulative = 0
        for name, coefficient in terms:
            variable = variables[name]
            lower_product = coefficient * variable.lower
            upper_product = coefficient * variable.upper
            if not INT64_MIN <= lower_product <= INT64_MAX or not INT64_MIN <= upper_product <= INT64_MAX:
                raise ValueError(f"possible {owner} term product exceeds signed int64: {name}")
            cumulative += max(abs(lower_product), abs(upper_product))
            if cumulative > INT64_MAX:
                raise ValueError(f"possible {owner} cumulative activity exceeds signed int64")
            if coefficient >= 0:
                minimum += lower_product
                maximum += upper_product
            else:
                minimum += upper_product
                maximum += lower_product
        if not INT64_MIN <= minimum <= INT64_MAX or not INT64_MIN <= maximum <= INT64_MAX:
            raise ValueError(f"possible {owner} activity exceeds signed int64")

    for constraint in model.constraints:
        validate_activity(f"row {constraint.name}", constraint.coefficients)
    if model.objective is not None:
        validate_activity("objective", model.objective.coefficients)


class CpSatBackend:
    """Thin translation layer from the benchmark integer IR to CP-SAT."""

    name = "cp_sat"
    version = "9.15.6755"

    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        _validate_cp_sat_numeric(model)
        if isinstance(time_limit_ms, bool) or not isinstance(time_limit_ms, int) or time_limit_ms <= 0:
            raise ValueError("time_limit_ms must be a positive integer")

        if model.objective is not None and any(
            coefficient == INT64_MIN for _, coefficient in model.objective.coefficients
        ):
            raise ValueError("CP-SAT objective coefficient INT64_MIN is unsupported")

        cp_model = _cp_model()
        native_model = cp_model.CpModel()
        variables: dict[str, Any] = {
            variable.name: native_model.new_int_var(variable.lower, variable.upper, variable.name)
            for variable in model.variables
        }

        def expression(terms: tuple[tuple[str, int], ...]) -> Any:
            return sum((coefficient * variables[name] for name, coefficient in terms), 0)

        for constraint in model.constraints:
            native_model.add_linear_constraint(
                expression(constraint.coefficients),
                INT64_MIN if constraint.lower is None else constraint.lower,
                INT64_MAX if constraint.upper is None else constraint.upper,
            )
        if model.objective is not None:
            objective = expression(model.objective.coefficients)
            if model.objective.sense == "MAX":
                native_model.maximize(objective)
            else:
                native_model.minimize(objective)

        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 4901
        solver.parameters.max_time_in_seconds = time_limit_ms / 1_000
        solver.parameters.log_search_progress = False
        solver.parameters.log_to_stdout = False
        started_ns = time.perf_counter_ns()
        native_result = solver.solve(native_model)
        solve_ns = max(1, time.perf_counter_ns() - started_ns)
        native_status = str(solver.status_name(native_result))

        if native_result == cp_model.INFEASIBLE:
            result = BackendResult(NativeSolveStatus.INFEASIBLE, (), None, None, native_status, solve_ns)
            validate_backend_result(model, result)
            return result
        if native_result not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            result = BackendResult(NativeSolveStatus.UNKNOWN, (), None, None, native_status, solve_ns)
            validate_backend_result(model, result)
            return result

        values = tuple(
            (variable.name, _cp_sat_integer_value(solver.value(variables[variable.name]), f"value for {variable.name}"))
            for variable in model.variables
        )
        values_by_name = dict(values)
        objective_value = (
            sum(values_by_name[name] * coefficient for name, coefficient in model.objective.coefficients)
            if model.objective is not None
            else None
        )
        objective_bound = None
        if model.objective is not None:
            # Keep the native best bound only as bounded diagnostics; the
            # parent recomputes the exact objective from native integer values.
            objective_bound = (
                _cp_sat_optional_bound(solver.best_objective_bound)
                if objective_value is None or abs(objective_value) <= DOUBLE_INT_MAX
                else None
            )
        result = BackendResult(
            NativeSolveStatus.OPTIMAL if native_result == cp_model.OPTIMAL else NativeSolveStatus.FEASIBLE,
            values,
            objective_value,
            objective_bound,
            native_status,
            solve_ns,
        )
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
    def _empty_expression(solver: Any, variable_indexes: dict[str, Any], quicksum: Any = None) -> Any:
        if variable_indexes:
            return 0 * next(iter(variable_indexes.values()))
        quicksum = quicksum or getattr(solver, "quicksum", None)
        if quicksum is not None:
            return quicksum(())
        return 0

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
                expression: Any = self._empty_expression(solver, variable_indexes, getattr(scip, "quicksum", None)) if not constraint.coefficients else 0
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
            requested_target = _lexical_artifact_path(artifact_root, requested_certificate)
            generated_certificate = _lexical_artifact_path(artifact_root, "__scip_exact_certificate.vipr")
            _remove_contained_artifact(artifact_root, generated_certificate)
            _remove_contained_artifact(artifact_root, generated_certificate.with_name("__scip_exact_certificate_complete.vipr"))
            exact_problem = _lexical_artifact_path(artifact_root, "scip-formal.mps")
            _remove_contained_artifact(artifact_root, exact_problem)
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
                expression: Any = self._empty_expression(solver, variable_indexes, getattr(scip, "quicksum", None)) if not constraint.coefficients else 0
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
        except Exception as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", max(1, time.perf_counter_ns() - started_ns))
        try:
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
                    "set presolving emphasis off",
                    "-c",
                    "set heuristics emphasis off",
                    "-c",
                    "set exact enable TRUE",
                    "-c",
                    "set certificate filename __scip_exact_certificate.vipr",
                    "-c",
                    "set certificate maxfilesize 64",
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
        except Exception as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", max(1, time.perf_counter_ns() - started_ns))
        generation_ns = max(1, exact_ns)
        try:
            _, generated_bytes, generated_sha256, generated_size = _read_contained_regular_file(artifact_root, generated_certificate)
        except ValueError as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", generation_ns)
        if exact_error is not None or exact_exit_code != 0:
            return self._unknown("PROOF_UNCLOSED", generation_ns)
        try:
            certificate_result = check_vipr_certificate(generated_certificate, artifact_root, timeout_ms=checker_timeout_ms)
            certificate_result = replace(certificate_result, generation_ns=generation_ns)
        except Exception as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", generation_ns)
        self.certificate = certificate_result
        if not certificate_result.checker_succeeded:
            return self._unknown("PROOF_UNCLOSED", generation_ns, certificate=certificate_result)
        try:
            _, generated_after_bytes, generated_after_sha256, generated_after_size = _read_contained_regular_file(artifact_root, generated_certificate)
            completed = _lexical_artifact_path(artifact_root, generated_certificate.with_name("__scip_exact_certificate_complete.vipr"))
            _, completed_bytes, completed_sha256, completed_size = _read_contained_regular_file(artifact_root, completed)
            if generated_after_bytes != generated_bytes or generated_after_sha256 != generated_sha256 or generated_after_size != generated_size:
                raise ValueError("generated certificate changed after checking")
            if certificate_result.certificate_sha256 != generated_after_sha256 or certificate_result.certificate_size_bytes != generated_after_size:
                raise ValueError("generated certificate evidence does not match checked bytes")
            if certificate_result.completed_certificate_sha256 != completed_sha256 or certificate_result.completed_certificate_size_bytes != completed_size:
                raise ValueError("completed certificate evidence does not match checked bytes")
            claim = _parse_vipr_claim(completed, model, completed_bytes)
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
        try:
            _write_contained_file(artifact_root, requested_target, generated_after_bytes)
        except Exception as exc:
            return self._unknown(f"PROOF_UNCLOSED: {exc}", generation_ns, certificate=certificate_result)
        return result


def _self_check(solver_name: str) -> None:
    if solver_name == "cp_sat":
        from importlib.metadata import PackageNotFoundError, version as package_version
        from open_trader.prediction_solver import IntVariable, LinearConstraint, LinearObjective

        try:
            native_version = package_version("ortools")
        except PackageNotFoundError as exc:
            raise RuntimeError("OR-Tools package metadata is unavailable") from exc
        backend = CpSatBackend()
        if native_version != backend.version:
            raise RuntimeError(f"OR-Tools version mismatch: expected {backend.version}, got {native_version}")
        maximum = backend.solve(
            LinearModel((IntVariable("x", 0, 1),), (), LinearObjective("MAX", (("x", 1),))),
            time_limit_ms=1_000,
        )
        minimum = backend.solve(
            LinearModel((IntVariable("x", 0, 1),), (), LinearObjective("MIN", (("x", 1),))),
            time_limit_ms=1_000,
        )
        infeasible = backend.solve(
            LinearModel(
                (IntVariable("x", 0, 1),),
                (LinearConstraint("impossible", (("x", 1),), 2, None),),
                None,
            ),
            time_limit_ms=1_000,
        )
        if maximum.status != NativeSolveStatus.OPTIMAL or dict(maximum.values) != {"x": 1}:
            raise RuntimeError(f"CP-SAT max self-check failed: {maximum}")
        if minimum.status != NativeSolveStatus.OPTIMAL or dict(minimum.values) != {"x": 0}:
            raise RuntimeError(f"CP-SAT min self-check failed: {minimum}")
        if infeasible.status != NativeSolveStatus.INFEASIBLE:
            raise RuntimeError(f"CP-SAT infeasible self-check failed: {infeasible}")
        print(json.dumps({"adapter": "CpSatBackend", "solver": "ortools", "version": native_version, "status": maximum.status.value}))
        return
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
        from open_trader.prediction_solver import IntVariable, LinearConstraint, LinearObjective

        model = LinearModel((IntVariable("x", 0, 1),), (), LinearObjective("MAX", (("x", 1),)))
        with TemporaryDirectory(prefix="open-trader-scip-exact-") as artifact_dir:
            tiny_dir = Path(artifact_dir) / "tiny"
            result = ScipBackend().solve(model, time_limit_ms=10_000, formal=True, artifact_dir=tiny_dir)
            if result.status != NativeSolveStatus.OPTIMAL or not isinstance(result, ScipBackendResult) or result.certificate is None or not result.certificate.checker_succeeded:
                raise RuntimeError(f"SCIP exact/VIPR self-check failed: {result}")
            constrained_model = LinearModel(
                (IntVariable("x", 0, 2),),
                (LinearConstraint("lb", (("x", 1),), 1, None),),
                LinearObjective("MIN", (("x", 1),)),
            )
            equality_model = LinearModel(
                (IntVariable("x", 0, 2),),
                (LinearConstraint("eq", (("x", 1),), 1, 1),),
                LinearObjective("MIN", (("x", 1),)),
            )
            ranged_model = LinearModel(
                (IntVariable("x", 0, 2),),
                (LinearConstraint("range", (("x", 1),), 1, 2),),
                LinearObjective("MIN", (("x", 1),)),
            )
            infeasible_model = LinearModel(
                (IntVariable("x", 0, 1),),
                (LinearConstraint("bad", (("x", 1),), 2, None),),
                None,
            )
            constrained = ScipBackend().solve(constrained_model, time_limit_ms=10_000, formal=True, artifact_dir=Path(artifact_dir) / "constrained")
            equality = ScipBackend().solve(equality_model, time_limit_ms=10_000, formal=True, artifact_dir=Path(artifact_dir) / "equality")
            ranged = ScipBackend().solve(ranged_model, time_limit_ms=10_000, formal=True, artifact_dir=Path(artifact_dir) / "ranged")
            infeasible = ScipBackend().solve(infeasible_model, time_limit_ms=10_000, formal=True, artifact_dir=Path(artifact_dir) / "infeasible")
            if constrained.status != NativeSolveStatus.OPTIMAL or equality.status != NativeSolveStatus.OPTIMAL or ranged.status != NativeSolveStatus.OPTIMAL or infeasible.status != NativeSolveStatus.INFEASIBLE:
                raise RuntimeError(f"SCIP exact/VIPR constrained self-check failed: {constrained}, {equality}, {ranged}, {infeasible}")
            completed_files = list(tiny_dir.glob("*_complete.vipr"))
            if len(completed_files) != 1:
                raise RuntimeError(f"SCIP exact/VIPR self-check missing completed certificate: {completed_files}")
            completed_files[0].write_bytes(b"corrupt")
            corrupt_exit_code, corrupt_check_ns, corrupt_error = _run_vipr_process(
                [shutil.which("viprchk") or "viprchk", str(completed_files[0])],
                cwd=tiny_dir,
                timeout_ms=10_000,
            )
            if corrupt_exit_code == 0 or corrupt_error is not None:
                raise RuntimeError(f"SCIP exact/VIPR corrupt-certificate self-check unexpectedly passed: {corrupt_exit_code}, {corrupt_error}")
            lossy_model = LinearModel((IntVariable("x", 0, 1),), (), LinearObjective("MAX", (("x", 2**53),)))
            lossy = ScipBackend().solve(lossy_model, time_limit_ms=10_000, formal=True, artifact_dir=Path(artifact_dir) / "lossy", certificate_path="lossy.vipr")
            if lossy.status != NativeSolveStatus.UNKNOWN or "PROOF_UNCLOSED" not in lossy.native_status:
                raise RuntimeError(f"SCIP exact/VIPR lossy-MPS self-check unexpectedly passed: {lossy}")
        print(json.dumps({"adapter": "ScipBackend", "solver": "SCIP+VIPR", "version": SCIP_VERSION, "status": result.status.value, "certificate_sha256": result.certificate.certificate_sha256, "certificate_size_bytes": result.certificate.certificate_size_bytes, "completed_certificate_sha256": result.certificate.completed_certificate_sha256, "completed_certificate_size_bytes": result.certificate.completed_certificate_size_bytes, "generation_ns": result.certificate.generation_ns, "completion_ns": result.certificate.completion_ns, "check_ns": result.certificate.check_ns, "corrupt_checker_exit_code": corrupt_exit_code, "corrupt_check_ns": corrupt_check_ns, "constrained_status": constrained.status.value, "equality_status": equality.status.value, "ranged_status": ranged.status.value, "infeasible_status": infeasible.status.value, "lossy_status": lossy.status.value, "lossy_native_status": lossy.native_status}))
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
    parser.add_argument("--self-check", choices=("highs", "scip", "scip-exact", "cp_sat"))
    args = parser.parse_args()
    if args.self_check is not None:
        _self_check(args.self_check)


if __name__ == "__main__":
    main()
