from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_INSTALLER = ROOT / "scripts" / "install_account_release.sh"
API_INSTALLER = ROOT / "scripts" / "install_account_api_launchd.sh"
SYNC_INSTALLER = ROOT / "scripts" / "install_account_sync_launchd.sh"
API_TEMPLATE = ROOT / "ops" / "launchd" / "com.open-trader.account-api.plist.template"
SYNC_TEMPLATE = ROOT / "ops" / "launchd" / "com.open-trader.account-sync-controller.plist.template"


def test_account_release_installer_dry_run_renders_both_jobs(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()

    result = subprocess.run(
        [
            str(RELEASE_INSTALLER), "--dry-run",
            "--repo-root", str(ROOT), "--runtime-root", str(runtime),
            "--python", sys.executable, "--launch-agents-dir", str(agents),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "com.open-trader.account-sync-controller" in result.stdout
    assert "com.open-trader.account-api" in result.stdout
    assert "account-sync-worker" in result.stdout
    assert "--mode" in result.stdout
    assert "production" in result.stdout
    assert "worker first, then API, then same-SHA cross-check" in result.stdout


def test_account_release_installer_refuses_dirty_release_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "ops/launchd").mkdir(parents=True)
    shutil.copy2(API_INSTALLER, repo / "scripts" / API_INSTALLER.name)
    shutil.copy2(SYNC_INSTALLER, repo / "scripts" / SYNC_INSTALLER.name)
    shutil.copy2(API_TEMPLATE, repo / "ops/launchd" / API_TEMPLATE.name)
    shutil.copy2(SYNC_TEMPLATE, repo / "ops/launchd" / SYNC_TEMPLATE.name)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.com",
         "commit", "-qm", "test"],
        check=True,
    )
    (repo / "dirty.txt").write_text("x", encoding="utf-8")

    result = subprocess.run(
        [
            str(RELEASE_INSTALLER),
            "--repo-root", str(repo), "--runtime-root", str(repo),
            "--python", sys.executable, "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "LAUNCHCTL_BIN": str(tmp_path / "missing-launchctl")},
    )

    assert result.returncode == 1
    assert "release root is dirty" in result.stderr
