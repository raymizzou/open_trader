# Live Broker Statement Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `import-statements` stop requiring Futu and Tiger statements so current Futu/Tiger holdings come only from live account sync.

**Architecture:** Keep `run_import()` generic and unchanged. Narrow the CLI monthly import wiring to Phillips-only statement parsing, while leaving historical Futu/Tiger parser modules and parser tests intact. Update docs so users run Phillips monthly import first, then Futu and Tiger live sync dry-runs/promotions.

**Tech Stack:** Python 3.12, argparse CLI, existing parser/pipeline modules, pytest.

---

## File Structure

- Modify `src/open_trader/cli.py`
  - Remove `FutuStatementParser` and `TigerStatementParser` imports if unused.
  - Remove `--futu` and `--tiger` arguments from `import-statements`.
  - Pass only Phillips statement path/parser into `run_import()`.

- Modify `tests/test_pipeline.py`
  - Update import CLI argument validation tests to omit Futu/Tiger.
  - Add help assertion that Futu/Tiger statement flags are absent.
  - Update main wiring test to assert only Phillips is passed to `run_import()`.

- Modify `README.md`, `README.zh-CN.md`, `docs/monthly_portfolio_import.md`
  - Replace three-broker monthly import examples with Phillips-only examples.
  - State that Futu and Tiger current holdings come from live sync.
  - Remove text saying Tiger is still required by `import-statements`.

## Task 1: Narrow Import CLI to Phillips

**Files:**
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Update failing tests for the new CLI contract**

In `tests/test_pipeline.py`, update `test_import_statements_help_includes_usd_hkd` to also assert the old broker flags are absent:

```python
def test_import_statements_help_includes_usd_hkd(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["import-statements", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--usd-hkd" in output
    assert "--phillips" in output
    assert "--futu" not in output
    assert "--tiger" not in output
```

Update invalid month and invalid FX parser invocations so they pass only `--phillips`:

```python
[
    "import-statements",
    "--month",
    month,
    "--phillips",
    "phillips.pdf",
    "--usd-hkd",
    "7.8",
]
```

```python
[
    "import-statements",
    "--month",
    "2026-05",
    "--phillips",
    "phillips.pdf",
    "--usd-hkd",
    rate,
]
```

Update `test_import_statements_main_calls_pipeline_and_prints_summary` CLI args:

```python
[
    "import-statements",
    "--month",
    "2026-05",
    "--phillips",
    "phillips.pdf",
    "--data-dir",
    str(tmp_path / "data"),
    "--usd-hkd",
    "7.8",
]
```

Update its expected `statement_paths` assertion:

```python
assert captured["statement_paths"] == {
    "phillips": Path("phillips.pdf"),
}
```

- [ ] **Step 2: Run the focused tests and verify they fail before implementation**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_pipeline.py::test_import_statements_help_includes_usd_hkd tests/test_pipeline.py::test_import_statements_main_calls_pipeline_and_prints_summary -q
```

Expected before implementation: fail because help still includes `--futu` and `--tiger`, and CLI still expects those args.

- [ ] **Step 3: Update CLI import parser arguments**

In `src/open_trader/cli.py`, remove these imports if they become unused:

```python
from .parsers.futu import FutuStatementParser
from .parsers.tiger import TigerStatementParser
```

In `build_parser()`, replace the broker arguments with:

```python
import_parser.add_argument("--phillips", type=Path, required=True)
```

- [ ] **Step 4: Update CLI import command wiring**

In `main()`, replace the `run_import()` call statement paths and parsers with:

```python
statement_paths={
    "phillips": args.phillips,
},
parsers=[
    PhillipsStatementParser(),
],
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_pipeline.py -q
```

Expected: all `tests/test_pipeline.py` tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/cli.py tests/test_pipeline.py
git commit -m "feat: make statement import live-broker aware"
```

## Task 2: Update User Documentation

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/monthly_portfolio_import.md`

- [ ] **Step 1: Update README monthly import examples**

In `README.md`, replace the monthly import example with:

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-05 \
  --phillips /path/to/phillips.pdf \
  --usd-hkd 7.85
```

Add one sentence after the output block:

```markdown
Futu and Tiger current holdings are refreshed through live account sync commands, not monthly statement import.
```

- [ ] **Step 2: Update Chinese README monthly import examples**

In `README.zh-CN.md`, replace the monthly import example with:

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-05 \
  --phillips /path/to/phillips.pdf \
  --usd-hkd 7.85
```

Add one Chinese sentence after the output block:

```markdown
Futu 和 Tiger 的当前持仓通过 live account sync 更新，不再依赖月结单导入。
```

- [ ] **Step 3: Update monthly workflow docs**

In `docs/monthly_portfolio_import.md`, replace the opening command with:

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-05 \
  --phillips /Users/ray/Downloads/phillips.pdf \
  --usd-hkd 7.85
```

Replace the sentence after the command with:

```markdown
Update `--month` and `--usd-hkd` for the target statement month. Replace the Phillips PDF path if the file is stored elsewhere. Futu and Tiger current holdings are refreshed through their live account sync commands.
```

Replace Futu live sync limitation text with:

```markdown
`sync-futu-portfolio` operates on the current `data/latest/portfolio.csv` and
replaces Futu-only rows with live Futu holdings and cash from Futu OpenD. It
keeps non-Futu broker rows from the current portfolio.
```

Replace Tiger live sync limitation text with:

```markdown
`sync-tiger-portfolio` operates on the current `data/latest/portfolio.csv`,
replaces Tiger-only rows with current Tiger OpenAPI holdings and cash, and
preserves non-Tiger rows from the current portfolio.
```

- [ ] **Step 4: Search for stale docs**

Run:

```bash
rg -n -- '--futu|--tiger|tiger.pdf|futu.pdf|still requires all statement inputs|including Tiger|including Futu' README.md README.zh-CN.md docs/monthly_portfolio_import.md
```

Expected: no stale monthly import examples or limitation text. Matches in live sync sections are acceptable only if they refer to command names such as `sync-tiger-portfolio`.

- [ ] **Step 5: Commit**

```bash
git add README.md README.zh-CN.md docs/monthly_portfolio_import.md
git commit -m "docs: document live broker import boundary"
```

## Task 3: Verify End to End

**Files:**
- Verify only

- [ ] **Step 1: Run focused broker boundary tests**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_pipeline.py tests/test_futu_account.py tests/test_tiger_account.py tests/test_tiger_account_cli.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run CLI help checks**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m open_trader import-statements --help
/Users/ray/projects/open_trader/.venv/bin/python -m open_trader sync-futu-portfolio --help
/Users/ray/projects/open_trader/.venv/bin/python -m open_trader sync-tiger-portfolio --help
```

Expected: `import-statements` help shows `--phillips` and does not show `--futu` or `--tiger`.

- [ ] **Step 3: Run full test suite**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q
```

Expected: full suite passes.

- [ ] **Step 4: Sensitive data scan**

Run:

```bash
git diff 99a19a8..HEAD | rg -n "(BEGIN (RSA |OPENSSH |PRIVATE )?PRIVATE KEY|TIGEROPEN_(PRIVATE_KEY|SECRET_KEY|TOKEN)=|account\\s*=\\s*['\\\"]?[0-9]{6,})" || true
```

Expected: no real secrets. Test fixture dummy strings are acceptable if tests were changed to add them, but this plan should not add any.

- [ ] **Step 5: Final code review**

Request one final code review over the range from `e2b1af4` to `HEAD`, focusing on whether monthly import no longer requires Futu/Tiger and whether live sync behavior is preserved.
