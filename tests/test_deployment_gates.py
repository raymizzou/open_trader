from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]


SERVICE_PORTS = {
    "gateway": ("http://127.0.0.1:8766", 8766, 4102),
    "legacy": ("http://127.0.0.1:8767", 8767, 4103),
    "account": ("http://127.0.0.1:8768", 8768, 4104),
    "prediction": ("http://127.0.0.1:8769", 8769, 4105),
}
DEFAULT_RELEASE_SERVICES = "gateway legacy account prediction"


def _create_release_fixture(tmp_path: Path, *, failure: str | None = None) -> tuple[Path, str, Path]:
    release = tmp_path / "release"
    release.mkdir()
    checker = ROOT / "scripts/check_production_log.py"
    (release / "scripts").mkdir()
    shutil.copyfile(checker, release / "scripts/check_production_log.py")
    for service in SERVICE_PORTS:
        log_dir = release / "logs" / {
            "gateway": "frontend_gateway",
            "legacy": "legacy_dashboard",
            "account": "account_api",
        }.get(service, "")
        if log_dir.name:
            log_dir.mkdir(parents=True, exist_ok=True)
            log = log_dir / "launchd.err.log"
            log.write_text(
                "ERROR: selected service failure\n"
                if failure == f"{service}_log"
                else "clean\n",
                encoding="utf-8",
            )
            if service == "account":
                sync_log = release / "logs/account_sync/launchd.err.log"
                sync_log.parent.mkdir(parents=True, exist_ok=True)
                sync_log.write_text("clean\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    prediction_log = runtime / "logs/prediction_service"
    prediction_log.mkdir(parents=True)
    (prediction_log / "launchd.err.log").write_text(
        "ERROR: selected service failure\n"
        if failure == "prediction_log"
        else "clean\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "init", "--quiet", str(release)], check=True)
    subprocess.run(
        ["git", "-C", str(release), "config", "user.name", "scope-test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(release), "config", "user.email", "scope@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(release), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(release), "commit", "--quiet", "-m", "release fixture"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(release), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(release), "checkout", "--quiet", "--detach", "HEAD"],
        check=True,
    )
    return release, sha, runtime


def _run_smoke(
    tmp_path: Path,
    *,
    services: str | None,
    failure: str | None = None,
    n_leg_paused: int = 0,
    prediction_n_leg: tuple[str, str] = ("paused", "N_LEG_PAUSED"),
    unselected_prediction_paused: bool = False,
    lp_payload: str = '{"state":"ready","orders":[],"positions":[],"recommendations":[]}',
    log_content: str | bytes | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    selected_services = services or DEFAULT_RELEASE_SERVICES
    release, sha, runtime = _create_release_fixture(tmp_path, failure=failure)
    prediction_log = runtime / "logs/prediction_service/launchd.err.log"
    if log_content is not None:
        prediction_log.write_bytes(log_content.encode("utf-8") if isinstance(log_content, str) else log_content)
    if failure == "log_missing":
        prediction_log.unlink()
    elif failure == "log_read":
        prediction_log.unlink()
        prediction_log.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    calls = tmp_path / "calls"
    old_sha = "0" * 40
    old_roots = {
        service: str(tmp_path / f"old-{service}")
        for service in SERVICE_PORTS
    }
    health: dict[str, dict[str, object]] = {}
    for service, (_url, _port, pid) in SERVICE_PORTS.items():
        common: dict[str, object] = {
            "pid": pid,
            "cwd": str(release),
            "source_state": "clean",
            "git_sha": sha,
            "code_root": str(release / "src"),
        }
        if service == "gateway":
            common.update(
                schema_version="open_trader.frontend_gateway.health.v1",
                module="frontend_gateway",
                legacy_upstream_status="ok",
                account_upstream_status="ok",
                prediction_upstream_status="ok",
                prediction_route_mode="service",
            )
        elif service == "legacy":
            common.update(
                schema_version="open_trader.legacy_dashboard.health.v1",
                module="legacy_dashboard",
            )
        elif service == "prediction":
            common.update(
                schema_version="open_trader.prediction_service.health.v1",
                module="prediction_service",
                status="running",
                mode="production",
                production_owner=True,
                mutations="enabled",
                n_leg={"status": prediction_n_leg[0], "code": prediction_n_leg[1]},
            )
        else:
            common = {
                "pid": pid,
                "api_git_sha": sha,
                "worker_git_sha": sha,
                "code_root": str(release / "src"),
                "worker_code_root": str(release / "src"),
                "schema_version": "open_trader.account_api.health.v1",
                "module": "account_api",
                "status": "ok",
                "mode": "production",
                "release_match": True,
            }
        health[service] = common

        old = json.loads(json.dumps(common))
        if service == "prediction" and unselected_prediction_paused:
            old["n_leg"] = {"status": "paused", "code": "N_LEG_PAUSED"}
        if service == "account":
            old.update(
                api_git_sha=old_sha,
                worker_git_sha=old_sha,
                code_root=old_roots[service],
                worker_code_root=old_roots[service],
            )
        else:
            old.update(git_sha=old_sha, cwd=old_roots[service], code_root=old_roots[service])
        health[f"old_{service}"] = old

    if failure == "default_mixed":
        health["legacy"] = health["old_legacy"]
    if failure == "gateway_sha":
        health["gateway"]["git_sha"] = old_sha
    elif failure == "gateway_root":
        health["gateway"]["code_root"] = str(tmp_path / "outside")
    elif failure == "account_worker":
        health["account"]["worker_git_sha"] = old_sha

    curl_lines = [
        "#!/bin/bash",
        'echo "curl $*" >> "$FAKE_CALLS"',
        'url="${@: -1}"',
    ]
    for service, (url, _port, _pid) in SERVICE_PORTS.items():
        curl_lines.extend(
            [
                f'if [[ "$url" == "{url}/healthz" ]]; then',
                f"  printf '%s\\n' '{json.dumps(health[service if service in selected_services.split() else f'old_{service}'], separators=(',', ':'))}'",
                "  exit 0",
                "fi",
            ]
        )
    curl_lines.extend(
        [
            'if [[ "$url" == "http://127.0.0.1:8769/api/prediction-arbitrage/lp/dashboard" ]]; then',
            '  printf "%s\\n" "$FAKE_LP_PAYLOAD"',
            "  exit 0",
            "fi",
            'if [[ "$url" == "http://127.0.0.1:8769/api/prediction-arbitrage/state" ]]; then',
            "  printf '%s\\n' '{\"n_leg\":{\"contract_generation\":2,\"mode\":\"MANUAL\",\"execution_scopes\":{\"SAME_EVENT_SAME_VENUE\":{\"capability\":\"OBSERVE_ONLY\"}}},\"opportunities\":[]}'",
            "  exit 0",
            "fi",
            "exit 22",
        ]
    )
    _write_executable(fake_bin / "curl", "\n".join(curl_lines) + "\n")

    _write_executable(
        fake_bin / "python",
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then\n'
        '  echo "python browser" >> "$FAKE_CALLS"\n'
        "  exit 0\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
    )
    lsof = [
        "#!/bin/bash",
        'echo "lsof $*" >> "$FAKE_CALLS"',
        'if [[ "$*" == *" -d cwd "* ]]; then printf "n%s\\n" "$EXPECTED_ROOT"; exit 0; fi',
    ]
    for service, (_url, port, pid) in SERVICE_PORTS.items():
        listener_pid = "9999" if failure == f"{service}_listener" else str(pid)
        lsof.append(f'if [[ "$*" == *"tiTCP:{port}"* ]]; then printf "%s\\n" "{listener_pid}"; exit 0; fi')
    lsof.append("exit 1")
    _write_executable(fake_bin / "lsof", "\n".join(lsof) + "\n")
    _write_executable(
        fake_bin / "ps",
        "#!/bin/sh\n"
        'if [ "$1" = "-p" ]; then exit 0; fi\n'
        "exec /bin/ps \"$@\"\n",
    )
    _write_executable(
        fake_bin / "rg",
        "#!/bin/sh\n" + ("exit 127\n" if failure == "rg_missing" else "exec grep -E \"$@\"\n"),
    )
    playwright = runtime / "node_modules/.bin/playwright"
    playwright.parent.mkdir(parents=True)
    _write_executable(
        playwright,
        "#!/bin/sh\n"
        'echo "playwright $*" >> "$FAKE_CALLS"\n'
        + ("exit 1\n" if failure == "browser" else "exit 0\n"),
    )

    environment = dict(os.environ)
    environment.update(
        PATH=f"{fake_bin}{os.pathsep}{environment['PATH']}",
        FAKE_CALLS=str(calls),
        EXPECTED_ROOT=str(release),
        FAKE_LP_PAYLOAD=lp_payload,
    )
    make_args = ["make", "production-smoke"]
    if services is not None:
        make_args.append(f"RELEASE_SERVICES={services}")
    make_args.extend(
        [
            f"PYTHON_BIN={fake_bin / 'python'}",
            f"REPOSITORY_ROOT={runtime}",
            f"PLAYWRIGHT_NODE_PATH={runtime / 'node_modules'}",
            f"EXPECTED_SHA={sha}",
            f"EXPECTED_ROOT={release}",
            f"EXPECTED_RUNTIME_ROOT={runtime}",
            f"N_LEG_PAUSED={n_leg_paused}",
        ]
    )
    result = subprocess.run(
        make_args,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return result, calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_host_readiness(
    tmp_path: Path,
    *,
    browser_available: bool,
    services: str = DEFAULT_RELEASE_SERVICES,
    fail_components: bool = False,
    first_deploy: bool = False,
    existing_listener: bool = False,
    existing_plist: bool = False,
    loaded_label: bool = False,
    prediction_listener: bool = True,
    manager_status: str = "READY",
    manager_exit: int = 0,
    manager_payload: str | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    calls = tmp_path / "calls"
    fake_python = fake_bin / "python"
    fail = "1" if fail_components else "0"
    _write_executable(
        fake_python,
        f"""#!/bin/sh
echo "python $*" >> "$FAKE_CALLS"
if [ "$1" = "-c" ]; then
    case "$2" in *open_trader.prediction_service.preflight.v1*) exec "{sys.executable}" "$@" ;; esac
fi
if [ "$1" = "-m" ] && [ "$2" = "open_trader" ] && [ "$3" = "prediction-arb" ] && [ "$4" = "nleg-validate" ]; then
    [ "{fail}" = "1" ] && exit 1
    printf '%s\\n' '{{"replay":{{"status":"PASS"}},"live":{{"reason":"LIVE_CATALOG_UNAVAILABLE"}}}}'
    exit 0
fi
case "$*" in
    *"account-sync-status"*|*"prediction-arb wallet"*) [ "{fail}" = "1" ] && exit 124 ;;
    *"socket.create_connection"*) [ "{fail}" = "1" ] && exit 124 ;;
esac
exit 0
""",
    )
    manager_response=tmp_path/"manager-response.json"
    manager_response.write_text(manager_payload if manager_payload is not None else json.dumps({
        "schema_version":"open_trader.prediction_service.preflight.v1","status":manager_status,
        "managed_identity_verified":True}))
    # The supported manager CLI is the external boundary for this Make consumer.
    # The manager itself is exercised by the separate real installer regressions.
    _write_executable(fake_bin/"bash", f"""#!/bin/sh
case "$1" in */scripts/install_prediction_service_launchd.sh)
    echo "prediction-manager $*" >> "$FAKE_CALLS"
    cat "{manager_response}"
    exit {manager_exit}
    ;;
esac
exec /bin/bash "$@"
""")
    lsof_mode = (
        "exit 1"
        if first_deploy and not existing_listener
        else (
            'case "$*" in *8766*) exit 0 ;; *) exit 1 ;; esac'
            if fail_components
            else "exit 0"
        )
    )
    if not prediction_listener:
        lsof_mode='case "$*" in *8769*) exit 1 ;; esac\n'+lsof_mode
    _write_executable(
        fake_bin / "lsof",
        f'#!/bin/sh\necho "lsof $*" >> "$FAKE_CALLS"\n{lsof_mode}\n',
    )
    _write_executable(
        fake_bin / "node",
        '#!/bin/sh\necho "node $*" >> "$FAKE_CALLS"\nexit %s\n'
        % ("0" if browser_available else "1"),
    )
    _write_executable(
        fake_bin / "launchctl",
        '#!/bin/sh\necho "launchctl $*" >> "$FAKE_CALLS"\n'
        + ('printf "1\\t0\\tcom.open-trader.account-api\\n"\n' if loaded_label else '')
        + 'exit 0\n',
    )

    fake_home = tmp_path / "home"
    launch_agents = fake_home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    if existing_plist:
        (launch_agents / "com.open-trader.account-api.plist").write_text("old")

    runtime_root = tmp_path / "runtime"
    playwright_bin = runtime_root / "node_modules" / ".bin" / "playwright"
    playwright_bin.parent.mkdir(parents=True)
    if browser_available:
        _write_executable(
            playwright_bin,
            '#!/bin/sh\necho "playwright $*" >> "$FAKE_CALLS"\nexit 0\n',
        )

    daily_config = tmp_path / "daily_premarket.env"
    daily_config.write_text(
        f"OPEN_TRADER_REPO={ROOT}\n"
        f"OPEN_TRADER_PYTHON={fake_python}\n"
        "OPEN_TRADER_TREND_EXECUTOR_HOST=never-this-host\n",
        encoding="utf-8",
    )

    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["TMPDIR"] = str(tmp_path)
    environment["FAKE_CALLS"] = str(calls)
    environment["HOME"] = str(fake_home)
    return subprocess.run(
        [
            "make",
            "host-readiness",
            f"RELEASE_SERVICES={services}",
            f"PYTHON_BIN={fake_python}",
            f"REPOSITORY_ROOT={runtime_root}",
            f"PLAYWRIGHT_NODE_PATH={runtime_root / 'node_modules'}",
            f"DAILY_CONFIG={daily_config}",
            f"PREDICTION_CONFIG={runtime_root / 'config/prediction_arbitrage.json'}",
            f"FIRST_DEPLOY={int(first_deploy)}",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("status", ["READY", "RECOVERABLE"])
def test_prediction_recovery_readiness_uses_verified_manager_without_old_listener(tmp_path, status):
    import shlex
    result=_run_host_readiness(tmp_path,browser_available=True,services="prediction",
        prediction_listener=False,manager_status=status)
    assert result.returncode==0,result.stdout+result.stderr
    assert result.stdout.rstrip().endswith("READY")
    calls=(tmp_path/"calls").read_text().splitlines()
    manager=[shlex.split(call)[1:] for call in calls if call.startswith("prediction-manager ")]
    assert manager==[[str(ROOT/'scripts/install_prediction_service_launchd.sh'),
        '--mode','production','--preflight','--repo-root',str(ROOT),
        '--runtime-root',str(tmp_path/'runtime'),'--python',str(tmp_path/'bin/python'),
        '--config',str(tmp_path/'runtime/config/prediction_arbitrage.json')]]
    assert not any('lsof ' in call and '8769' in call for call in calls)
    assert not any('curl ' in call or '--restart' in call or '--prepare-target' in call for call in calls)
    assert any('prediction-arb wallet' in call for call in calls)
    assert 'prediction n-leg replay validation: PASS' in result.stdout
    assert 'Playwright Chromium: PASS' in result.stdout and 'storage: PASS' in result.stdout


@pytest.mark.parametrize(("payload","exit_status"), [
    pytest.param('{"schema_version":"open_trader.prediction_service.preflight.v1","status":"BLOCKED","managed_identity_verified":true}',0,id='blocked-status'),
    pytest.param('{"schema_version":"open_trader.prediction_service.preflight.v1","status":"READY","managed_identity_verified":true}',1,id='producer-nonzero'),
    pytest.param('{"schema_version":"open_trader.prediction_service.preflight.v1","status":"RECOVERABLE","managed_identity_verified":true}',127,id='producer-unavailable'),
    pytest.param('{"schema_version":"unknown","status":"READY","managed_identity_verified":true}',0,id='unknown-schema'),
    pytest.param('{"schema_version":"open_trader.prediction_service.preflight.v1","status":"UNKNOWN","managed_identity_verified":true}',0,id='unknown-status'),
    pytest.param('{',0,id='malformed-json'),
    pytest.param('[]',0,id='array'),
    pytest.param('null',0,id='null'),
    pytest.param('{"schema_version":"open_trader.prediction_service.preflight.v1","status":"READY"}',0,id='missing-proof'),
    *[pytest.param(json.dumps({"schema_version":"open_trader.prediction_service.preflight.v1",
        "status":"RECOVERABLE","managed_identity_verified":proof}),0,id=name)
      for name,proof in [('false-proof',False),('integer-proof',1),('string-proof','true'),
        ('array-proof',[True]),('object-proof',{'verified':True}),('null-proof',None)]],
])
def test_prediction_recovery_readiness_refuses_unverified_manager(tmp_path,payload,exit_status):
    result=_run_host_readiness(tmp_path,browser_available=True,services="prediction",
        prediction_listener=False,manager_payload=payload,manager_exit=exit_status)
    assert result.returncode!=0,result.stdout+result.stderr
    assert result.stdout.rstrip().endswith("BLOCKED")
    assert 'prediction managed recovery: BLOCKED' in result.stdout
    calls=(tmp_path/'calls').read_text()
    assert '--preflight' in calls and '--restart' not in calls and '--prepare-target' not in calls


def test_host_readiness_does_not_require_old_prediction_state(tmp_path: Path) -> None:
    result = _run_host_readiness(tmp_path, browser_available=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("READY")


def test_first_deploy_readiness_accepts_empty_host_without_old_account(
    tmp_path: Path,
) -> None:
    result = _run_host_readiness(
        tmp_path, browser_available=True, first_deploy=True
    )
    calls = (tmp_path / "calls").read_text(encoding="utf-8")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "fresh host ownership: PASS" in result.stdout
    assert "loopback listeners absent: PASS" in result.stdout
    assert "account status:" not in result.stdout
    assert "account-sync-status" not in calls
    assert "prediction-arb wallet" in calls
    assert result.stdout.rstrip().endswith("READY")


@pytest.mark.parametrize(
    "blocker", ("existing_listener", "existing_plist", "loaded_label")
)
def test_first_deploy_readiness_rejects_existing_owner(
    tmp_path: Path, blocker: str
) -> None:
    result = _run_host_readiness(
        tmp_path, browser_available=True, first_deploy=True, **{blocker: True}
    )

    assert result.returncode != 0
    assert result.stdout.rstrip().endswith("BLOCKED")


def test_first_deploy_readiness_keeps_wallet_and_futu_gates(tmp_path: Path) -> None:
    result = _run_host_readiness(
        tmp_path, browser_available=True, first_deploy=True, fail_components=True
    )

    assert "fresh host ownership: PASS" in result.stdout
    assert "loopback listeners absent: PASS" in result.stdout
    assert "prediction wallet: BLOCKED" in result.stdout
    assert "Futu connectivity: BLOCKED" in result.stdout
    assert result.stdout.rstrip().endswith("BLOCKED")


def test_host_readiness_still_blocks_missing_browser(tmp_path: Path) -> None:
    result = _run_host_readiness(tmp_path, browser_available=False)

    assert result.returncode != 0
    assert "Playwright Chromium: BLOCKED" in result.stdout
    assert result.stdout.rstrip().endswith("BLOCKED")


def test_scoped_host_readiness_ignores_unselected_component_blockers(
    tmp_path: Path,
) -> None:
    result = _run_host_readiness(
        tmp_path,
        browser_available=True,
        services="gateway",
        fail_components=True,
    )
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("READY")
    assert "gateway launchd dry-run: PASS" in result.stdout
    assert not any("account-sync-status" in call for call in calls)
    assert not any("prediction-arb" in call for call in calls)
    assert not any("socket.create_connection" in call for call in calls)
    assert any("-iTCP:8766" in call for call in calls)
    assert not any(
        f"-iTCP:{port}" in call for port in (8767, 8768, 8769) for call in calls
    )


def test_scoped_host_readiness_still_runs_prediction_gates(
    tmp_path: Path,
) -> None:
    result = _run_host_readiness(
        tmp_path,
        browser_available=True,
        services="prediction",
        fail_components=True,
    )

    assert result.returncode != 0
    assert "prediction wallet: BLOCKED" in result.stdout
    assert result.stdout.rstrip().endswith("BLOCKED")


def test_scoped_host_readiness_still_runs_account_and_legacy_gates(
    tmp_path: Path,
) -> None:
    account = _run_host_readiness(
        tmp_path / "account",
        browser_available=True,
        services="account",
        fail_components=True,
    )
    legacy = _run_host_readiness(
        tmp_path / "legacy",
        browser_available=True,
        services="legacy",
        fail_components=True,
    )

    assert account.returncode != 0
    assert "account status: BLOCKED" in account.stdout
    assert legacy.returncode != 0
    assert "Futu connectivity: BLOCKED" in legacy.stdout


@pytest.mark.parametrize(
    "services",
    ("gateway", "legacy", "account", "prediction", "gateway prediction"),
)
def test_scoped_smoke_accepts_selected_release_with_older_unselected_services(
    tmp_path: Path, services: str
) -> None:
    result, calls = _run_smoke(tmp_path, services=services)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("HEALTHY")
    for service, (url, port, _pid) in SERVICE_PORTS.items():
        if service in services.split():
            assert any(f"{url}/healthz" in call for call in calls)
            assert any(f"tiTCP:{port}" in call for call in calls)
        else:
            assert not any(f"{url}/healthz" in call for call in calls)
            assert not any(f"tiTCP:{port}" in call for call in calls)
    assert any(call.startswith("playwright ") for call in calls)
    assert not any("prediction-arbitrage/state" in call for call in calls)
    if "prediction" in services.split():
        assert any("prediction-arbitrage/lp/dashboard" in call for call in calls)


@pytest.mark.parametrize(
    ("status", "code", "lp_payload", "healthy"),
    (
        (
            "paused",
            "N_LEG_PAUSED",
            '{"state":"ready","orders":[],"positions":[],"recommendations":[]}',
            True,
        ),
        (
            "running",
            "N_LEG_RUNNING",
            '{"state":"ready","orders":[],"positions":[],"recommendations":[]}',
            False,
        ),
        (
            "paused",
            "N_LEG_PAUSED",
            '{"state":"unknown"}',
            False,
        ),
    ),
)
def test_scoped_smoke_preserves_prediction_pause_contract(
    tmp_path: Path,
    status: str,
    code: str,
    lp_payload: str,
    healthy: bool,
) -> None:
    result, calls = _run_smoke(
        tmp_path,
        services="prediction",
        n_leg_paused=1,
        prediction_n_leg=(status, code),
        lp_payload=lp_payload,
    )

    if healthy:
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.rstrip().endswith("HEALTHY")
        assert any(call.endswith("/api/prediction-arbitrage/lp/dashboard") for call in calls)
        assert not any(call.endswith("/api/prediction-arbitrage/state") for call in calls)
        assert any(call == "python browser" for call in calls)
    else:
        assert result.returncode != 0
        assert result.stdout.rstrip().endswith("ROLLBACK")


@pytest.mark.parametrize("log_content, healthy", [
    ("plain clean startup\n", True),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null}\n', True),
    ('2026-10-01 12:00:00 INFO prediction: {"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null}\n', True),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":"relations:TransportError"}\n', False),
    (r'{"level":"INFO","status":"healthy","degraded_reasons":[],"last_\u0065rror":"transport failed"}', False),
    (r'{"level":"INFO","status":"healthy","degraded_reasons":[],"message":"\u0045rror"}', False),
    (r'{"level":"INFO","status":"healthy","degraded_reasons":[],"last_\u0065rror":null}', True),
    ('ERROR prefix {"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null}\n', False),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null,"other_error":null}\n', False),
    ('{"level":"INFO","status":"healthy","degraded_reasons":["unknown"],"last_error":null}\n', False),
    ('{"level":"WARNING","status":"healthy","degraded_reasons":[],"last_error":null}\n', False),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null', False),
    ('{"broken-json', False),
    ('{broken-json', True),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null,"metric":NaN}\n', False),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null,"metric":Infinity}\n', False),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null,"metric":-Infinity}\n', False),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null,"last_error":null}\n', False),
    ('Traceback (most recent call last):\n', False),
    ('FATAL worker stopped\n', False),
    ('Unhandled exception\n', False),
    (b'\xff\xfe\n', False),
    ('ERROR outside retained window\n' + 'clean\n' * 200, False),
    (b'\xff\n' + b'clean\n' * 200, False),
])
def test_smoke_log_checker_only_exempts_proven_healthy_null(tmp_path, log_content, healthy):
    result, calls = _run_smoke(tmp_path, services="prediction", log_content=log_content)
    assert (result.returncode == 0) is healthy, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("HEALTHY" if healthy else "ROLLBACK")
    assert any(call.startswith("playwright ") for call in calls) is healthy
    assert "relations:TransportError" not in result.stdout + result.stderr


@pytest.mark.parametrize("details, healthy", [
    ("{'facts_read': 0.003}", True),
    ("{'facts_read': 0.003, 'error': 'unavailable'}", False),
    ("{'facts_read': 'Exception'}", False),
])
def test_smoke_timing_dict_text_keeps_full_error_check(tmp_path, details, healthy):
    result, calls = _run_smoke(
        tmp_path, services="prediction",
        log_content=f"2026-10-01 12:00:00 WARNING prediction: lp_facts_timing outcome=failed stages={details} thread=123\n",
    )
    assert (result.returncode == 0) is healthy, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("HEALTHY" if healthy else "ROLLBACK")
    assert any(call.startswith("playwright ") for call in calls) is healthy


@pytest.mark.parametrize("failure", ["log_missing", "log_read", "rg_missing"])
def test_smoke_log_checker_read_and_tool_failures_block(tmp_path, failure):
    result, calls = _run_smoke(tmp_path, services="prediction", failure=failure)
    assert result.returncode != 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("ROLLBACK")
    assert not any(call.startswith("playwright ") for call in calls)


def test_gateway_scope_ignores_unselected_prediction_pause(tmp_path: Path) -> None:
    result, calls = _run_smoke(
        tmp_path,
        services="gateway",
        unselected_prediction_paused=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("HEALTHY")
    assert any(call.endswith("http://127.0.0.1:8766/healthz") for call in calls)
    assert not any("8769/healthz" in call for call in calls)
    assert not any("-iTCP:8769" in call for call in calls)
    assert not any("prediction-arbitrage/state" in call for call in calls)
    assert not any("prediction-arbitrage/lp/dashboard" in call for call in calls)
    assert any(call == "python browser" for call in calls)


@pytest.mark.parametrize(
    ("services", "failure"),
    (
        ("gateway", "gateway_sha"),
        ("gateway", "gateway_root"),
        ("gateway", "gateway_listener"),
        ("gateway", "gateway_log"),
        ("account", "account_worker"),
        ("gateway", "browser"),
    ),
)
def test_scoped_smoke_preserves_selected_service_identity_guards(
    tmp_path: Path, services: str, failure: str
) -> None:
    result, _ = _run_smoke(tmp_path, services=services, failure=failure)

    assert result.returncode != 0
    assert result.stdout.rstrip().endswith("ROLLBACK")


def test_default_smoke_still_rejects_mixed_identities(tmp_path: Path) -> None:
    result, _ = _run_smoke(tmp_path, services=None, failure="default_mixed")

    assert result.returncode != 0
    assert result.stdout.rstrip().endswith("ROLLBACK")


@pytest.mark.parametrize("target", ("host-readiness", "production-smoke"))
@pytest.mark.parametrize("services", ("", "typo"))
def test_release_scopes_reject_empty_or_unknown_services_before_probes(
    tmp_path: Path, target: str, services: str
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    for command in ("curl", "df", "lsof", "node", "python", "rg"):
        _write_executable(
            fake_bin / command,
            '#!/bin/sh\necho "$0 $*" >> "$FAKE_CALLS"\nexit 99\n',
        )

    environment = dict(os.environ)
    environment.update(
        PATH=f"{fake_bin}{os.pathsep}{environment['PATH']}",
        FAKE_CALLS=str(calls),
    )
    result = subprocess.run(
        ["make", target, f"RELEASE_SERVICES={services}"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode != 0
    marker = "READY" if target == "host-readiness" else "HEALTHY"
    assert marker not in result.stdout
    assert result.stdout.rstrip().endswith(
        "BLOCKED" if target == "host-readiness" else "ROLLBACK"
    )
    assert "RELEASE_SERVICES" in result.stdout + result.stderr
    assert not calls.exists()


@pytest.mark.parametrize("state", ["absent", "running", "failed"])
def test_normal_readiness_requires_actual_managed_prediction_proof(tmp_path, state):
    from tests.test_prediction_release_launchd import ReleaseHarness
    from tests.test_lp410_launchd_recovery import failed_service
    producer_root=tmp_path/"producer"
    producer_root.mkdir()
    harness=failed_service(producer_root) if state=="failed" else ReleaseHarness(producer_root)
    if state=="running":
        installed=harness.install(check=True)
        assert installed.returncode==0,installed.stdout+installed.stderr
    if state=="absent":
        assert not harness.state['loaded'] and harness.state['pid']==0
        assert not harness.state['listener'] and harness.state['owner_available']
        assert not harness.plist.exists() and not harness.runtime_root.exists()
    else:
        assert harness.state['loaded']
        assert harness.state['listener'] is (state=="running")

    def protected():
        return (harness.runtime_root.exists(),
            {str(path):path.read_bytes() for base in (harness.runtime_root,harness.agents)
             for path in base.rglob('*') if path.is_file()},harness.config.read_bytes())
    before=protected()
    harness.calls.clear()
    producer=harness.install(preflight=True,owner_probe=state=="running")
    # Keep the exact public producer output/exit, rather than fabricating its
    # JSON as a consumer expectation. All OS boundaries remain disposable.
    (tmp_path/'producer-result.json').write_text(json.dumps(dict(command=producer.args,
        exit=producer.returncode,stdout=producer.stdout,stderr=producer.stderr),indent=2))
    assert producer.returncode==0,producer.stdout+producer.stderr
    payload=json.loads(producer.stdout)
    assert payload['schema_version']=='open_trader.prediction_service.preflight.v1'
    assert payload['status']==('RECOVERABLE' if state=='failed' else 'READY')
    assert protected()==before
    assert not any(' bootout ' in f' {call} ' or ' bootstrap ' in f' {call} '
                   for call in harness.calls.all())
    consumer_root=tmp_path/'normal-host'
    consumer=_run_host_readiness(consumer_root,browser_available=True,services='prediction',
        prediction_listener=False,manager_payload=producer.stdout,manager_exit=producer.returncode)
    (tmp_path/'consumer-result.json').write_text(json.dumps(dict(command=consumer.args,
        exit=consumer.returncode,stdout=consumer.stdout,stderr=consumer.stderr),indent=2))
    assert (consumer_root/'manager-response.json').read_text()==producer.stdout
    if state=='absent':
        assert consumer.returncode!=0,consumer.stdout+consumer.stderr
        assert consumer.stdout.rstrip().endswith('BLOCKED')
        assert 'prediction managed recovery: BLOCKED' in consumer.stdout
        first=_run_host_readiness(tmp_path/'first-host',browser_available=True,
            services='prediction',prediction_listener=False,first_deploy=True,
            manager_payload=producer.stdout,manager_exit=producer.returncode)
        assert first.returncode==0,first.stdout+first.stderr
        assert 'fresh host ownership: PASS' in first.stdout
        assert 'loopback listeners absent: PASS' in first.stdout
        assert first.stdout.rstrip().endswith('READY')
    else:
        assert consumer.returncode==0,consumer.stdout+consumer.stderr
        assert consumer.stdout.rstrip().endswith('READY')
        assert 'prediction managed recovery: PASS' in consumer.stdout
    assert payload['managed_identity_verified'] is (state!='absent')
    calls=(consumer_root/'calls').read_text().splitlines()
    assert not any(call.startswith('lsof ') and '8769' in call for call in calls)
    assert not any('--restart' in call or '--prepare-target' in call for call in calls)
    assert protected()==before


@pytest.mark.parametrize("case", ["normal-null", "normal-empty", "ws-exception", "other-error"])
def test_smoke_accepts_normal_ws_empty_exception_and_blocks_real_errors(tmp_path, case):
    rows = [
        {"at": "2026-10-29T13:44:10+00:00", "event": "connected", "reason": None,
         "code": None, "exception_class": "" if case == "normal-empty" else None},
        {"at": "2026-10-29T13:44:11+00:00", "event": "disconnected", "reason": "closed",
         "code": None, "exception_class": "" if case == "normal-empty" else None},
    ]
    if case == "ws-exception": rows[-1]["exception_class"] = "RemoteProtocolError private-payload-sentinel"
    log_content = "INFO prediction_runtime_state ready\n" + "\n".join(
        "2026-10-29T13:44:10+0000 INFO open_trader.polymarket_lp_orderbook "
        "prediction_lp_orderbook_connection " + json.dumps(row) for row in rows
    ) + "\n"
    if case == "other-error": log_content = "ERROR private-payload-sentinel\n" + "INFO clean\n" * 201 + log_content
    result, calls = _run_smoke(tmp_path, services="prediction", log_content=log_content)
    healthy = case in ("normal-null", "normal-empty")
    assert (result.returncode == 0) is healthy, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("HEALTHY" if healthy else "ROLLBACK")
    assert any(call.startswith("playwright ") for call in calls) is healthy
    # The Python browser precheck precedes logs; Playwright is the post-log gate.
    assert any(call == "python browser" for call in calls)
    assert "private-payload-sentinel" not in result.stdout + result.stderr
