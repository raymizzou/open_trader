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


def test_n_leg_manual_diagnostics_preserve_active_services() -> None:
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
    ):
        assert f"tests/{filename}" not in active
        assert f"tests/{filename}" in restored
    for filename in (
        "test_polymarket_lp.py", "test_prediction_runtime.py",
        "test_prediction_service.py", "test_prediction_arbitrage_store.py",
        "test_prediction_release_launchd.py", "test_prediction_n_leg.py",
        "test_prediction_monitor_selection_driver.py", "test_prediction_n_leg_cutover.py", "test_run_nleg_cutover.py",
    ):
        assert f"tests/{filename}" in active
    assert "N-leg permanently retired" in active
    assert "N-leg permanently retired" not in restored
    assert active == preview("test", "SERVICE=prediction", "TEST_N_LEG=0")
    assert "tests/test_prediction_n_leg_execution.py" not in preview(
        "test", "SERVICE=gateway prediction"
    )
    assert "tests/test_frontend_gateway.py" in preview("test", "SERVICE=gateway prediction")
    focused = preview("test", "TEST=tests/test_prediction_n_leg_execution.py")
    assert "tests/test_prediction_n_leg_execution.py" in focused
    assert "N-leg permanently retired" not in focused
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


def test_explicit_retirement_manifest_preserves_shared_and_new_tests(tmp_path) -> None:
    import json
    import shutil
    root = tmp_path / 'repo'
    root.mkdir()
    shutil.copy(ROOT / 'Makefile', root / 'Makefile')
    shutil.copytree(ROOT / 'scripts', root / 'scripts')
    (root / 'tests').mkdir()
    for source in (ROOT / 'tests').glob('test_*.py'):
        (root / 'tests' / source.name).write_text('def test_example(): pass\n')
    (root / 'tests/test_prediction_n_leg_new.py').write_text('def test_new(): pass\n')
    (root / 'tests/nested').mkdir()
    (root / 'tests/nested/test_new.py').write_text('def test_new(): pass\n')
    def selection(*args):
        output = subprocess.check_output(['make', '-sn', 'test', 'SERVICE=prediction', *args], cwd=root, text=True)
        return set(re.findall(r'tests/[\w/]+\.py', output))
    active = selection()
    for filename in ['test_prediction_monitor_selection_driver.py', 'test_prediction_n_leg_cutover.py',
                     'test_run_nleg_cutover.py', 'test_prediction_runtime.py', 'test_prediction_service.py',
                     'test_prediction_n_leg.py', 'test_prediction_n_leg_new.py', 'test_polymarket_lp.py',
                     'test_polymarket_trading.py', 'test_prediction_arbitrage_store.py']:
        assert 'tests/' + filename in active
    assert {str(p.relative_to(ROOT)) for p in (ROOT/'tests').glob('test_lp_*.py')} <= active
    manifest = json.loads((root/'scripts/ci_nleg_retired.json').read_text())
    retired = {item['path'] for item in manifest['retired']}
    assert len(retired) == 29
    assert not active & retired
    assert selection('TEST_N_LEG=1') - active == retired
    assert active == selection('TEST_N_LEG=0')
    explicit = subprocess.check_output(['make','-sn','test','TEST=tests/test_prediction_n_leg_validation.py'],cwd=root,text=True)
    assert 'tests/test_prediction_n_leg_validation.py' in explicit
    parts = subprocess.check_output(['make','-s','ci-test-files'],cwd=root,text=True)
    assert 'tests/nested/test_new.py' in parts


def test_invalid_retirement_manifest_fails_before_execution(tmp_path) -> None:
    import copy
    import json
    manifest = json.loads((ROOT/'scripts/ci_nleg_retired.json').read_text())
    broken = []
    duplicate = copy.deepcopy(manifest); duplicate['retired'].append(duplicate['retired'][0]); broken.append(duplicate)
    for path in ['tests/test_missing.py', '../tests/test_prediction_solver.py',
                 'tests/test_prediction_runtime.py', 'tests/test_prediction_n_leg_cutover.py']:
        item = copy.deepcopy(manifest); item['retired'][0]['path'] = path; broken.append(item)
    wrong_reason = copy.deepcopy(manifest); wrong_reason['retired'][0]['reason']='temporary pause'; broken.append(wrong_reason)
    unknown = copy.deepcopy(manifest); unknown['policy']='unknown'; broken.append(unknown)
    for index, data in enumerate(broken):
        target=tmp_path/f'bad-{index}.json'; target.write_text(json.dumps(data))
        run=subprocess.run(['make','test','SERVICE=prediction',f'N_LEG_MANIFEST={target}','DOCKER=must-not-run'],
                           cwd=ROOT,capture_output=True,text=True)
        assert run.returncode != 0
        assert 'Invalid retirement selection' in run.stderr
        assert 'must-not-run build' not in run.stdout
