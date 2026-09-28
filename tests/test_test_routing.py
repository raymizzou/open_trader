from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_backend_tests_require_an_explicit_scope() -> None:
    for service, included, excluded in (
        ("gateway", "tests/test_frontend_gateway.py", "tests/test_dashboard_web.py"),
        ("legacy", "tests/test_dashboard_web.py", "tests/test_frontend_gateway.py"),
        ("account", "tests/test_account_api.py", "tests/test_prediction_service.py"),
        ("prediction", "tests/test_prediction_service.py", "tests/test_account_api.py"),
    ):
        preview = subprocess.run(
            ["make", "-n", "test", f"SERVICE={service}", "TEST_N_LEG=1"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert included in preview
        assert excluded not in preview

    suites = {
        service: set(re.findall(r"tests/test_\w+\.py", subprocess.run(
            ["make", "-n", "test", f"SERVICE={service}", "TEST_N_LEG=1"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout))
        for service in ("gateway", "legacy", "account", "prediction")
    }
    all_tests = {str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_*.py")}
    assert set().union(*suites.values()) == all_tests
    assert sum(map(len, suites.values())) == len(all_tests)

    combined = subprocess.run(
        ["make", "-n", "test", "SERVICE=gateway prediction"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "tests/test_frontend_gateway.py" in combined
    assert "tests/test_prediction_service.py" in combined

    unscoped = subprocess.run(
        ["make", "test", "DOCKER=true"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert unscoped.returncode != 0
    assert "SERVICE or TEST" in unscoped.stdout + unscoped.stderr

    unknown = subprocess.run(
        ["make", "test", "SERVICE=unknown", "DOCKER=true"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert unknown.returncode != 0
    assert "Unknown SERVICE" in unknown.stdout + unknown.stderr

    focused = subprocess.run(
        ["make", "-n", "test", "TEST=tests/test_test_routing.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "tests/test_test_routing.py" in focused
    assert "tests/test_account_api.py" not in focused


def test_prediction_parallelism_preserves_scope_and_serial_override() -> None:
    for scope, workers, expected in (
        ("SERVICE=prediction", None, "-n 6 --dist=loadgroup"),
        ("SERVICE=prediction", "4", "-n 4 --dist=loadgroup"),
        ("SERVICE=prediction", "6", "-n 6 --dist=loadgroup"),
        ("SERVICE=prediction", "1", None),
        ("SERVICE=account", None, None),
        ("TEST=tests/test_prediction_service.py", None, None),
        ("TEST=tests/test_prediction_service.py", "6", "-n 6 --dist=loadgroup"),
    ):
        command = ["make", "-n", "test", scope]
        if workers is not None:
            command.append(f"TEST_WORKERS={workers}")
        preview = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        if expected is None:
            assert "--dist=" not in preview
        else:
            assert expected in preview
        if "prediction" in scope:
            assert "tests/test_prediction_service.py" in preview
            assert "tests/test_account_api.py" not in preview

    candidate = subprocess.run(
        ["make", "-n", "candidate-acceptance", "TEST_WORKERS=6"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert "--dist=" not in candidate


def test_n_leg_development_pause_is_reversible_and_preserves_active_services() -> None:
    def preview(*arguments: str) -> str:
        return subprocess.run(
            ["make", "-n", *arguments], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout

    active = preview("test", "SERVICE=prediction")
    restored = preview("test", "SERVICE=prediction", "TEST_N_LEG=1")
    for filename in (
        "test_prediction_n_leg_execution.py", "test_run_nleg_no_submit_validation.py",
        "test_prediction_solver_benchmark.py", "test_prediction_solver_worker.py",
        "test_prediction_live_resolver.py", "test_prediction_partial_fill.py",
        "test_prediction_monitor_selection_driver.py",
    ):
        assert f"tests/{filename}" not in active
        assert f"tests/{filename}" in restored
    for filename in (
        "test_polymarket_lp.py", "test_prediction_runtime.py",
        "test_prediction_service.py", "test_prediction_arbitrage_store.py",
        "test_prediction_release_launchd.py", "test_prediction_n_leg.py",
    ):
        assert f"tests/{filename}" in active
    assert "N-leg dedicated tests paused" in active
    assert "N-leg dedicated tests paused" not in restored
    assert active == preview("test", "SERVICE=prediction", "TEST_N_LEG=0")
    assert "tests/test_prediction_n_leg_execution.py" not in preview(
        "test", "SERVICE=gateway prediction"
    )
    assert "tests/test_frontend_gateway.py" in preview("test", "SERVICE=gateway prediction")
    focused = preview("test", "TEST=tests/test_prediction_n_leg_execution.py")
    assert "tests/test_prediction_n_leg_execution.py" in focused
    assert "N-leg dedicated tests paused" not in focused
    assert preview("candidate-acceptance", "TEST_N_LEG=0") == preview(
        "candidate-acceptance", "TEST_N_LEG=1"
    )
    for value in ("", "bad", "0 1"):
        invalid = subprocess.run(
            ["make", "test", "SERVICE=prediction", f"TEST_N_LEG={value}", "DOCKER=true"],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert invalid.returncode != 0
        assert "TEST_N_LEG must be 0 or 1" in invalid.stdout + invalid.stderr
