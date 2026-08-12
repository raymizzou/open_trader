"""Optional native adapters for the solver benchmark.

The benchmark keeps vendor packages out of the application environment.  This
module therefore imports HiGHS only when an adapter is used.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
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


def _self_check(solver_name: str) -> None:
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
    parser.add_argument("--self-check", choices=("highs",))
    args = parser.parse_args()
    if args.self_check is not None:
        _self_check(args.self_check)


if __name__ == "__main__":
    main()
