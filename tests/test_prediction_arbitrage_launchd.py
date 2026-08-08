from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_dashboard_launchd.sh"
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
    assert args[args.index("--prediction-config") + 1] == str(
        runtime_root / "config/prediction_arbitrage.json"
    )
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


def test_dashboard_installer_waits_for_bootout_before_bootstrap(
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
    (repo / "config/prediction_arbitrage.json").write_text(
        "{}\n", encoding="utf-8"
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
            "FAKE_PENDING_REMOVAL": str(pending_removal),
            "HOME": str(tmp_path),
            "LAUNCHCTL_BIN": str(launchctl),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    assert state.read_text(encoding="utf-8").strip() == "1"
    assert result.stderr == ""
    assert stdout_log.read_text(encoding="utf-8") == ""
    assert stderr_log.read_text(encoding="utf-8") == ""


def test_prediction_status_command_is_registered() -> None:
    source = (ROOT / "src" / "open_trader" / "cli.py").read_text(encoding="utf-8")
    assert 'add_parser("status"' in source
    assert "event_count" in source
    assert "masked_wallet" in source


class _JsonResponse:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        import json

        return json.dumps(self._payload).encode("utf-8")


def _arm_ready_state() -> dict[str, object]:
    return {
        "status": "healthy",
        "cross_venue": {"status": "ready", "breaker": {"open": False}},
        "venues": [
            {"venue": "polymarket", "rest": "ready", "ws": "ready"},
            {"venue": "predict.fun", "rest": "ready", "ws": "ready"},
        ],
        "breaker": {"open": False},
        "current_execution": None,
        "cross_auto": {
            "configured_mode": "auto_submit",
            "notification_ready": True,
        },
    }


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        (("health_status", "bad"), "healthz_unavailable"),
        (("sha", "other-sha"), "git_sha_mismatch"),
        (("dirty", "dirty"), "source_dirty"),
        (("cross", "degraded"), "cross_venue_not_ready"),
        (("poly_rest", "stale"), "polymarket_rest_not_ready"),
        (("poly_ws", "stale"), "polymarket_ws_not_ready"),
        (("predict_rest", "stale"), "predict_fun_rest_not_ready"),
        (("predict_ws", "stale"), "predict_fun_ws_not_ready"),
        (("breaker", True), "breaker_open"),
        (("active", {"state": "running"}), "active_execution"),
        (("mode", "manual_confirm"), "configured_mode_not_auto_submit"),
        (("notification", False), "notification_config_unavailable"),
    ),
)
def test_cross_auto_arm_fails_closed_for_remote_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: tuple[str, object],
    reason: str,
) -> None:
    state = _arm_ready_state()
    health: dict[str, object] = {"git_sha": "accepted-sha", "source_state": "clean"}
    field, value = change
    if field == "health_status":
        health["status"] = value
    elif field == "sha":
        health["git_sha"] = value
    elif field == "dirty":
        health["source_state"] = value
    elif field == "cross":
        state["cross_venue"] = {"status": value, "breaker": {"open": False}}
    elif field == "poly_rest":
        state["venues"][0]["rest"] = value  # type: ignore[index]
    elif field == "poly_ws":
        state["venues"][0]["ws"] = value  # type: ignore[index]
    elif field == "predict_rest":
        state["venues"][1]["rest"] = value  # type: ignore[index]
    elif field == "predict_ws":
        state["venues"][1]["ws"] = value  # type: ignore[index]
    elif field == "breaker":
        state["breaker"] = {"open": value}
    elif field == "active":
        state["current_execution"] = value
    elif field == "mode":
        state["cross_auto"]["configured_mode"] = value  # type: ignore[index]
    elif field == "notification":
        state["cross_auto"]["notification_ready"] = value  # type: ignore[index]

    def fetch(url: str, timeout: float) -> _JsonResponse:
        assert timeout == 10
        if url.endswith("/healthz"):
            response = _JsonResponse(health)
            response.status = int(health.get("status", 200))
            return response
        return _JsonResponse(state)

    monkeypatch.setattr(cli, "urlopen", fetch)
    assert cli.main(
        [
            "prediction-arb", "cross-auto", "arm", "--data-dir", str(tmp_path),
            "--url", "http://127.0.0.1:8766", "--expected-sha", "accepted-sha",
        ]
    ) == 2
    assert f"reason: {reason}" in capsys.readouterr().out
    assert PredictionArbitrageStore(tmp_path).cross_auto_state()["armed"] is False


def test_cross_auto_arm_requires_complete_remote_readiness_and_status_is_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = _arm_ready_state()
    health = {"git_sha": "accepted-sha", "source_state": "clean"}
    monkeypatch.setattr(
        cli,
        "urlopen",
        lambda url, timeout: _JsonResponse(health if url.endswith("/healthz") else state),
    )
    args = [
        "prediction-arb", "cross-auto", "arm", "--data-dir", str(tmp_path),
        "--url", "http://127.0.0.1:8766", "--expected-sha", "accepted-sha",
    ]
    assert cli.main(args) == 0
    assert "result: PASS" in capsys.readouterr().out
    assert PredictionArbitrageStore(tmp_path).cross_auto_state()["armed"] is True
    assert cli.main(["prediction-arb", "cross-auto", "status", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "armed: True" in output
    assert "result: PASS" in output


def test_cross_auto_arm_never_contacts_a_non_loopback_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "urlopen", lambda *args, **kwargs: pytest.fail("must not fetch"))
    assert cli.main(
        [
            "prediction-arb", "cross-auto", "arm", "--data-dir", str(tmp_path),
            "--url", "http://example.com", "--expected-sha", "accepted-sha",
        ]
    ) == 2
    assert "reason: url_not_loopback" in capsys.readouterr().out
