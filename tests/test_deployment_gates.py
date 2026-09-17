from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

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
    prediction_n_leg: tuple[str, str] = ("running", "N_LEG_RUNNING"),
    unselected_prediction_paused: bool = False,
    lp_payload: str = '{"state":"ready","orders":[],"positions":[],"recommendations":[]}',
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    selected_services = services or DEFAULT_RELEASE_SERVICES
    release, sha, runtime = _create_release_fixture(tmp_path, failure=failure)
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
        "#!/bin/sh\nexec grep -E \"$@\"\n",
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
    lsof_mode = (
        'case "$*" in *8766*) exit 0 ;; *) exit 1 ;; esac'
        if fail_components
        else "exit 0"
    )
    _write_executable(
        fake_bin / "lsof",
        f'#!/bin/sh\necho "lsof $*" >> "$FAKE_CALLS"\n{lsof_mode}\n',
    )
    _write_executable(
        fake_bin / "node",
        '#!/bin/sh\necho "node $*" >> "$FAKE_CALLS"\nexit %s\n'
        % ("0" if browser_available else "1"),
    )

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
    return subprocess.run(
        [
            "make",
            "host-readiness",
            f"RELEASE_SERVICES={services}",
            f"PYTHON_BIN={fake_python}",
            f"REPOSITORY_ROOT={runtime_root}",
            f"PLAYWRIGHT_NODE_PATH={runtime_root / 'node_modules'}",
            f"DAILY_CONFIG={daily_config}",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_host_readiness_does_not_require_old_prediction_state(tmp_path: Path) -> None:
    result = _run_host_readiness(tmp_path, browser_available=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("READY")


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
    if "prediction" in services.split():
        assert any("prediction-arbitrage/state" in call for call in calls)
    else:
        assert not any("prediction-arbitrage/state" in call for call in calls)


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
