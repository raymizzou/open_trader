from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from open_trader.prediction_solver import (
    IntVariable,
    LinearConstraint,
    LinearModel,
    LinearObjective,
    NativeSolveStatus,
    UnsafeSolverResult,
    validate_backend_result,
)
import open_trader.prediction_solver_backends as solver_backends
from open_trader.prediction_solver_backends import HighsBackend

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "prediction_solver"
BUILD = ROOT / "scripts" / "build_prediction_solver_envs.sh"


def test_manifest_pins_are_exact() -> None:
    assert (BENCHMARK / "requirements" / "highs.txt").read_text() == "highspy==1.15.1\n"
    assert (BENCHMARK / "requirements" / "scip.txt").read_text() == "pyscipopt==6.2.1\n"
    assert (BENCHMARK / "requirements" / "cp_sat.txt").read_text() == "ortools==9.15.6755\n"


def test_build_key_material_covers_every_rebuild_input() -> None:
    script = BUILD.read_text()
    for required_input in (
        "sys.version_info.major",
        "sys.version_info.minor",
        "uname -s",
        "uname -m",
        "open_trader.prediction_solver.protocol.v1",
        'cat "$requirements"',
        'cat "$dockerfile"',
    ):
        assert required_input in script
    assert ".build-key" in script
    assert "REUSED" in script
    assert "REBUILD_REQUIRED" in script


def test_builder_rejects_unknown_environment_with_exit_2() -> None:
    result = subprocess.run(
        [str(BUILD), "unknown"], cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2


def test_linux_recipes_allowlist_requirements_and_verify_exact_capabilities() -> None:
    python_recipe = (BENCHMARK / "Dockerfile.python").read_text()
    assert "highs.txt|cp_sat.txt" in python_recipe

    scip_recipe = (BENCHMARK / "Dockerfile.scip").read_text()
    for required in (
        "eecc29f31e8c8a3089c95ef99dd310d05e1546ba40f4ff36551d75a5f5c47073",
        "30f2951d1e90e47afa821bdd1b12b82246656c42",
        "bfd905e3378353b5f4e93ad2405c75feed0d477e0a74113496fb2d6e04ca7786",
        "-DEXACTSOLVE=on",
        "-DLPSEXACT=spx",
        "-DGMP=on",
        "-DMPFR=on",
        "viprchk",
        "viprcomp",
        "exact solving disabled",
    ):
        assert required in scip_recipe


def test_scip_recipe_caps_scip_and_vipr_build_parallelism() -> None:
    scip_recipe = (BENCHMARK / "Dockerfile.scip").read_text()
    build_commands = []
    for line in scip_recipe.splitlines():
        if "cmake --build" not in line:
            continue
        command = line.strip().removesuffix("\\").strip()
        command = command.removeprefix("RUN ").removeprefix("&& ")
        build_commands.append(command)
    assert build_commands == [
        "cmake --build /opt/build/scip --parallel 2",
        "cmake --build /opt/build/vipr --parallel 2",
    ]


def test_scip_recipe_checks_generated_exactsolve_config_path() -> None:
    scip_recipe = (BENCHMARK / "Dockerfile.scip").read_text()
    assert "grep -qx '#define SCIP_WITH_EXACTSOLVE' /opt/build/scip/scip/scip/config.h" in scip_recipe
    assert "/opt/build/scip/scip/config.h" not in scip_recipe


def test_all_target_builds_solver_environments_in_order() -> None:
    script = BUILD.read_text()
    all_target_body = script.split('if [[ "$target" == all ]]; then', 1)[1].split("else", 1)[0]
    assert [line.strip() for line in all_target_body.splitlines() if line.strip()] == [
        'for environment in highs scip cp_sat; do build_one "$environment"; done',
    ]


def test_license_manifest_has_explicit_pinned_evidence_without_commercial_keys() -> None:
    licenses = json.loads((BENCHMARK / "licenses.json").read_text())
    expected = {
        "highspy": ("1.15.1", "MIT"),
        "pyscipopt": ("6.2.1", "MIT"),
        "scip": ("10.0.2", "Apache-2.0"),
        "ortools": ("9.15.6755", "Apache-2.0"),
        "vipr": ("30f2951d1e90e47afa821bdd1b12b82246656c42", "MIT"),
    }
    for name, (version, license_id) in expected.items():
        entry = licenses[name]
        assert entry["version"] == version
        assert entry["license"] == license_id
        assert entry["project_url"].startswith("https://")
        assert entry["evidence_path"]
        assert entry["commercial_key_required"] is False

    vipr = licenses["vipr"]
    assert vipr["evidence_path"] == "code/viprchk.cpp"
    assert vipr["evidence_sha256"] == "2baf9c4593f5b8ef42323fbfb7cbfa0e4dfafff65e636cf6a143561b9dca2738"


@pytest.fixture(scope="module")
def highs_backend() -> HighsBackend:
    pytest.importorskip("highspy")
    return HighsBackend()


def test_highs_translates_integer_rows_and_maximizes(highs_backend: HighsBackend) -> None:
    model = LinearModel(
        variables=(IntVariable("x", 0, 10), IntVariable("y", 0, 10)),
        constraints=(LinearConstraint("capacity", (("x", 1), ("y", 1)), None, 7),),
        objective=LinearObjective("MAX", (("x", 3), ("y", 1))),
    )

    result = highs_backend.solve(model, time_limit_ms=1_000)

    assert result.status == NativeSolveStatus.OPTIMAL
    assert dict(result.values) == {"x": 7, "y": 0}
    assert result.objective_value == 21
    validate_backend_result(model, result)


def test_highs_translates_minimize_objective(highs_backend: HighsBackend) -> None:
    model = LinearModel(
        variables=(IntVariable("x", 0, 10), IntVariable("y", 0, 10)),
        constraints=(LinearConstraint("minimum", (("x", 1), ("y", 1)), 7, None),),
        objective=LinearObjective("MIN", (("x", 2), ("y", 1))),
    )

    result = highs_backend.solve(model, time_limit_ms=1_000)

    assert result.status == NativeSolveStatus.OPTIMAL
    assert dict(result.values) == {"x": 0, "y": 7}
    assert result.objective_value == 7
    validate_backend_result(model, result)


def test_highs_reports_infeasible_model_without_an_incumbent(highs_backend: HighsBackend) -> None:
    model = LinearModel(
        variables=(IntVariable("x", 0, 1),),
        constraints=(LinearConstraint("impossible", (("x", 1),), 2, None),),
        objective=None,
    )

    result = highs_backend.solve(model, time_limit_ms=1_000)

    assert result.status == NativeSolveStatus.INFEASIBLE
    assert result.values == ()
    assert result.objective_value is None
    validate_backend_result(model, result)


class _FakeModelStatus:
    OPTIMAL = "optimal"
    INFEASIBLE = "infeasible"
    TIME_LIMIT = "time_limit"
    OTHER = "other"


class _FakeHighs:
    model_status = _FakeModelStatus.OTHER
    solution = SimpleNamespace(value_valid=False, col_value=())
    info = SimpleNamespace(mip_dual_bound=0.0)
    instances: list["_FakeHighs"] = []

    def __init__(self) -> None:
        self.options: dict[str, object] = {}
        self.variables: list[dict[str, object]] = []
        self.rows: list[tuple[object, ...]] = []
        self.objective_sense: str | None = None
        type(self).instances.append(self)

    def setOptionValue(self, name: str, value: object) -> None:
        self.options[name] = value

    def addVariable(self, **kwargs: object) -> None:
        self.variables.append(kwargs)

    def addRow(self, *args: object) -> None:
        self.rows.append(args)

    def setMaximize(self) -> None:
        self.objective_sense = "MAX"

    def setMinimize(self) -> None:
        self.objective_sense = "MIN"

    def run(self) -> str:
        return "ok"

    def getModelStatus(self) -> str:
        return type(self).model_status

    def modelStatusToString(self, status: str) -> str:
        return status

    def getSolution(self) -> SimpleNamespace:
        return type(self).solution

    def getInfo(self) -> SimpleNamespace:
        return type(self).info


@pytest.fixture
def fake_highspy(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    _FakeHighs.instances.clear()
    module = SimpleNamespace(
        Highs=_FakeHighs,
        HighsVarType=SimpleNamespace(kInteger="integer"),
        HighsModelStatus=SimpleNamespace(kOptimal=_FakeModelStatus.OPTIMAL, kInfeasible=_FakeModelStatus.INFEASIBLE),
        kHighsInf=1e100,
    )
    monkeypatch.setattr(solver_backends.importlib, "import_module", lambda name: module)
    return module


def _one_variable_model(*, objective: int = 1) -> LinearModel:
    return LinearModel((IntVariable("x", 0, 1),), (), LinearObjective("MAX", (("x", objective),)))


def test_fake_highs_sets_deterministic_options_and_translates_max_model(fake_highspy: SimpleNamespace) -> None:
    _FakeHighs.model_status = _FakeModelStatus.OPTIMAL
    model = LinearModel(
        variables=(IntVariable("x", 0, 10), IntVariable("y", 0, 10)),
        constraints=(LinearConstraint("capacity", (("x", 1), ("y", 1)), None, 7),),
        objective=LinearObjective("MAX", (("x", 3), ("y", 1))),
    )
    _FakeHighs.solution = SimpleNamespace(value_valid=True, col_value=(7.0, 0.0))
    _FakeHighs.info = SimpleNamespace(mip_dual_bound=21.0)

    result = HighsBackend().solve(model, time_limit_ms=321)

    instance = _FakeHighs.instances[-1]
    assert result.status == NativeSolveStatus.OPTIMAL
    assert dict(result.values) == {"x": 7, "y": 0}
    assert result.objective_value == 21
    assert instance.options == {
        "output_flag": False,
        "log_to_console": False,
        "threads": 1,
        "random_seed": 4901,
        "time_limit": 0.321,
    }
    assert instance.objective_sense == "MAX"
    assert [variable["type"] for variable in instance.variables] == ["integer", "integer"]
    validate_backend_result(model, result)


def test_fake_highs_translates_minimize_objective(fake_highspy: SimpleNamespace) -> None:
    model = LinearModel(
        variables=(IntVariable("x", 0, 10), IntVariable("y", 0, 10)),
        constraints=(LinearConstraint("minimum", (("x", 1), ("y", 1)), 7, None),),
        objective=LinearObjective("MIN", (("x", 2), ("y", 1))),
    )
    _FakeHighs.model_status = _FakeModelStatus.OPTIMAL
    _FakeHighs.solution = SimpleNamespace(value_valid=True, col_value=(0.0, 7.0))
    _FakeHighs.info = SimpleNamespace(mip_dual_bound=7.0)

    result = HighsBackend().solve(model, time_limit_ms=1)

    assert result.status == NativeSolveStatus.OPTIMAL
    assert result.objective_value == 7
    assert _FakeHighs.instances[-1].objective_sense == "MIN"


def test_fake_highs_maps_minimize_and_native_states(fake_highspy: SimpleNamespace) -> None:
    model = _one_variable_model()
    cases = (
        (_FakeModelStatus.OPTIMAL, True, NativeSolveStatus.OPTIMAL),
        (_FakeModelStatus.INFEASIBLE, False, NativeSolveStatus.INFEASIBLE),
        (_FakeModelStatus.TIME_LIMIT, True, NativeSolveStatus.FEASIBLE),
        (_FakeModelStatus.TIME_LIMIT, False, NativeSolveStatus.UNKNOWN),
        (_FakeModelStatus.OTHER, True, NativeSolveStatus.FEASIBLE),
        (_FakeModelStatus.OTHER, False, NativeSolveStatus.UNKNOWN),
    )
    for native_status, value_valid, expected in cases:
        _FakeHighs.model_status = native_status
        _FakeHighs.solution = SimpleNamespace(value_valid=value_valid, col_value=(1.0,) if value_valid else ())
        _FakeHighs.info = SimpleNamespace(mip_dual_bound=1.0)

        result = HighsBackend().solve(model, time_limit_ms=1)

        assert result.status == expected
        if expected in {NativeSolveStatus.FEASIBLE, NativeSolveStatus.OPTIMAL}:
            assert result.values == (("x", 1),)
        else:
            assert result.values == ()


def test_fake_highs_accepts_only_native_values_within_one_integer(fake_highspy: SimpleNamespace) -> None:
    model = _one_variable_model()
    _FakeHighs.model_status = _FakeModelStatus.OTHER
    _FakeHighs.info = SimpleNamespace(mip_dual_bound=0.0)

    _FakeHighs.solution = SimpleNamespace(value_valid=True, col_value=(1.0000005,))
    assert HighsBackend().solve(model, time_limit_ms=1).values == (("x", 1),)

    _FakeHighs.solution = SimpleNamespace(value_valid=True, col_value=(1.000002,))
    with pytest.raises(UnsafeSolverResult, match="non-integral"):
        HighsBackend().solve(model, time_limit_ms=1)


def test_fake_highs_rejects_native_solution_that_breaks_parent_row(fake_highspy: SimpleNamespace) -> None:
    model = LinearModel(
        variables=(IntVariable("x", 0, 1),),
        constraints=(LinearConstraint("cap", (("x", 1),), None, 0),),
        objective=LinearObjective("MAX", (("x", 1),)),
    )
    _FakeHighs.model_status = _FakeModelStatus.TIME_LIMIT
    _FakeHighs.solution = SimpleNamespace(value_valid=True, col_value=(1.0,))
    _FakeHighs.info = SimpleNamespace(mip_dual_bound=1.0)

    with pytest.raises(UnsafeSolverResult, match="constraint violated: cap"):
        HighsBackend().solve(model, time_limit_ms=1)


def test_fake_highs_rounds_only_near_integer_objective_bounds(fake_highspy: SimpleNamespace) -> None:
    model = _one_variable_model()
    _FakeHighs.model_status = _FakeModelStatus.TIME_LIMIT
    _FakeHighs.solution = SimpleNamespace(value_valid=False, col_value=())

    _FakeHighs.info = SimpleNamespace(mip_dual_bound=4.0000005)
    assert HighsBackend().solve(model, time_limit_ms=1).objective_bound == 4

    _FakeHighs.info = SimpleNamespace(mip_dual_bound=4.000002)
    assert HighsBackend().solve(model, time_limit_ms=1).objective_bound is None


@pytest.mark.parametrize(
    "model",
    (
        LinearModel((IntVariable("x", 0, 2**53 + 1),), (), None),
        LinearModel((IntVariable("x", 0, 1),), (LinearConstraint("row", (("x", 2**53 + 1),), None, None),), None),
        LinearModel((IntVariable("x", 0, 1),), (LinearConstraint("row", (("x", 1),), 2**53 + 1, None),), None),
        LinearModel((IntVariable("x", 0, 1),), (LinearConstraint("row", (("x", 1),), None, 2**53 + 1),), None),
        _one_variable_model(objective=2**53 + 1),
    ),
)
def test_fake_highs_rejects_int64_values_that_double_cannot_represent(
    fake_highspy: SimpleNamespace, model: LinearModel
) -> None:
    _FakeHighs.model_status = _FakeModelStatus.INFEASIBLE
    _FakeHighs.solution = SimpleNamespace(value_valid=False, col_value=())
    _FakeHighs.info = SimpleNamespace(mip_dual_bound=0.0)
    with pytest.raises(UnsafeSolverResult, match="cannot represent"):
        HighsBackend().solve(model, time_limit_ms=1)
    assert _FakeHighs.instances == []


def test_fake_highs_accepts_the_exact_double_boundary(fake_highspy: SimpleNamespace) -> None:
    _FakeHighs.model_status = _FakeModelStatus.OPTIMAL
    _FakeHighs.solution = SimpleNamespace(value_valid=True, col_value=(1.0,))
    _FakeHighs.info = SimpleNamespace(mip_dual_bound=float(2**53))

    result = HighsBackend().solve(_one_variable_model(objective=2**53), time_limit_ms=1)

    assert result.status == NativeSolveStatus.OPTIMAL
    assert result.objective_value == 2**53
    assert _FakeHighs.instances


def test_fake_highs_rejects_unsafe_aggregate_activity_before_native_solve(fake_highspy: SimpleNamespace) -> None:
    model = LinearModel(
        variables=tuple(IntVariable(f"x{index}", 0, 1) for index in range(4)),
        constraints=(
            LinearConstraint("balance", (("x0", -1), ("x1", 2), ("x2", 1), ("x3", 1)), 1, 1),
        ),
        objective=LinearObjective("MAX", (("x0", 2**53), ("x1", 2**53), ("x2", 2**53), ("x3", 2))),
    )
    _FakeHighs.model_status = _FakeModelStatus.OPTIMAL
    _FakeHighs.solution = SimpleNamespace(value_valid=True, col_value=(1.0, 1.0, 0.0, 0.0))
    _FakeHighs.info = SimpleNamespace(mip_dual_bound=float(2**54))

    with pytest.raises(UnsafeSolverResult, match="activity"):
        HighsBackend().solve(model, time_limit_ms=1)
    assert _FakeHighs.instances == []


def test_fake_highs_rejects_fixed_cancellation_with_an_unsafe_term_product(fake_highspy: SimpleNamespace) -> None:
    model = LinearModel(
        variables=(
            IntVariable("x", 2**52 + 1, 2**52 + 1),
            IntVariable("y", 2**52, 2**52),
            IntVariable("z", 0, 1),
        ),
        constraints=(
            LinearConstraint("balance", (("x", 3), ("y", -3), ("z", 1)), 3, 3),
        ),
        objective=LinearObjective("MIN", (("z", 1),)),
    )
    _FakeHighs.model_status = _FakeModelStatus.OPTIMAL
    _FakeHighs.solution = SimpleNamespace(value_valid=True, col_value=(float(2**52 + 1), float(2**52), 0.0))
    _FakeHighs.info = SimpleNamespace(mip_dual_bound=0.0)

    with pytest.raises(UnsafeSolverResult, match="term contribution"):
        HighsBackend().solve(model, time_limit_ms=1)
    assert _FakeHighs.instances == []


def test_highs_backend_exposes_pinned_solver_identity_without_candidate_imports() -> None:
    assert HighsBackend.name == "highs"
    assert HighsBackend.version == "1.15.1"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import open_trader.prediction_solver_backends; print('highspy' in sys.modules, 'pyscipopt' in sys.modules, 'ortools' in sys.modules)",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "False False False"
