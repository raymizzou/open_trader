from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


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
        ROOT / "config/prediction_arbitrage.json"
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
