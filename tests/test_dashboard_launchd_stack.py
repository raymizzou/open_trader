from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_dashboard_launchd.sh"
GATEWAY_LABEL = "com.open-trader.frontend-gateway"
LEGACY_LABEL = "com.open-trader.legacy-dashboard"
GATEWAY_TEMPLATE = ROOT / f"ops/launchd/{GATEWAY_LABEL}.plist.template"
LEGACY_TEMPLATE = ROOT / f"ops/launchd/{LEGACY_LABEL}.plist.template"


def _dry_run_sections(stdout: str) -> dict[str, dict[str, object]]:
    sections: dict[str, dict[str, object]] = {}
    for section in stdout.split("===== ")[1:]:
        label, xml = section.split(" =====\n", 1)
        sections[label] = plistlib.loads(xml.encode("utf-8"))
    return sections


def test_stack_templates_define_separate_loopback_jobs() -> None:
    gateway = plistlib.loads(GATEWAY_TEMPLATE.read_bytes())
    legacy = plistlib.loads(LEGACY_TEMPLATE.read_bytes())
    gateway_args = gateway["ProgramArguments"]
    legacy_args = legacy["ProgramArguments"]

    assert gateway["Label"] == GATEWAY_LABEL
    assert legacy["Label"] == LEGACY_LABEL
    assert gateway_args[gateway_args.index("-m") : gateway_args.index("-m") + 3] == [
        "-m",
        "open_trader",
        "frontend-gateway",
    ]
    assert gateway_args[gateway_args.index("--port") + 1] == "8766"
    assert gateway_args[gateway_args.index("--upstream-port") + 1] == "8767"
    assert legacy_args[legacy_args.index("-m") : legacy_args.index("-m") + 3] == [
        "-m",
        "open_trader",
        "dashboard",
    ]
    assert legacy_args[legacy_args.index("--port") + 1] == "8767"
    assert legacy_args[legacy_args.index("--public-url") + 1] == (
        "http://127.0.0.1:8766/"
    )
    assert gateway["StandardOutPath"] == (
        "OPEN_TRADER_REPO/logs/frontend_gateway/launchd.out.log"
    )
    assert legacy["StandardOutPath"] == (
        "OPEN_TRADER_REPO/logs/legacy_dashboard/launchd.out.log"
    )


def test_stack_dry_run_prints_two_valid_plists_without_side_effects(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    forbidden = tmp_path / "forbidden-tool"
    forbidden.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    forbidden.chmod(0o755)
    result = subprocess.run(
        [
            str(INSTALLER),
            "--dry-run",
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(runtime),
            "--launch-agents-dir",
            str(agents),
            "--python",
            str(ROOT / ".venv/bin/python"),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "LAUNCHCTL_BIN": str(forbidden),
            "LSOF_BIN": str(forbidden),
            "CURL_BIN": str(forbidden),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    sections = _dry_run_sections(result.stdout)
    assert set(sections) == {GATEWAY_LABEL, LEGACY_LABEL}
    assert sections[GATEWAY_LABEL]["WorkingDirectory"] == str(ROOT)
    assert str(runtime / "data") in sections[LEGACY_LABEL]["ProgramArguments"]
    assert not list(agents.iterdir())
