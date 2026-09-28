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
            ["make", "-n", "test", f"SERVICE={service}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert included in preview
        assert excluded not in preview

    suites = {
        service: set(re.findall(r"tests/test_\w+\.py", subprocess.run(
            ["make", "-n", "test", f"SERVICE={service}"],
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
