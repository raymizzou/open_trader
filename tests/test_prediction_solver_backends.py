from __future__ import annotations

import json
import os
import stat
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
from open_trader.prediction_solver_backends import HighsBackend, ScipBackend, ViprCheckResult, check_vipr_certificate

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


class _FakeScipExpr:
    def __init__(self, terms: tuple[tuple[int, str], ...] = (), constant: int = 0) -> None:
        self.terms = terms
        self.constant = constant

    def __add__(self, other: object) -> "_FakeScipExpr":
        if isinstance(other, _FakeScipExpr):
            return _FakeScipExpr((*self.terms, *other.terms), self.constant + other.constant)
        return _FakeScipExpr(self.terms, self.constant + int(other))

    def __radd__(self, other: object) -> "_FakeScipExpr":
        return self + other

    def __mul__(self, other: object) -> "_FakeScipExpr":
        return _FakeScipExpr(tuple((int(other) * coefficient, name) for coefficient, name in self.terms), int(other) * self.constant)

    def __rmul__(self, other: object) -> "_FakeScipExpr":
        return self * other

    def __ge__(self, other: object) -> tuple[str, "_FakeScipExpr", int]:
        return (">=", self, int(other))

    def __le__(self, other: object) -> tuple[str, "_FakeScipExpr", int]:
        return ("<=", self, int(other))


class _FakeScipVar(_FakeScipExpr):
    def __init__(self, name: str) -> None:
        super().__init__(((1, name),))
        self.name = name


class _FakeScipModel:
    status = "optimal"
    values = {"x": 1}
    valid_solution = True
    dual_bound = 1.0
    instances: list["_FakeScipModel"] = []

    def __init__(self) -> None:
        self.options: dict[str, object] = {}
        self.variables: list[_FakeScipVar] = []
        self.constraints: list[object] = []
        self.objective_sense: str | None = None
        self.certificate_path: str | None = None
        type(self).instances.append(self)

    def hideOutput(self) -> None:
        self.options["hideOutput"] = True

    def setParam(self, name: str, value: object) -> None:
        self.options[name] = value
        if name == "certificate/filename":
            self.certificate_path = str(value)

    def enableExactSolving(self, enabled: bool) -> None:
        self.options["exact/enable"] = enabled

    def addVar(self, **kwargs: object) -> _FakeScipVar:
        variable = _FakeScipVar(str(kwargs["name"]))
        self.variables.append(variable)
        return variable

    def addCons(self, constraint: object, **kwargs: object) -> None:
        self.constraints.append((constraint, kwargs))

    def setMaximize(self) -> None:
        self.objective_sense = "MAX"

    def setMinimize(self) -> None:
        self.objective_sense = "MIN"

    def optimize(self) -> None:
        if self.certificate_path is not None:
            Path(self.certificate_path).write_bytes(b"vipr original certificate\n")

    def getStatus(self) -> str:
        return type(self).status

    def getBestSol(self) -> object | None:
        return object() if type(self).valid_solution else None

    def getSolVal(self, solution: object, variable: _FakeScipVar) -> object:
        return type(self).values[variable.name]

    def getDualbound(self) -> float:
        return type(self).dual_bound


@pytest.fixture
def fake_pyscipopt(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    _FakeScipModel.instances.clear()
    _FakeScipModel.status = "optimal"
    _FakeScipModel.values = {"x": 1}
    _FakeScipModel.valid_solution = True
    _FakeScipModel.dual_bound = 1.0
    module = SimpleNamespace(Model=_FakeScipModel)
    monkeypatch.setattr(
        solver_backends.importlib,
        "import_module",
        lambda name: module if name == "pyscipopt" else (_ for _ in ()).throw(AssertionError(name)),
    )
    return module


def test_fake_scip_maps_optimal_and_infeasible_claims(fake_pyscipopt: SimpleNamespace) -> None:
    model = _one_variable_model()
    _FakeScipModel.status = "optimal"
    _FakeScipModel.values = {"x": 1}
    optimal = ScipBackend().solve(model, time_limit_ms=321)
    assert optimal.status == NativeSolveStatus.OPTIMAL
    assert optimal.values == (("x", 1),)
    assert optimal.objective_value == 1

    _FakeScipModel.status = "infeasible"
    _FakeScipModel.valid_solution = False
    infeasible = ScipBackend().solve(model, time_limit_ms=321)
    assert infeasible.status == NativeSolveStatus.INFEASIBLE
    assert infeasible.values == ()


@pytest.mark.parametrize("status", ("timelimit", "memlimit", "nodelimit", "gaplimit", "unknown"))
def test_fake_scip_limits_and_unknown_statuses_are_unknown(fake_pyscipopt: SimpleNamespace, status: str) -> None:
    _FakeScipModel.status = status
    _FakeScipModel.valid_solution = True
    _FakeScipModel.values = {"x": 1}
    result = ScipBackend().solve(_one_variable_model(), time_limit_ms=1)
    assert result.status == NativeSolveStatus.UNKNOWN
    assert result.values == ()


def test_scip_exact_replay_does_not_interpolate_controlled_certificate_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeModelWithProblem(_FakeScipModel):
        def writeProblem(self, filename: str, **kwargs: object) -> None:
            Path(filename).write_bytes(b"MPS")

    monkeypatch.setattr(
        solver_backends.importlib,
        "import_module",
        lambda name: SimpleNamespace(Model=FakeModelWithProblem) if name == "pyscipopt" else (_ for _ in ()).throw(AssertionError(name)),
    )
    cli = _write_executable(
        tmp_path / "scip",
        "#!/usr/bin/env python3\nfrom pathlib import Path\nPath.cwd().joinpath('__scip_exact_certificate.vipr').write_bytes(b'cert')\n",
    )
    monkeypatch.setattr(solver_backends, "_scip_cli", lambda: str(cli))
    _install_formal_tools(tmp_path, monkeypatch, _one_variable_vipr_prefix(objective=-1))
    escaped = tmp_path / "escaped.mps"
    requested = f"request.vipr\nread {escaped}\nwrite problem {escaped}"

    result = ScipBackend().solve(
        _one_variable_model(),
        time_limit_ms=5_000,
        formal=True,
        artifact_dir=tmp_path,
        certificate_path=requested,
    )

    assert result.status == NativeSolveStatus.OPTIMAL, result
    assert (tmp_path / requested).is_file()
    assert not escaped.exists()


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _one_variable_min_model(*, objective: int = 1) -> LinearModel:
    return LinearModel((IntVariable("x", 0, 1),), (), LinearObjective("MIN", (("x", objective),)))


def _one_variable_vipr_prefix(*, objective: int = 1, value: int = 1, lower: int | None = None, upper: int | None = None) -> str:
    transformed = objective
    if lower is None:
        lower = transformed
    if upper is None:
        upper = transformed
    return "\n".join(
        (
            "VER 1.0",
            "VAR 1",
            "t_v0",
            "INT 1",
            "0",
            "OBJ min",
            f"1 0 {transformed}",
            "CON 2 2",
            "B0 G 0 1 0 1",
            "B1 L 1 1 0 1",
            f"RTP range {lower} {upper}",
            "SOL 1",
            f"best 1 0 {value}",
            "DER 0",
            "",
        )
    )


def _install_formal_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    cli = _write_executable(
        tmp_path / "scip",
        "#!/usr/bin/env python3\nfrom pathlib import Path\n"
        f"Path.cwd().joinpath('__scip_exact_certificate.vipr').write_text({prefix!r})\n",
    )
    comp = _write_executable(
        tmp_path / "viprcomp",
        "#!/usr/bin/env python3\nfrom pathlib import Path\nimport sys\n"
        "source = Path(sys.argv[-1])\n"
        "source.with_name(source.stem + '_complete' + source.suffix).write_bytes(source.read_bytes())\n",
    )
    chk = _write_executable(tmp_path / "viprchk", "#!/usr/bin/env python3\n")
    monkeypatch.setattr(solver_backends, "_scip_cli", lambda: str(cli))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    assert comp.is_file() and chk.is_file()


def test_formal_production_path_does_not_optimize_ordinary_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class WriteOnlyModel(_FakeScipModel):
        def writeProblem(self, filename: str, **kwargs: object) -> None:
            Path(filename).write_bytes(b"MPS")

        def optimize(self) -> None:
            raise AssertionError("formal mode must not use ordinary optimize claim")

    monkeypatch.setattr(
        solver_backends.importlib,
        "import_module",
        lambda name: SimpleNamespace(Model=WriteOnlyModel) if name == "pyscipopt" else (_ for _ in ()).throw(AssertionError(name)),
    )
    _install_formal_tools(tmp_path, monkeypatch, _one_variable_vipr_prefix(objective=-1))

    result = ScipBackend().solve(
        _one_variable_model(), time_limit_ms=5_000, formal=True, artifact_dir=tmp_path, certificate_path="request.vipr"
    )

    assert result.status == NativeSolveStatus.OPTIMAL
    assert dict(result.values) == {"x": 1}


def test_formal_mps_write_failure_is_proof_unclosed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FailingModel(_FakeScipModel):
        def writeProblem(self, filename: str, **kwargs: object) -> None:
            raise OSError("MPS write failed")

    monkeypatch.setattr(
        solver_backends.importlib,
        "import_module",
        lambda name: SimpleNamespace(Model=FailingModel) if name == "pyscipopt" else (_ for _ in ()).throw(AssertionError(name)),
    )

    result = ScipBackend().solve(
        _one_variable_model(), time_limit_ms=321, formal=True, artifact_dir=tmp_path, certificate_path="request.vipr"
    )

    assert result.status == NativeSolveStatus.UNKNOWN
    assert "PROOF_UNCLOSED" in result.native_status


@pytest.mark.parametrize(
    "source, expected",
    (
        ("#!/bin/sh\nexit 7\n", "PROOF_UNCLOSED"),
        ("#!/bin/sh\nsleep 10\n", "PROOF_UNCLOSED"),
    ),
)
def test_formal_exact_cli_nonzero_or_timeout_is_proof_unclosed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, expected: str
) -> None:
    class WriteOnlyModel(_FakeScipModel):
        def writeProblem(self, filename: str, **kwargs: object) -> None:
            Path(filename).write_bytes(b"MPS")

    monkeypatch.setattr(
        solver_backends.importlib,
        "import_module",
        lambda name: SimpleNamespace(Model=WriteOnlyModel) if name == "pyscipopt" else (_ for _ in ()).throw(AssertionError(name)),
    )
    cli = _write_executable(tmp_path / "scip", source)
    monkeypatch.setattr(solver_backends, "_scip_cli", lambda: str(cli))
    limit = 10 if "sleep" in source else 321

    result = ScipBackend().solve(
        _one_variable_model(), time_limit_ms=limit, formal=True, artifact_dir=tmp_path, certificate_path="request.vipr"
    )

    assert result.status == NativeSolveStatus.UNKNOWN
    assert expected in result.native_status


def test_formal_rejects_lossy_mps_certificate_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class WriteOnlyModel(_FakeScipModel):
        def writeProblem(self, filename: str, **kwargs: object) -> None:
            Path(filename).write_bytes(b"MPS")

    monkeypatch.setattr(
        solver_backends.importlib,
        "import_module",
        lambda name: SimpleNamespace(Model=WriteOnlyModel) if name == "pyscipopt" else (_ for _ in ()).throw(AssertionError(name)),
    )
    _install_formal_tools(tmp_path, monkeypatch, _one_variable_vipr_prefix(objective=9007199254740990, value=1))

    result = ScipBackend().solve(
        _one_variable_model(objective=2**53),
        time_limit_ms=321,
        formal=True,
        artifact_dir=tmp_path,
        certificate_path="request.vipr",
    )

    assert result.status == NativeSolveStatus.UNKNOWN
    assert "PROOF_UNCLOSED" in result.native_status


def test_formal_rejects_certificate_claim_value_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class WriteOnlyModel(_FakeScipModel):
        def writeProblem(self, filename: str, **kwargs: object) -> None:
            Path(filename).write_bytes(b"MPS")

    monkeypatch.setattr(
        solver_backends.importlib,
        "import_module",
        lambda name: SimpleNamespace(Model=WriteOnlyModel) if name == "pyscipopt" else (_ for _ in ()).throw(AssertionError(name)),
    )
    _install_formal_tools(tmp_path, monkeypatch, _one_variable_vipr_prefix(objective=-1, value=1, lower=0, upper=0))

    result = ScipBackend().solve(
        _one_variable_model(), time_limit_ms=321, formal=True, artifact_dir=tmp_path, certificate_path="request.vipr"
    )

    assert result.status == NativeSolveStatus.UNKNOWN
    assert "PROOF_UNCLOSED" in result.native_status


def test_vipr_helper_completes_and_checks_in_separate_single_threaded_processes(tmp_path: Path) -> None:
    original = tmp_path / "original.vipr"
    original.write_bytes(b"original")
    comp = _write_executable(
        tmp_path / "viprcomp",
        "#!/usr/bin/env python3\nfrom pathlib import Path\nimport sys\n"
        "source = Path(sys.argv[-1])\n"
        "source.with_name(source.stem + '_complete' + source.suffix).write_bytes(source.read_bytes() + b' completed')\n",
    )
    chk = _write_executable(
        tmp_path / "viprchk",
        "#!/usr/bin/env python3\nfrom pathlib import Path\nimport sys\nassert Path(sys.argv[-1]).read_bytes().endswith(b' completed')\nassert __import__('os').environ.get('OMP_NUM_THREADS') == '1'\n",
    )

    result = check_vipr_certificate(original, tmp_path, viprcomp=str(comp), viprchk=str(chk), timeout_ms=1_000)

    assert result.checker_succeeded is True
    assert result.certificate_size_bytes == len(b"original")
    assert result.completed_certificate_size_bytes == len(b"original completed")
    assert result.certificate_sha256.startswith("sha256:")
    assert result.completed_certificate_sha256.startswith("sha256:")
    assert result.completion_ns > 0
    assert result.check_ns > 0


def test_vipr_helper_missing_or_failed_checker_is_not_proof(tmp_path: Path) -> None:
    original = tmp_path / "original.vipr"
    original.write_bytes(b"corrupt")
    chk = _write_executable(tmp_path / "viprchk", "#!/usr/bin/env python3\nraise SystemExit(17)\n")
    comp = _write_executable(tmp_path / "viprcomp", "#!/usr/bin/env python3\nraise SystemExit(19)\n")

    result = check_vipr_certificate(original, tmp_path, viprcomp=str(comp), viprchk=str(chk), timeout_ms=1_000)

    assert result.checker_succeeded is False
    assert result.checker_exit_code == 19
    assert result.completed_certificate_sha256 is None


def test_vipr_helper_maps_missing_certificate_and_checker_timeout_to_failure(tmp_path: Path) -> None:
    missing = check_vipr_certificate(tmp_path / "missing.vipr", tmp_path, timeout_ms=1_000)
    assert missing.checker_succeeded is False
    assert missing.error == "certificate is missing"

    original = tmp_path / "original.vipr"
    original.write_bytes(b"corrupt")
    comp = _write_executable(tmp_path / "viprcomp", "#!/usr/bin/env python3\nimport sys\n")
    chk = _write_executable(tmp_path / "viprchk", "#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n")
    timed_out = check_vipr_certificate(original, tmp_path, viprcomp=str(comp), viprchk=str(chk), timeout_ms=10)
    assert timed_out.checker_succeeded is False
    assert timed_out.checker_exit_code is None
    assert timed_out.error == "VIPR subprocess timed out"


def test_vipr_helper_rejects_certificate_outside_request_artifact_dir(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.vipr"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="artifact directory"):
        check_vipr_certificate(outside, tmp_path, viprcomp="unused", viprchk="unused", timeout_ms=1_000)
