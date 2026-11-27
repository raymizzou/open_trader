import importlib.util
import json
import os
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

DAY = "2025-01-02"
PIN = "a" * 40
SPACE = "sp00000000"


@pytest.mark.parametrize("launcher_form", ["literal", "imported"])
def test_host_prepare_loads_the_installed_space_and_packages_handoff(
    tmp_path, monkeypatch, launcher_form,
):
    host = Path(__file__).resolve().parents[1] / "host"
    spec = importlib.util.spec_from_file_location(
        "prepare_candidate", host / "prepare.py"
    )
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    home = tmp_path.resolve() / "synthetic-home"
    config = home / ".config/trend-animals"
    release = config / "skill-releases" / PIN
    service = release / "services/trend_v3"
    service.mkdir(parents=True)
    shutil.copytree(host, service / "host")
    os.symlink(host.parent / "src", service / "src")
    existing = config / "run-daily.py"
    existing.write_text(
        f"SPACE_ID = {SPACE!r}\n" if launcher_form == "literal"
        else "from daily_release import PIN, SERVICE_ROOT, SPACE_ID\n"
    )
    if launcher_form == "imported":
        (config / "daily_release.py").write_text(f"SPACE_ID = {SPACE!r}\n")
    initial = {p.name: p.read_bytes() for p in config.glob("*.py")}
    output = tmp_path / "candidate"
    monkeypatch.setattr(Path, "home", classmethod(lambda _: home))
    monkeypatch.setattr(
        subprocess, "check_output",
        lambda command, **_: f"{PIN}\n" if "rev-parse" in command else "",
    )
    assert prepare.prepare(release, output) == PIN
    assert {p.name for p in output.iterdir()} == {
        "daily_host.py", "daily_sync.py", "daily_paper.py", "daily_handoff.py",
        "daily_release.py", "run-daily.py",
    }
    assert {p.name: p.read_bytes() for p in config.glob("*.py")} == initial
    assert f"SPACE_ID = {SPACE!r}" in (output / "daily_release.py").read_text()

    # Only the prepared files and unchanged synthetic host dependencies are visible.
    install_home = tmp_path.resolve() / "candidate-home"
    install = install_home / ".config/trend-animals"
    shutil.copytree(output, install)
    (install / "daily_lifecycle.py").write_text(
        "atomic_json = record_launch = cleanup_old_tabs = None\n"
    )
    (install / "daily_support.py").write_text(
        "class SyncDenied(ValueError): pass\nretry_sync = sync_allowed = None\n"
    )
    environment = {"HOME": str(install_home), "PATH": os.defpath}
    for script in ("daily_host.py", "run-daily.py"):
        result = subprocess.run(
            [sys.executable, "-B", str(install / script), "--help"],
            capture_output=True, text=True, env=environment, timeout=10,
        )
        assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [sys.executable, "-B", "-c", "import daily_handoff"], cwd=install,
        capture_output=True, text=True, env=environment, timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def launch_prompt(tmp_path, monkeypatch):
    service = Path(__file__).resolve().parents[1]
    host = service / "host"
    verified_source = None

    def latest_report(root, day, release, *, source_only):
        assert source_only and release == PIN and day == DAY
        if verified_source is None:
            raise ValueError("unverified report")
        return {}, {}, {
            "report_sha256": sha256(verified_source).hexdigest(), "revision": "v05",
        }

    # Stand in for trusted-host configuration, lifecycle and cloud access.
    monkeypatch.setitem(
        sys.modules,
        "daily_release",
        SimpleNamespace(PIN=PIN, SERVICE_ROOT=service, SPACE_ID=SPACE),
    )
    monkeypatch.setitem(
        sys.modules,
        "daily_lifecycle",
        SimpleNamespace(
            cleanup_old_tabs=lambda *_: None,
            record_launch=lambda *_: None,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "daily_support",
        SimpleNamespace(sync_allowed=lambda *_: False),
    )
    monkeypatch.setitem(
        sys.modules,
        "daily_paper",
        SimpleNamespace(latest_report=latest_report),
    )
    monkeypatch.setitem(
        sys.modules,
        "daily_sync",
        SimpleNamespace(
            folder_identity=lambda *_: pytest.fail("unexpected cloud access"),
        ),
    )
    spec = importlib.util.spec_from_file_location(
        "daily_launcher", host / "run-daily.py"
    )
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    codex = tmp_path / ".local/bin/codex"
    codex.parent.mkdir(parents=True)
    codex.touch(mode=0o700)
    sop = tmp_path / ".config/trend-animals/daily-report-sop.md"
    sop.parent.mkdir(parents=True)
    sop.write_text(f"skill-releases/{PIN}/\n")

    root = tmp_path / ".local/share/trend-animals"
    space = {
        "id": SPACE,
        "name": "trend_v3",
        "folder": str(root),
        "machine": None,
        "tabs": [],
    }
    launch_argv = []

    def herdr(argv, **kwargs):
        assert argv[0] == codex.with_name("herdr-app")
        if argv[1:] == ["ls"]:
            reply = {"spaces": [space]}
        else:
            assert argv[1:3] == ["do", "tab.new"] and len(argv) == 4
            request = json.loads(argv[3])
            launch_argv.append(request["argv"])
            space["tabs"].append(
                {
                    "name": request["name"],
                    "terminals": [{"pane": "pane", "terminal": "terminal"}],
                }
            )
            reply = {}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(reply).encode())

    monkeypatch.setattr(subprocess, "run", herdr)

    def launch(*, smoke, existing_report=None, verified_original=None):
        nonlocal verified_source
        verified_source = verified_original
        if existing_report is not None:
            path = root / "reports" / f"{DAY}.html"
            path.parent.mkdir(parents=True)
            path.write_bytes(existing_report)
        result = launcher.run(tmp_path, DAY, "daily-fixture", smoke)
        assert result.startswith("tab created;")
        [argv] = launch_argv
        assert argv[0] == str(codex)
        return argv[-1]

    return launch


def test_scheduled_prompt_requires_visual_board_before_api(launch_prompt):
    prompt = launch_prompt(smoke=False)

    # Reuse must short-circuit fresh acquisition in the actual dispatch prompt.
    reuse = "reuse it without opening WeChat, fetching data or regenerating it"
    app = "read a visible 趋势动物Pro mini-program page"
    board = "大类资产 → 金融资产"
    fallback = "board-data API fallback"
    assert app in prompt
    assert prompt.index(reuse) < prompt.index(app) < prompt.index(board)
    assert prompt.index(board) < prompt.index(fallback)
    assert "Window → 趋势动物Pro" in prompt
    assert "overlapping scrolls to the group tail" in prompt
    assert "every displayed class name and exact strength, including zero" in prompt
    assert "record the actual attempt or applicable prohibition" in prompt
    assert "preserve the verified visual rows" in prompt
    assert "only unreadable or missing board fields" in prompt
    assert "zero board-data API calls" in prompt
    assert "collection.py closeout" in prompt
    assert "board-acquisition.json" in prompt and "daily-completion.json" in prompt
    assert "collection=complete" not in prompt
    assert "workflow_failed" in prompt
    assert "--reuse-sha256" not in prompt
    assert f"paper_cli --report-date {DAY} cycle PLAN" in prompt
    assert "Preopen approvals wait for their opening" in prompt
    assert "Fresh approvals after opening may submit" in prompt
    assert "never authorizes automatic catch-up" in prompt


def test_smoke_prompt_preserves_offline_mode(launch_prompt):
    prompt = launch_prompt(smoke=True)

    assert "offline REHEARSAL" in prompt
    assert "invented UUP data" in prompt
    assert "Use no APIs, keys, external sources" in prompt
    assert "Use no remote resources" in prompt
    assert "WeChat" not in prompt
    assert "大类资产 → 金融资产" not in prompt
    assert "collection.py closeout" not in prompt


@pytest.mark.parametrize("verified_report", (True, False))
def test_scheduled_verified_reuse_is_bound_to_prelaunch_report(
    launch_prompt, verified_report,
):
    original = b"existing private HTML"
    display = b"current HTML with separately verified paper overlay"
    prompt = launch_prompt(
        smoke=False, existing_report=display,
        verified_original=original if verified_report else None,
    )

    proof = f"--reuse-sha256 {sha256(original).hexdigest()} --reuse-revision v05"
    assert (proof in prompt) == verified_report
    assert sha256(display).hexdigest() not in prompt
    assert "verified_reuse" in prompt
