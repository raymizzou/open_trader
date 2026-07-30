# Statement Upload Source Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an accepted Eastmoney or Phillips PDF statement authoritative based only on broker statement-period freshness, while reporting downstream statistics rebuild failure without rolling back the statement.

**Architecture:** `StatementImportService.import_pdf` keeps one source transaction for archive plus imported run, then performs the derived statistics rebuild through the existing atomic statistics writer in a separate best-effort boundary. The Phillips parser narrows incomplete-execution warnings to rows that explicitly contain `Bought` or `Sold`, and the Dashboard reports a successful import with `统计待重建` when the derived rebuild fails.

**Tech Stack:** Python 3.12, pytest, pdfplumber, stdlib filesystem snapshots, browser JavaScript exercised through the existing Node test harness.

## Global Constraints

- Uploaded PDF statements are the source of truth after parse and statement-period freshness validation.
- Reject only a statement period older than the broker's current accepted period; allow same-period replacement.
- Do not use server current time, `generated_at`, or `statistics_cutoff_at` to accept or reject a statement.
- A statistics build or atomic-write failure must keep the new statement archive and imported run, retain the previous statistics file, and return `statistics_status="failed"`.
- Only an unparseable `Bought` or `Sold` row may create a Phillips `invalid_execution_row` warning; Payment, Deposit, balances, headings, and wrapped non-trade text do not reduce fill completeness.
- Do not add a queue, background job, database, dependency, or global relaxation of the trend statistics time invariant.

---

### Task 1: Phillips Non-Trade Transaction Rows

**Files:**
- Modify: `src/open_trader/parsers/phillips.py:118-158`
- Test: `tests/test_parsers_text.py:242-330`

**Interfaces:**
- Consumes: `parse_phillips_text(text: str, month: str) -> ParseResult`
- Produces: `ParseResult.fills_complete=True` and no execution warning for a transaction section containing no `Bought` or `Sold`; malformed candidate executions remain incomplete.

- [ ] **Step 1: Write the failing non-trade regression test**

```python
def test_phillips_payment_and_deposit_rows_do_not_claim_invalid_execution() -> None:
    result = parse_phillips_text(
        "Transaction Details\n"
        "29/07/26 29/07/26 HKD Payment Balance Forward 10,000.00\n"
        "29/07/26 29/07/26 UT Deposit FUND-CODE 1,000.00\n"
        "Opening Balance 0.00\n"
        "Closing Balance 10,000.00\n",
        "2026-07",
    )

    assert result.trades == []
    assert result.fills == []
    assert result.fills_complete is True
    assert result.warnings == []
```

- [ ] **Step 2: Run the new parser test and confirm RED**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_parsers_text.py::test_phillips_payment_and_deposit_rows_do_not_claim_invalid_execution -q`

Expected: FAIL because each non-empty non-trade row currently creates `invalid_execution_row` and sets `fills_complete=False`.

- [ ] **Step 3: Narrow the parser warning gate**

After `_is_ignored_transaction_line(line)`, skip lines without an explicit trade side:

```python
if re.search(r"\b(?:Bought|Sold)\b", line, re.IGNORECASE) is None:
    continue
```

Retain the existing parse and warning path unchanged for any row that contains `Bought` or `Sold`.

- [ ] **Step 4: Run focused Phillips parser coverage**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_parsers_text.py -q`

Expected: PASS after updating old broad expectations so non-trade or unknown-side rows are ignored, while all malformed `Bought`/`Sold` cases still assert `invalid_execution_row`.

- [ ] **Step 5: Commit the parser fix**

```bash
git add src/open_trader/parsers/phillips.py tests/test_parsers_text.py
git commit -m "fix: ignore Phillips non-trade statement rows"
```

### Task 2: Separate Source Acceptance from Derived Statistics

**Files:**
- Modify: `src/open_trader/statement_import.py:46-130`
- Test: `tests/test_statement_import.py:642-690`

**Interfaces:**
- Consumes: `StatementImportService.import_pdf(broker: str, body: bytes) -> dict[str, object]`
- Produces: source success payload containing `statistics_status: Literal["updated", "failed"]`; on failure `actual_rounds=None` and `statistics_cutoff_at=None` because no new statistics cutoff was published.

- [ ] **Step 1: Change the statistics-write regression to assert retained source**

Replace the rollback expectation with:

```python
result = service.import_pdf("phillips", b"%PDF-1.7\ncorrected")

assert result["status"] == "ok"
assert result["statistics_status"] == "failed"
assert result["actual_rounds"] is None
assert result["statistics_cutoff_at"] is None
assert archive.read_bytes() == b"%PDF-1.7\ncorrected"
assert Path(result["run_path"]).is_dir()
assert (data_dir / "latest/trend_api_stats.json").read_bytes() == before_stats
```

Keep assertions that the imported run contains the corrected source facts.

- [ ] **Step 2: Add the Eastmoney same-day clock regression**

Patch `build_statement_actual_stats_payload` to raise `ValueError("generated_at must not precede statistics_cutoff_at")`, import a same-period Eastmoney statement, and assert:

```python
assert result["status"] == "ok"
assert result["broker"] == "eastmoney"
assert result["statistics_status"] == "failed"
assert Path(result["run_path"]).is_dir()
assert archive.read_bytes() == b"%PDF-1.7\nsame period replacement"
assert stats_path.read_bytes() == before_stats
```

- [ ] **Step 3: Run both service tests and confirm RED**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_statement_import.py::test_stats_write_failure_keeps_accepted_source_and_previous_stats tests/test_statement_import.py::test_eastmoney_statistics_clock_failure_keeps_same_period_statement -q`

Expected: FAIL because the current implementation builds statistics before source promotion and rolls back the archive and run for a write error.

- [ ] **Step 4: Implement two transaction boundaries**

In `import_pdf`:

1. Snapshot the broker period run, promote the archive, and call `run_uploaded_statement`.
2. On source failure, restore only that archive and run snapshot, then re-raise.
3. After source success, delete the archive backup.
4. Build and atomically write statistics inside a separate `try`.
5. On statistics failure, rely on the existing atomic writer to retain the previous file and set `stats=None`.
6. Return `statistics_status="updated"` with the new counts/cutoff when `stats` exists; otherwise return `statistics_status="failed"`, `actual_rounds=None`, and `statistics_cutoff_at=None`.

Do not change `_statement_cutoff` or the validation inside `build_statement_actual_stats_payload`.

- [ ] **Step 5: Run the complete statement import suite**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_statement_import.py tests/test_eastmoney_parser.py tests/test_parsers_text.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the source-authority transaction**

```bash
git add src/open_trader/statement_import.py tests/test_statement_import.py
git commit -m "fix: keep accepted statements when stats rebuild fails"
```

### Task 3: Dashboard Result Copy and Operator Log

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:4894-4910`
- Test: `tests/test_dashboard_web.py:843-920`
- Modify: `CHANGELOG.md:6`

**Interfaces:**
- Consumes: successful statement upload JSON with `statement_date`, `positions`, and `statistics_status`
- Produces: `state.statementUpload.message` containing `已导入 <date> · 持仓 <count>` and appending ` · 统计待重建` only when `statistics_status === "failed"`.

- [ ] **Step 1: Write the failing Dashboard message test**

Drive `handleStatementFileSelection` through the existing Node harness with a successful response:

```javascript
globalThis.fetch = async () => ({
  ok: true,
  status: 200,
  json: async () => ({
    status: "ok",
    statement_date: "2026-07-30",
    positions: 12,
    statistics_status: "failed",
  }),
});
await handleStatementFileSelection(event);
console.log(JSON.stringify(state.statementUpload));
```

Assert `error is False` and `message == "已导入 2026-07-30 · 持仓 12 · 统计待重建"`.

- [ ] **Step 2: Run the new Dashboard test and confirm RED**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_statement_upload_reports_deferred_statistics_without_failing -q`

Expected: FAIL because the current success message does not inspect `statistics_status`.

- [ ] **Step 3: Implement the conditional message**

```javascript
const statisticsMessage =
  payload.statistics_status === "failed" ? " · 统计待重建" : "";
state.statementUpload = {
  broker,
  busy: false,
  message: `已导入 ${payload.statement_date} · 持仓 ${payload.positions}${statisticsMessage}`,
  error: false,
};
```

- [ ] **Step 4: Add the dated operator log**

Add a 2026-07-30 `CHANGELOG.md` entry stating that statement freshness now depends only on broker period, statistics failure no longer rolls back an accepted upload, and Phillips Payment/Deposit rows no longer create false execution warnings.

- [ ] **Step 5: Run focused Dashboard and service tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the UI and changelog**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py CHANGELOG.md
git commit -m "fix: report deferred statement statistics"
```

### Task 4: Real PDF and Final Dashboard Verification

**Files:**
- Verify: `/Users/ray/Downloads/电子对账单 3.pdf`
- Verify: `/Users/ray/Downloads/PDF document-CB4DF68D154F-1.pdf`
- Verify: `data/statements/`, `data/runs/`, `data/latest/trend_api_stats.json`

**Interfaces:**
- Consumes: the two user-supplied PDF statements through `StatementImportService.import_pdf`
- Produces: isolated source archives and runs with truthful statistics status; accepted Dashboard SHA deployed on `http://127.0.0.1:8766/`.

- [ ] **Step 1: Run both real PDFs in an isolated data directory**

Use the real parsers and `StatementImportService`, copy only the minimum existing report/statistics fixtures needed into a temporary directory, and assert:

```python
assert eastmoney["status"] == "ok"
assert eastmoney["statement_date"] == "2026-07-30"
assert eastmoney["statistics_status"] == "failed"
assert Path(eastmoney["run_path"]).is_dir()
assert phillips["status"] == "ok"
assert phillips["statement_date"] == "2026-07-29"
assert phillips["warnings"] == 0
assert Path(phillips["run_path"]).is_dir()
```

Do not write either real PDF into the live data directory during this check.

- [ ] **Step 2: Run the full suite from root runtime-data context**

Run:

```bash
PYTHONSAFEPATH=1 \
PYTHONPATH="/Users/ray/projects/open_trader/.worktrees/statement-upload-source-authority:/Users/ray/projects/open_trader/.worktrees/statement-upload-source-authority/src" \
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
/Users/ray/projects/open_trader/.worktrees/statement-upload-source-authority/tests -q
```

Expected: all tests pass; compare the count with the pre-change baseline `3862 passed in 116.25s`.

- [ ] **Step 3: Commit any verification-only corrections**

```bash
git add src tests CHANGELOG.md docs/superpowers
git commit -m "test: cover real statement upload boundaries"
```

Skip this commit when verification produces no source changes.

- [ ] **Step 4: Run the final Dashboard acceptance gate**

Run: `make acceptance`

Expected: `PASS`. Fix any `FAIL`; report `BLOCKED` without substituting curl, fixtures, or screenshots.

- [ ] **Step 5: Redeploy the exact accepted SHA**

Restart the Dashboard and affected account-sync process using the repository operator scripts. Verify the new PID, working directory, exact Git SHA, fresh log timestamp, and HTTP 200 at `http://127.0.0.1:8766/`.

- [ ] **Step 6: Record final evidence**

Run:

```bash
git status --short --branch
git rev-parse HEAD
curl --fail --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: clean feature branch, accepted/deployed SHA unchanged, and `200`.
