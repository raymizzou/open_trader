from __future__ import annotations

import json
import subprocess
from pathlib import Path


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
