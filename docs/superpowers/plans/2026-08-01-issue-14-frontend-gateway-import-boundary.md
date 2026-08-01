# Issue 14 Frontend Gateway Import Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both supported `frontend-gateway` launch paths start without importing Open Trader domain, adapter, or worker implementations.

**Architecture:** `open_trader.__main__` becomes the shared thin command dispatcher used by `python -m open_trader` and the installed `open-trader` script. It routes only `frontend-gateway` to a small parser inside `frontend_gateway.py`; every other command lazily delegates to the existing monolithic CLI unchanged.

**Tech Stack:** Python 3 standard library (`argparse`, `sys`), pytest, existing Gateway smoke workflow.

## Global Constraints

- Preserve all existing Dashboard content, interaction, API, strategy, execution, notification, and worker behavior.
- Preserve `frontend-gateway` defaults: listener `127.0.0.1:8766`, upstream `127.0.0.1:8767`, public origin `http://127.0.0.1:8766`, timeout `30.0` seconds, and packaged static directory.
- Add no dependency and do not refactor the monolithic CLI.
- Do not change production ports or launchd services in Issue #14.
- Run `make acceptance` only after focused tests, direct dual-process smoke, full tests, source commit, and changelog commit are complete.

---

### Task 1: Route Gateway Launches Before the Domain CLI Import

**Files:**

- Modify: `src/open_trader/__main__.py`
- Modify: `src/open_trader/frontend_gateway.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_frontend_gateway_cli.py`

**Interfaces:**

- Consumes: command arguments in `sys.argv[1:]` or an explicit `list[str]`.
- Produces: `open_trader.__main__.main(argv: list[str] | None = None) -> int` and `open_trader.frontend_gateway.main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing isolation and dispatch tests**

Add a subprocess test that executes `open_trader.__main__` with `frontend-gateway --help`, catches `SystemExit`, then prints `sys.modules`. Assert that representative domain modules are absent:

```python
def test_frontend_gateway_module_entrypoint_does_not_import_domain_modules() -> None:
    script = """
import runpy
import sys
sys.argv = ["open_trader", "frontend-gateway", "--help"]
try:
    runpy.run_module("open_trader", run_name="__main__")
except SystemExit as error:
    assert error.code == 0
print("LOADED_MODULES")
print("\\n".join(sorted(sys.modules)))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = completed.stdout.split("LOADED_MODULES\n", 1)[1].splitlines()
    assert "open_trader.dashboard_web" not in loaded
    assert "open_trader.polymarket_monitor" not in loaded
    assert "open_trader.advice.tradingagents_adapter" not in loaded
```

Extend the existing CLI tests to call `open_trader.frontend_gateway.main()` and assert the same exact defaults and explicit overrides already covered through `open_trader.cli.main()`.

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/pytest tests/test_frontend_gateway_cli.py -q
```

Expected: the subprocess assertion fails because `src/open_trader/__main__.py` imports `open_trader.cli` before inspecting the command.

- [ ] **Step 3: Add the minimal standalone Gateway parser**

Add `main()` to `frontend_gateway.py` using `argparse` and the existing config/server functions:

```python
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-trader frontend-gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=8767)
    parser.add_argument("--public-origin", default="http://127.0.0.1:8766")
    parser.add_argument("--upstream-timeout", type=float, default=30.0)
    parser.add_argument("--static-dir", type=Path, default=Path(__file__).with_name("dashboard_static"))
    args = parser.parse_args(argv)
    serve_frontend_gateway(
        config=FrontendGatewayConfig(
            static_dir=args.static_dir,
            upstream_host=args.upstream_host,
            upstream_port=args.upstream_port,
            public_origin=args.public_origin,
            upstream_timeout_seconds=args.upstream_timeout,
        ),
        host=args.host,
        port=args.port,
    )
    return 0
```

- [ ] **Step 4: Make the package entrypoint dispatch lazily**

Replace the eager CLI import in `__main__.py`:

```python
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["frontend-gateway"]:
        from .frontend_gateway import main as frontend_gateway_main

        return frontend_gateway_main(args[1:])
    from .cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

Point `[project.scripts] open-trader` at `open_trader.__main__:main` so the installed command uses the same isolation boundary.

- [ ] **Step 5: Run focused tests and both direct help commands**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/pytest tests/test_frontend_gateway.py tests/test_frontend_gateway_cli.py tests/test_dashboard_cli.py -q
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m open_trader frontend-gateway --help
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m open_trader dashboard --help
```

Expected: all tests pass; the Gateway help shows its seven options, and Dashboard help remains unchanged.

- [ ] **Step 6: Commit the import-boundary fix**

```bash
git add src/open_trader/__main__.py src/open_trader/frontend_gateway.py \
  pyproject.toml tests/test_frontend_gateway_cli.py
git commit -m "fix: keep frontend gateway imports lightweight"
```

