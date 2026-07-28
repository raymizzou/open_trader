from __future__ import annotations

import os
import plistlib
import subprocess
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
            "--dry-run",
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(runtime_root),
            "--launch-agents-dir",
            str(agents),
            "--python",
            str(ROOT / ".venv" / "bin" / "python"),
        ],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
    )
    payload = plistlib.loads(result.stdout.encode("utf-8"))
    assert payload["Label"] == "com.open-trader.dashboard"
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


def test_uninstaller_only_targets_the_dashboard_label() -> None:
    source = UNINSTALLER.read_text(encoding="utf-8")
    assert "com.open-trader.dashboard" in source
    assert "rm -rf" not in source
    assert "com.open-trader.premarket" not in source


def test_dashboard_installer_retries_transient_launchctl_bootstrap(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "bootstrap-count"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
if [ "$1" = "bootstrap" ]; then
  count="$(cat "$FAKE_BOOTSTRAP_STATE" 2>/dev/null || echo 0)"
  count=$((count + 1))
  echo "$count" > "$FAKE_BOOTSTRAP_STATE"
  [ "$count" -gt 1 ] || exit 5
fi
exit 0
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    for name in ("curl", "lsof"):
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

    subprocess.run(
        [
            str(INSTALLER),
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(runtime_root),
            "--launch-agents-dir",
            str(agents),
            "--python",
            str(ROOT / ".venv" / "bin" / "python"),
            "--wait-seconds",
            "1",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "FAKE_BOOTSTRAP_STATE": str(state),
            "HOME": str(tmp_path),
            "LAUNCHCTL_BIN": str(launchctl),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    assert state.read_text(encoding="utf-8").strip() == "2"


def test_prediction_status_command_is_registered() -> None:
    source = (ROOT / "src" / "open_trader" / "cli.py").read_text(encoding="utf-8")
    assert 'add_parser("status"' in source
    assert "event_count" in source
    assert "masked_wallet" in source
