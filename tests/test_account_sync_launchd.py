from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_account_sync_launchd.sh"
UNINSTALLER = ROOT / "scripts" / "uninstall_account_sync_launchd.sh"
TEMPLATE = ROOT / "ops" / "launchd" / "com.open-trader.account-sync-controller.plist.template"
LABEL = "com.open-trader.account-sync-controller"


def test_template_runs_only_the_account_sync_controller() -> None:
    payload = plistlib.loads(TEMPLATE.read_bytes())

    assert payload["Label"] == LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert ["-m", "open_trader", "account-sync-controller"] == payload[
        "ProgramArguments"
    ][
        payload["ProgramArguments"].index("-m") : payload["ProgramArguments"].index("-m") + 3
    ]
    assert payload["WorkingDirectory"] == "OPEN_TRADER_REPO"
    assert payload["StandardOutPath"] == "OPEN_TRADER_REPO/logs/account_sync/launchd.out.log"
    assert payload["StandardErrorPath"] == "OPEN_TRADER_REPO/logs/account_sync/launchd.err.log"


def test_dry_run_renders_runtime_paths_without_tiger_secrets(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()

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
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = plistlib.loads(result.stdout.encode("utf-8"))
    args = payload["ProgramArguments"]
    assert payload["WorkingDirectory"] == str(ROOT)
    assert str(runtime_root / "data") in args
    assert str(runtime_root / "reports") in args
    assert str(runtime_root / "config/daily_premarket.env") in args
    assert "logs/account_sync/launchd.out.log" in payload["StandardOutPath"]
    rendered = result.stdout.lower()
    assert "private_key" not in rendered
    assert "secret_key" not in rendered
    assert "token=" not in rendered


def test_installer_retries_bootstrap_kickstarts_and_waits_for_matching_status(
    tmp_path: Path,
) -> None:
    repo = _copy_repo(tmp_path)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    status_path = tmp_path / "runtime/data/account_sync/controller_status.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl = bin_dir / "launchctl"
    bootstrap_count = tmp_path / "bootstrap-count"
    expected_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = bootstrap ]; then\n"
        "  count=$(cat \"$FAKE_BOOTSTRAP_COUNT\" 2>/dev/null || echo 0)\n"
        "  count=$((count + 1)); echo \"$count\" > \"$FAKE_BOOTSTRAP_COUNT\"\n"
        "  [ \"$count\" -lt 3 ] && exit 1\n"
        "fi\n"
        "if [ \"$1\" = kickstart ]; then\n"
        "  mkdir -p \"$(dirname \"$FAKE_STATUS_PATH\")\"\n"
        "  printf '%s' \"$FAKE_STATUS_JSON\" > \"$FAKE_STATUS_PATH\"\n"
        "fi\n",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    status = {
        "schema_version": "open_trader.account_sync.controller.v1",
        "pid": 99,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "working_directory": str(repo),
        "git_sha": expected_sha,
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "phase": "idle",
        "account_loop": {"status": "ok"},
        "quote_loop": {"status": "ok"},
        "blocker": None,
    }
    env = {
        **os.environ,
        "LAUNCHCTL_BIN": str(launchctl),
        "FAKE_BOOTSTRAP_COUNT": str(bootstrap_count),
        "FAKE_STATUS_PATH": str(status_path),
        "FAKE_STATUS_JSON": json.dumps(status),
    }

    result = subprocess.run(
        [
            str(repo / "scripts/install_account_sync_launchd.sh"),
            "--repo-root",
            str(repo),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--python",
            sys.executable,
            "--launch-agents-dir",
            str(agents),
            "--wait-seconds",
            "2",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert bootstrap_count.read_text(encoding="utf-8").strip() == "3"
    assert f"installed launchd agent: {LABEL}" in result.stdout


def test_uninstaller_preserves_a_still_loaded_plist(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    plist = agents / f"{LABEL}.plist"
    plist.write_text("keep", encoding="utf-8")
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n[ \"$1\" = print ] && exit 0\nexit 0\n", encoding="utf-8"
    )
    launchctl.chmod(0o755)

    result = subprocess.run(
        [str(UNINSTALLER), "--repo-root", str(ROOT), "--launch-agents-dir", str(agents)],
        capture_output=True,
        text=True,
        env={**os.environ, "LAUNCHCTL_BIN": str(launchctl)},
    )

    assert result.returncode == 1
    assert plist.exists()
    assert LABEL in result.stderr


def _copy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "ops/launchd").mkdir(parents=True)
    shutil.copy2(INSTALLER, repo / "scripts/install_account_sync_launchd.sh")
    shutil.copy2(TEMPLATE, repo / "ops/launchd/com.open-trader.account-sync-controller.plist.template")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "test"],
        check=True,
    )
    return repo
