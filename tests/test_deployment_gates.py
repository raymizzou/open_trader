from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_host_readiness(tmp_path: Path, *, browser_available: bool) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    _write_executable(
        fake_python,
        """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "open_trader" ] && [ "$3" = "prediction-arb" ] && [ "$4" = "nleg-validate" ]; then
    printf '%s\\n' '{"replay":{"status":"PASS"},"live":{"reason":"LIVE_CATALOG_UNAVAILABLE"}}'
    exit 0
fi
case "$*" in
    *"prediction-arb status"*) exit 124 ;;
esac
exit 0
""",
    )
    _write_executable(fake_bin / "lsof", "#!/bin/sh\nexit 0\n")
    _write_executable(
        fake_bin / "node",
        "#!/bin/sh\nexit %s\n" % ("0" if browser_available else "1"),
    )

    runtime_root = tmp_path / "runtime"
    playwright_bin = runtime_root / "node_modules" / ".bin" / "playwright"
    playwright_bin.parent.mkdir(parents=True)
    if browser_available:
        _write_executable(playwright_bin, "#!/bin/sh\nexit 0\n")

    daily_config = tmp_path / "daily_premarket.env"
    daily_config.write_text(
        f"OPEN_TRADER_REPO={ROOT}\n"
        f"OPEN_TRADER_PYTHON={fake_python}\n"
        "OPEN_TRADER_TREND_EXECUTOR_HOST=never-this-host\n",
        encoding="utf-8",
    )

    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["TMPDIR"] = str(tmp_path)
    return subprocess.run(
        [
            "make",
            "host-readiness",
            f"PYTHON_BIN={fake_python}",
            f"REPOSITORY_ROOT={runtime_root}",
            f"PLAYWRIGHT_NODE_PATH={runtime_root / 'node_modules'}",
            f"DAILY_CONFIG={daily_config}",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_host_readiness_does_not_require_old_prediction_state(tmp_path: Path) -> None:
    result = _run_host_readiness(tmp_path, browser_available=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("READY")


def test_host_readiness_still_blocks_missing_browser(tmp_path: Path) -> None:
    result = _run_host_readiness(tmp_path, browser_available=False)

    assert result.returncode != 0
    assert "Playwright Chromium: BLOCKED" in result.stdout
    assert result.stdout.rstrip().endswith("BLOCKED")
