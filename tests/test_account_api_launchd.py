from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_account_api_launchd.sh"
UNINSTALLER = ROOT / "scripts" / "uninstall_account_api_launchd.sh"
TEMPLATE = ROOT / "ops" / "launchd" / "com.open-trader.account-api.plist.template"
LABEL = "com.open-trader.account-api"


def test_account_api_template_runs_mode_specific_command() -> None:
    payload = plistlib.loads(TEMPLATE.read_bytes())

    assert payload["Label"] == LABEL
    assert payload["WorkingDirectory"] == "OPEN_TRADER_REPO"
    assert payload["EnvironmentVariables"] == {"PYTHONPATH": "OPEN_TRADER_REPO/src"}
    assert payload["ProgramArguments"] == [
        "OPEN_TRADER_PYTHON", "-m", "open_trader", "account-api",
        "--data-dir", "OPEN_TRADER_DATA_DIR",
        "--mode", "OPEN_TRADER_ACCOUNT_API_MODE",
        "--config", "OPEN_TRADER_DAILY_CONFIG",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ProcessType"] == "Interactive"
    assert payload["ThrottleInterval"] == 5
    assert payload["StandardOutPath"] == "OPEN_TRADER_REPO/logs/account_api/launchd.out.log"
    assert payload["StandardErrorPath"] == "OPEN_TRADER_REPO/logs/account_api/launchd.err.log"


def test_account_api_installer_dry_run_defaults_to_shadow(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()

    result = subprocess.run(
        [
            str(INSTALLER), "--dry-run", "--repo-root", str(ROOT),
            "--runtime-root", str(runtime), "--python", sys.executable,
            "--launch-agents-dir", str(agents),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = plistlib.loads(result.stdout.encode())
    assert payload["WorkingDirectory"] == str(ROOT)
    assert payload["EnvironmentVariables"]["PYTHONPATH"] == str(ROOT / "src")
    assert str(runtime / "data") in payload["ProgramArguments"]
    assert ["--mode", "shadow"] == payload["ProgramArguments"][-4:-2]
    assert payload["ProgramArguments"][-2:] == [
        "--config", str(runtime / "config/daily_premarket.env")
    ]
    assert "frontend-gateway" not in result.stdout
    assert "account-sync-worker" not in result.stdout


def test_account_api_installer_dry_run_renders_explicit_production(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()

    result = subprocess.run(
        [
            str(INSTALLER), "--dry-run", "--mode", "production",
            "--repo-root", str(ROOT), "--runtime-root", str(runtime),
            "--python", sys.executable, "--launch-agents-dir", str(agents),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    arguments = plistlib.loads(result.stdout.encode())["ProgramArguments"]
    assert arguments[-4:-2] == ["--mode", "production"]


def test_account_api_installer_rejects_unknown_mode_before_launchd(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    launchctl = tmp_path / "launchctl"
    launchctl.write_text("#!/bin/sh\necho called >> \"$FAKE_CALLS\"\n", encoding="utf-8")
    launchctl.chmod(0o755)

    result = subprocess.run(
        [str(INSTALLER), "--mode", "invalid", "--launch-agents-dir", str(tmp_path / "LaunchAgents")],
        capture_output=True,
        text=True,
        env={**os.environ, "LAUNCHCTL_BIN": str(launchctl), "FAKE_CALLS": str(calls)},
    )

    assert result.returncode == 2
    assert not calls.exists()


@pytest.mark.parametrize(("health_mode", "expect_success"), [("production", True), ("shadow", False)])
def test_production_installer_accepts_only_matching_mode_health(
    tmp_path: Path, health_mode: str, expect_success: bool
) -> None:
    repo = _copy_repo(tmp_path)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    calls = tmp_path / "calls"
    state = tmp_path / "state"
    pending = tmp_path / "pending"
    launchctl = tmp_path / "launchctl"
    lsof = tmp_path / "lsof"
    curl = tmp_path / "curl"
    expected_sha = _git_sha(repo)
    launchctl.write_text(
        "#!/bin/sh\n"
        "echo \"$*\" >> \"$FAKE_CALLS\"\n"
        "case \"$1\" in\n"
        "bootout) : > \"$FAKE_PENDING\"; rm -f \"$FAKE_STATE\" ;;\n"
        "print)\n"
        "  if [ -f \"$FAKE_PENDING\" ]; then\n"
        "    count=$(cat \"$FAKE_PENDING\" 2>/dev/null || echo 0); count=$((count + 1)); echo \"$count\" > \"$FAKE_PENDING\"\n"
        "    if [ \"$count\" -lt 2 ]; then echo 'pid = 4242'; exit 0; fi\n"
        "    rm -f \"$FAKE_PENDING\"; echo 'Could not find service' >&2; exit 113\n"
        "  fi\n"
        "  if [ -f \"$FAKE_STATE\" ]; then echo 'pid = 4242'; exit 0; fi\n"
        "  echo 'Could not find service' >&2; exit 113 ;;\n"
        "bootstrap) : > \"$FAKE_STATE\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    lsof.write_text(
        "#!/bin/sh\n"
        "echo \"$*\" >> \"$FAKE_CALLS\"\n"
        "case \"$*\" in\n"
        "*'-d cwd -Fn'*) printf 'p4242\\nfcwd\\nn%s\\n' \"$FAKE_REPO\" ;;\n"
        "*'-iTCP:8768 -sTCP:LISTEN -Fn'*) printf 'p4242\\nn127.0.0.1:8768\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    curl.write_text(
        "#!/bin/sh\n"
        "echo \"$*\" >> \"$FAKE_CALLS\"\n"
        "printf '%s\\n' \"$FAKE_HEALTH\"\n",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    lsof.chmod(0o755)
    curl.chmod(0o755)
    health = json.dumps(
        {
            "schema_version": "open_trader.account_api.health.v1",
            "module": "account_api",
            "status": "ok",
            "mode": health_mode,
            "pid": 4242,
            "api_git_sha": expected_sha,
            "worker_git_sha": expected_sha,
            "release_match": True,
        }
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_account_api_launchd.sh"),
            "--repo-root", str(repo), "--runtime-root", str(tmp_path / "runtime"),
            "--python", sys.executable, "--launch-agents-dir", str(agents),
            "--wait-seconds", "2", "--mode", "production",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "LAUNCHCTL_BIN": str(launchctl),
            "LSOF_BIN": str(lsof),
            "CURL_BIN": str(curl),
            "FAKE_CALLS": str(calls),
            "FAKE_STATE": str(state),
            "FAKE_PENDING": str(pending),
            "FAKE_REPO": str(repo),
            "FAKE_HEALTH": health,
        },
    )

    if not expect_success:
        assert result.returncode == 1
        assert "Account API did not publish matching production health" in result.stderr
        return

    assert result.returncode == 0
    domain = f"gui/{os.getuid()}"
    assert result.stderr == ""
    assert f"installed launchd agent: {LABEL}" in result.stdout
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"bootout {domain}/{LABEL}",
        f"print {domain}/{LABEL}",
        f"print {domain}/{LABEL}",
        f"bootstrap {domain} {agents / f'{LABEL}.plist'}",
        f"print {domain}/{LABEL}",
        "-a -p 4242 -d cwd -Fn",
        "-nP -a -p 4242 -iTCP:8768 -sTCP:LISTEN -Fn",
        "-fsS http://127.0.0.1:8768/healthz",
    ]


def test_production_installer_timeout_boots_out_only_its_label(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    calls = tmp_path / "calls"
    state = tmp_path / "state"
    launchctl = tmp_path / "launchctl"
    lsof = tmp_path / "lsof"
    curl = tmp_path / "curl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "echo \"$*\" >> \"$FAKE_CALLS\"\n"
        "case \"$1\" in bootout) rm -f \"$FAKE_STATE\" ;; bootstrap) : > \"$FAKE_STATE\" ;; print)\n"
        "  [ -f \"$FAKE_STATE\" ] && { echo 'pid = 4242'; exit 0; }; echo 'Could not find service' >&2; exit 113 ;; esac\n",
        encoding="utf-8",
    )
    curl.write_text("#!/bin/sh\nprintf '%s\\n' '{}'\n", encoding="utf-8")
    lsof.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    lsof.chmod(0o755)
    curl.chmod(0o755)

    result = subprocess.run(
        [
            str(repo / "scripts/install_account_api_launchd.sh"),
            "--repo-root", str(repo), "--runtime-root", str(tmp_path / "runtime"),
            "--python", sys.executable, "--launch-agents-dir", str(agents),
            "--wait-seconds", "1", "--mode", "production",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "LAUNCHCTL_BIN": str(launchctl), "LSOF_BIN": str(lsof), "CURL_BIN": str(curl), "FAKE_CALLS": str(calls), "FAKE_STATE": str(state)},
    )

    assert result.returncode == 1
    assert "Account API did not publish matching production health" in result.stderr
    domain = f"gui/{os.getuid()}/{LABEL}"
    assert calls.read_text(encoding="utf-8").splitlines().count(f"bootout {domain}") == 2
    assert "frontend-gateway" not in calls.read_text(encoding="utf-8")
    assert "account-sync" not in calls.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad_field", ["cwd", "listener"])
def test_production_installer_rejects_matching_health_when_process_identity_is_not_exact(
    tmp_path: Path, bad_field: str
) -> None:
    repo = _copy_repo(tmp_path)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    calls = tmp_path / "calls"
    state = tmp_path / "state"
    launchctl = tmp_path / "launchctl"
    lsof = tmp_path / "lsof"
    curl = tmp_path / "curl"
    expected_sha = _git_sha(repo)
    launchctl.write_text(
        "#!/bin/sh\n"
        "echo \"$*\" >> \"$FAKE_CALLS\"\n"
        "case \"$1\" in\n"
        "bootout) rm -f \"$FAKE_STATE\" ;;\n"
        "bootstrap) : > \"$FAKE_STATE\" ;;\n"
        "print)\n"
        "  if [ -f \"$FAKE_STATE\" ]; then echo 'pid = 4242'; exit 0; fi\n"
        "  echo 'Could not find service' >&2; exit 113 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    lsof.write_text(
        "#!/bin/sh\n"
        "echo \"$*\" >> \"$FAKE_CALLS\"\n"
        "case \"$*\" in\n"
        "*'-d cwd -Fn'*)\n"
        "  if [ \"$FAKE_BAD_FIELD\" = cwd ]; then printf 'p4242\\nfcwd\\nn/tmp/wrong\\n'; else printf 'p4242\\nfcwd\\nn%s\\n' \"$FAKE_REPO\"; fi ;;\n"
        "*'-iTCP:8768 -sTCP:LISTEN -Fn'*)\n"
        "  if [ \"$FAKE_BAD_FIELD\" = listener ]; then printf 'p4242\\nn127.0.0.1:8768\\nn*:8768\\n'; else printf 'p4242\\nn127.0.0.1:8768\\n'; fi ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    curl.write_text(
        "#!/bin/sh\n"
        "echo \"$*\" >> \"$FAKE_CALLS\"\n"
        "printf '%s\\n' \"$FAKE_HEALTH\"\n",
        encoding="utf-8",
    )
    for path in (launchctl, lsof, curl):
        path.chmod(0o755)
    health = json.dumps(
        {
            "schema_version": "open_trader.account_api.health.v1",
            "module": "account_api",
            "status": "ok",
            "mode": "production",
            "pid": 4242,
            "api_git_sha": expected_sha,
            "worker_git_sha": expected_sha,
            "release_match": True,
        }
    )

    result = subprocess.run(
        [
            str(repo / "scripts/install_account_api_launchd.sh"),
            "--repo-root", str(repo), "--runtime-root", str(tmp_path / "runtime"),
            "--python", sys.executable, "--launch-agents-dir", str(agents),
            "--wait-seconds", "1", "--mode", "production",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "LAUNCHCTL_BIN": str(launchctl),
            "LSOF_BIN": str(lsof),
            "CURL_BIN": str(curl),
            "FAKE_CALLS": str(calls),
            "FAKE_STATE": str(state),
            "FAKE_REPO": str(repo),
            "FAKE_BAD_FIELD": bad_field,
            "FAKE_HEALTH": health,
        },
    )

    assert result.returncode == 1
    assert "Account API did not publish matching production health" in result.stderr
    recorded = calls.read_text(encoding="utf-8")
    assert "-a -p 4242 -d cwd -Fn" in recorded
    if bad_field == "listener":
        assert "-nP -a -p 4242 -iTCP:8768 -sTCP:LISTEN -Fn" in recorded
    assert "http://127.0.0.1:8768/healthz" not in recorded


def test_uninstaller_preserves_loaded_plist_then_is_idempotent(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    plist = agents / f"{LABEL}.plist"
    plist.write_text("keep", encoding="utf-8")
    launchctl = tmp_path / "launchctl"
    launchctl.write_text("#!/bin/sh\n[ \"$1\" = print ] && exit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)

    loaded = subprocess.run(
        [str(UNINSTALLER), "--repo-root", str(ROOT), "--launch-agents-dir", str(agents)],
        capture_output=True,
        text=True,
        env={**os.environ, "LAUNCHCTL_BIN": str(launchctl)},
    )
    assert loaded.returncode == 1
    assert plist.exists()
    assert LABEL in loaded.stderr

    launchctl.write_text("#!/bin/sh\necho 'Could not find service' >&2\nexit 113\n", encoding="utf-8")
    launchctl.chmod(0o755)
    removed = subprocess.run(
        [str(UNINSTALLER), "--repo-root", str(ROOT), "--launch-agents-dir", str(agents)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "LAUNCHCTL_BIN": str(launchctl)},
    )
    assert not plist.exists()
    assert "removed launchd agent" in removed.stdout
    repeated = subprocess.run(
        [str(UNINSTALLER), "--repo-root", str(ROOT), "--launch-agents-dir", str(agents)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "LAUNCHCTL_BIN": str(launchctl)},
    )
    assert "launchd agent not installed" in repeated.stdout


def _copy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "ops/launchd").mkdir(parents=True)
    shutil.copy2(INSTALLER, repo / "scripts" / INSTALLER.name)
    shutil.copy2(TEMPLATE, repo / "ops/launchd" / TEMPLATE.name)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "test"], check=True)
    return repo


def _git_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
