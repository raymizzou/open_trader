import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

DAY = "2025-01-02"
PIN = "a" * 40
SPACE = "sp00000000"


@pytest.fixture
def launch_prompt(tmp_path, monkeypatch):
    service = Path(__file__).resolve().parents[1]
    host = service / "host"
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

    def launch(*, smoke):
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


def test_smoke_prompt_preserves_offline_mode(launch_prompt):
    prompt = launch_prompt(smoke=True)

    assert "offline REHEARSAL" in prompt
    assert "invented UUP data" in prompt
    assert "Use no APIs, keys, external sources" in prompt
    assert "Use no remote resources" in prompt
    assert "WeChat" not in prompt
    assert "大类资产 → 金融资产" not in prompt
