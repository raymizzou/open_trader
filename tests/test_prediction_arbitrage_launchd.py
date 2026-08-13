from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import open_trader.cli as cli


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_dashboard_launchd.sh"
HEALTH_INSTALLER = ROOT / "scripts" / "install_prediction_arbitrage_health_launchd.sh"
UNINSTALLER = ROOT / "scripts" / "uninstall_dashboard_launchd.sh"
TEMPLATE = ROOT / "ops" / "launchd" / "com.open-trader.dashboard.plist.template"


def test_dashboard_launchd_template_has_the_single_loopback_job() -> None:
    payload = plistlib.loads(TEMPLATE.read_bytes())
    assert payload["Label"] == "com.open-trader.dashboard"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["WorkingDirectory"] == "OPEN_TRADER_REPO"
    assert payload["EnvironmentVariables"]["PYTHONPATH"] == "OPEN_TRADER_REPO/src"
    assert payload["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-s"]
    assert payload["ProgramArguments"][2:6] == [
        "OPEN_TRADER_PYTHON", "-m", "open_trader", "dashboard"
    ]
    args = payload["ProgramArguments"]
    assert ["--host", "127.0.0.1"] == args[args.index("--host") : args.index("--host") + 2]
    assert ["--port", "8766"] == args[args.index("--port") : args.index("--port") + 2]
    assert "OPEN_TRADER_REPO/logs/dashboard/launchd.out.log" == payload["StandardOutPath"]
    assert "OPEN_TRADER_REPO/logs/dashboard/launchd.err.log" == payload["StandardErrorPath"]
    rendered = TEMPLATE.read_text(encoding="utf-8").lower()
    assert "private_key" not in rendered
    assert "api_secret" not in rendered


def test_dashboard_launchd_dry_run_is_valid_and_has_no_side_effect(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    result = subprocess.run(
        [
            str(INSTALLER),
            "--mode",
            "single",
            "--dry-run",
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(runtime_root),
            "--launch-agents-dir",
            str(agents),
            "--python",
            sys.executable,
        ],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
    )
    payload = plistlib.loads(result.stdout.encode("utf-8"))
    assert payload["Label"] == "com.open-trader.dashboard"
    assert payload["EnvironmentVariables"]["PATH"] == (
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    args = payload["ProgramArguments"]
    assert payload["WorkingDirectory"] == str(ROOT)
    assert args[args.index("--portfolio") + 1] == str(
        runtime_root / "data/latest/portfolio.csv"
    )
    assert args[args.index("--data-dir") + 1] == str(runtime_root / "data")
    assert args[args.index("--reports-dir") + 1] == str(runtime_root / "reports")
    assert args[args.index("--config") + 1] == str(
        runtime_root / "config/daily_premarket.env"
    )
    assert "--prediction-config" not in args
    assert "--prediction-owner" not in args
    assert not list(agents.iterdir())
    assert "127.0.0.1" in result.stdout
    assert "8766" in result.stdout


def test_uninstaller_targets_only_dashboard_stack_labels() -> None:
    source = UNINSTALLER.read_text(encoding="utf-8")
    assert "com.open-trader.dashboard" in source
    assert "com.open-trader.frontend-gateway" in source
    assert "com.open-trader.legacy-dashboard" in source
    assert "rm -rf" not in source
    assert "com.open-trader.premarket" not in source


def test_dashboard_installer_waits_across_seconds_boundary_before_bootstrap(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "ops/launchd").mkdir(parents=True)
    (repo / "config").mkdir()
    shutil.copy2(INSTALLER, repo / "scripts/install_dashboard_launchd.sh")
    shutil.copy2(
        TEMPLATE,
        repo / "ops/launchd/com.open-trader.dashboard.plist.template",
    )
    log_dir = repo / "logs/dashboard"
    log_dir.mkdir(parents=True)
    stdout_log = log_dir / "launchd.out.log"
    stderr_log = log_dir / "launchd.err.log"
    stdout_log.write_text("old stdout\n", encoding="utf-8")
    stderr_log.write_text("old stderr\n", encoding="utf-8")
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "bootstrap-count"
    pending_removal = tmp_path / "pending-removal"
    sleep_log = tmp_path / "sleep-calls"
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(
        """force_seconds_boundary() {
  if [[ "${FUNCNAME[1]:-}" == "wait_agent_absent" ]] &&
    [[ "${label:-}" == "com.open-trader.dashboard" ]] &&
    [[ "$BASH_COMMAND" == output=* && "${FAKE_BOUNDARY_CROSSED:-0}" -eq 0 ]]; then
    FAKE_BOUNDARY_CROSSED=1
    SECONDS=$((SECONDS + WAIT_SECONDS))
  fi
}
set -T
trap force_seconds_boundary DEBUG
""",
        encoding="utf-8",
    )
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
label="${2##*/}"
if [ "$1" = "bootout" ]; then
  if [ "$label" = "com.open-trader.dashboard" ]; then
    echo 0 > "$FAKE_PENDING_REMOVAL"
  fi
  exit 0
fi
if [ "$1" = "print" ]; then
  if [ "$label" = "com.open-trader.dashboard" ] && [ -f "$FAKE_PENDING_REMOVAL" ]; then
    count="$(cat "$FAKE_PENDING_REMOVAL")"
    count=$((count + 1))
    echo "$count" > "$FAKE_PENDING_REMOVAL"
    if [ "$count" -lt 2 ]; then
      echo 'pid = 4242'
      exit 0
    fi
    rm -f "$FAKE_PENDING_REMOVAL"
  fi
  echo 'Could not find service' >&2
  exit 113
fi
if [ "$1" = "bootstrap" ]; then
  count="$(cat "$FAKE_BOOTSTRAP_STATE" 2>/dev/null || echo 0)"
  count=$((count + 1))
  echo "$count" > "$FAKE_BOOTSTRAP_STATE"
  if [ -f "$FAKE_PENDING_REMOVAL" ]; then
    echo 'Bootstrap failed: 5: Input/output error' >&2
    rm -f "$FAKE_PENDING_REMOVAL"
    exit 5
  fi
fi
exit 0
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_SLEEP_LOG\"\n",
        encoding="utf-8",
    )
    sleep.chmod(0o755)
    lsof = bin_dir / "lsof"
    lsof.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    lsof.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *healthz*) printf '%s\\n' '{\"module\":\"legacy_dashboard\"}' ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    result = subprocess.run(
        [
            str(INSTALLER),
            "--mode",
            "single",
            "--repo-root",
            str(repo),
            "--runtime-root",
            str(runtime_root),
            "--launch-agents-dir",
            str(agents),
            "--python",
            sys.executable,
            "--wait-seconds",
            "1",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "FAKE_BOOTSTRAP_STATE": str(state),
            "FAKE_SLEEP_LOG": str(sleep_log),
            "FAKE_PENDING_REMOVAL": str(pending_removal),
            "BASH_ENV": str(bash_env),
            "HOME": str(tmp_path),
            "LAUNCHCTL_BIN": str(launchctl),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert state.read_text(encoding="utf-8").strip() == "1"
    assert sleep_log.read_text(encoding="utf-8").splitlines() == ["1"]
    assert result.stderr == ""
    assert stdout_log.read_text(encoding="utf-8") == ""
    assert stderr_log.read_text(encoding="utf-8") == ""


def test_prediction_status_command_is_registered() -> None:
    source = (ROOT / "src" / "open_trader" / "cli.py").read_text(encoding="utf-8")
    assert 'add_parser("status"' in source
    assert "event_count" in source
    assert "masked_wallet" in source


def test_obsolete_shadow_validator_command_is_not_exposed() -> None:
    source = (ROOT / "src" / "open_trader" / "__main__.py").read_text(encoding="utf-8")
    assert "prediction-shadow-validate" not in source
    assert not (ROOT / "src" / "open_trader" / "prediction_shadow_validation.py").exists()


def _status_state() -> dict[str, object]:
    return {
        "status": "healthy",
        "stale": False,
        "breaker": {"open": False},
        "readiness": {"ready": True},
        "opportunities": [],
    }


def _prediction_service_health() -> dict[str, object]:
    return {
        "schema_version": "open_trader.prediction_service.health.v1",
        "module": "prediction_service",
        "status": "running",
        "mode": "production",
        "production_owner": True,
        "mutations": "enabled",
        "source_state": "clean",
        "pid": 4242,
        "cwd": "/srv/open_trader",
        "git_sha": "accepted-sha",
    }


def test_prediction_status_8769_uses_exact_service_health_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fetch(request: object, timeout: float) -> _JsonResponse:
        url = getattr(request, "full_url", request)
        if url.endswith("/healthz"):
            return _JsonResponse(_prediction_service_health())
        return _JsonResponse(_status_state())

    monkeypatch.setattr(cli, "urlopen", fetch)
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: pytest.fail("process scan is obsolete"))

    assert cli.main(["prediction-arb", "status", "--url", "http://127.0.0.1:8769"]) == 0
    output = capsys.readouterr().out
    assert "health: healthy" in output
    assert "pid: 4242" in output
    assert "result: PASS" in output


@pytest.mark.parametrize(
    "override",
    [
        {"mode": "shadow", "production_owner": False, "mutations": "prohibited"},
        {"production_owner": False},
        {"mutations": "prohibited"},
        {"schema_version": "malformed"},
        {"source_state": "dirty"},
        {"cwd": ""},
        {"git_sha": ""},
        {"pid": "4242"},
    ],
)
def test_prediction_status_8769_fails_closed_for_non_service_health(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    override: dict[str, object],
) -> None:
    health = _prediction_service_health()
    health.update(override)

    def fetch(request: object, timeout: float) -> _JsonResponse:
        url = getattr(request, "full_url", request)
        return _JsonResponse(health if url.endswith("/healthz") else _status_state())

    monkeypatch.setattr(cli, "urlopen", fetch)
    assert cli.main(["prediction-arb", "status", "--url", "http://127.0.0.1:8769"]) == 2
    output = capsys.readouterr().out
    assert "result: BLOCKED" in output


def test_prediction_status_8766_validates_gateway_health_without_fabricating_pid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    health = {
        "schema_version": "open_trader.frontend_gateway.health.v1",
        "module": "frontend_gateway",
        "upstream_status": "ok",
        "prediction_route_mode": "service",
        "prediction_upstream_status": "ok",
    }

    def fetch(request: object, timeout: float) -> _JsonResponse:
        url = getattr(request, "full_url", request)
        return _JsonResponse(health if url.endswith("/healthz") else _status_state())

    monkeypatch.setattr(cli, "urlopen", fetch)
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: pytest.fail("process scan is obsolete"))

    assert cli.main(["prediction-arb", "status", "--url", "http://127.0.0.1:8766"]) == 0
    output = capsys.readouterr().out
    assert "pid: unknown" in output
    assert "result: PASS" in output


def test_prediction_status_rejects_isolated_shadow_port_before_fetch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "urlopen", lambda *args, **kwargs: pytest.fail("isolated port must not be queried"))
    assert cli.main(["prediction-arb", "status", "--url", "http://127.0.0.1:18769"]) == 2
    output = capsys.readouterr().out
    assert "unsupported status URL" in output
    assert "result: BLOCKED" in output


def test_prediction_status_fails_closed_for_malformed_url(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["prediction-arb", "status", "--url", "http://127.0.0.1:bad"]) == 2
    output = capsys.readouterr().out
    assert "result: BLOCKED" in output


class _JsonResponse:
    status = 200

    def __init__(
        self, payload: dict[str, object], *, headers: dict[str, str] | None = None
    ) -> None:
        self._payload = payload
        self.headers = headers or {}

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        import json

        return json.dumps(self._payload).encode("utf-8")


def test_cross_auto_mode_and_arm_commands_are_removed() -> None:
    for command in ("mode auto_submit", "arm"):
        with pytest.raises(SystemExit) as error:
            cli.build_parser().parse_args(
                ["prediction-arb", "cross-auto", *command.split()]
            )
        assert error.value.code == 2


def test_cross_auto_status_reads_service_state_over_http(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = {
        "cross_auto": {
            "configured_mode": "manual_confirm",
            "effective_mode": "manual_confirm",
            "armed": False,
            "pause_reason": "not_armed",
            "daily_principal": {"current": "2", "limit": "100"},
            "latest_attempt": None,
        },
        "csrf_token": "ignored-by-status",
    }
    calls: list[object] = []

    def fetch(request: object, timeout: float) -> _JsonResponse:
        calls.append(request)
        assert timeout == 5
        return _JsonResponse(
            state,
            headers={"Set-Cookie": "ot_prediction_session=session; Path=/"},
        )

    monkeypatch.setattr(cli, "urlopen", fetch)
    assert cli.main(
        ["prediction-arb", "cross-auto", "status"]
    ) == 0

    output = capsys.readouterr().out
    assert "configured_mode: manual_confirm" in output
    assert "effective_mode: manual_confirm" in output
    assert "armed: False" in output
    assert "result: PASS" in output
    assert len(calls) == 1
    request = calls[0]
    assert getattr(request, "full_url") == (
        "http://127.0.0.1:8769/api/prediction-arbitrage/state"
    )


def test_cross_auto_status_does_not_accept_data_dir() -> None:
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(
            ["prediction-arb", "cross-auto", "status", "--data-dir", "data"]
        )
    assert error.value.code == 2


@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        ({"status": "healthy"}, "cross_auto_state_unavailable"),
        ({"cross_auto": []}, "cross_auto_state_schema_invalid"),
        ({"cross_auto": {"configured_mode": "observe_only"}}, "cross_auto_state_schema_invalid"),
    ),
)
def test_cross_auto_status_fails_closed_for_missing_or_malformed_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
    reason: str,
) -> None:
    monkeypatch.setattr(cli, "urlopen", lambda *args, **kwargs: _JsonResponse(payload))

    assert cli.main(["prediction-arb", "cross-auto", "status"]) == 2
    output = capsys.readouterr().out
    assert f"reason: {reason}" in output
    assert "result: BLOCKED" in output


@pytest.mark.parametrize(
    "field,value",
    (
        ("configured_mode", "unexpected_mode"),
        ("effective_mode", "unexpected_mode"),
        ("daily_principal", {"current": [], "limit": "100"}),
        ("daily_principal", {"current": "0", "limit": {}}),
        ("daily_principal", {"current": "not-money", "limit": "100"}),
        ("daily_principal", {"current": "-1", "limit": "100"}),
        ("daily_principal", {"current": float("nan"), "limit": "100"}),
        ("daily_principal", {"current": float("inf"), "limit": "100"}),
        ("daily_principal", {"current": True, "limit": "100"}),
        ("daily_principal", {"current": "0", "limit": "0"}),
        ("daily_principal", {"current": "0", "limit": "-1"}),
        ("daily_principal", {"current": "0", "limit": float("inf")}),
        ("daily_principal", {"current": "0", "limit": False}),
        (
            "latest_attempt",
            {"decision": 1, "reason": "cross_auto_paused", "reason_code": "cross_auto_paused"},
        ),
        (
            "latest_attempt",
            {"decision": "rejected", "reason": "", "reason_code": "cross_auto_paused"},
        ),
        (
            "latest_attempt",
            {"decision": "rejected", "reason": "cross_auto_paused", "reason_code": ""},
        ),
        ("latest_attempt", {"decision": "rejected", "reason": "cross_auto_paused"}),
    ),
)
def test_cross_auto_status_fails_closed_for_semantically_invalid_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: object,
) -> None:
    cross_auto: dict[str, object] = {
        "configured_mode": "observe_only",
        "effective_mode": "observe_only",
        "armed": False,
        "pause_reason": "not_armed",
        "daily_principal": {"current": "0", "limit": "100"},
        "latest_attempt": None,
    }
    cross_auto[field] = value
    monkeypatch.setattr(
        cli,
        "urlopen",
        lambda *args, **kwargs: _JsonResponse({"cross_auto": cross_auto}),
    )

    assert cli.main(["prediction-arb", "cross-auto", "status"]) == 2
    output = capsys.readouterr().out
    assert "reason: cross_auto_state_schema_invalid" in output
    assert "result: BLOCKED" in output
    assert "result: PASS" not in output


def test_prediction_health_installer_defaults_to_service_port(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            str(HEALTH_INSTALLER),
            "--dry-run",
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        env={**os.environ, "OPEN_TRADER_HEALTH_URL": ""},
        check=True,
        capture_output=True,
        text=True,
    )

    assert "http://127.0.0.1:8769" in result.stdout
    assert "http://127.0.0.1:8766" not in result.stdout
    assert "--data-dir" not in result.stdout
    assert "--repo" not in result.stdout


def test_prediction_health_cli_has_no_dead_storage_flags() -> None:
    for flag in ("--data-dir", "--repo"):
        with pytest.raises(SystemExit) as error:
            cli.build_parser().parse_args(
                ["prediction-arb", "health-check", flag, "unused"]
            )
        assert error.value.code == 2
