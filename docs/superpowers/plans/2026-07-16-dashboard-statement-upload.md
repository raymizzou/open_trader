# Dashboard Statement Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add desktop-only, right-aligned statement-upload buttons for Phillips and Eastmoney that immediately and safely replace only the selected broker's account data.

**Architecture:** The browser sends the selected PDF as a raw `application/pdf` body to a broker-specific local-only endpoint, avoiding multipart parsing and new dependencies. A focused statement-import service detects the date with the existing broker parser, rejects stale or invalid input, archives the PDF, and calls a dated pipeline import that atomically replaces only that broker while preserving every other broker. The existing Dashboard payload is reloaded after success; reports, notifications, and watchers are not triggered.

**Tech Stack:** Python 3.12 stdlib HTTP server, `pdfplumber`, existing portfolio pipeline, vanilla JavaScript/CSS, pytest, Playwright acceptance.

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/dashboard-statement-upload` on `feature/dashboard-statement-upload`, created directly from local `main` commit `c60cbcc`.
- Preserve unrelated dirty changes in `/Users/ray/projects/open_trader`; never implement in that checkout.
- Baseline is `2200 passed`; the worktree needs the ignored `data/latest/kelly_strategy_templates.json` link to canonical project data.
- Upload UI is desktop-only and hidden at `max-width: 760px`; existing mobile Dashboard behavior must still pass regression acceptance.
- Upload is immediate: no preview and no second confirmation.
- Accept one PDF up to exactly `20 * 1024 * 1024` bytes from loopback clients only.
- Use fixed existing rates: `USD/HKD = 7.8`, `CNY/HKD = 1.08`.
- Eastmoney's password comes only from `OPEN_TRADER_EASTMONEY_PDF_PASSWORD` in the configured local env file; it is never returned to or submitted by the browser.
- Parse or write failure must leave previous portfolio, run data, and archived statement intact.
- Replace the target broker as a whole, preserve other brokers, and reject unsafe mixed-broker aggregate rows instead of guessing how to split them.
- Reject statements older than the target broker's current source; allow same-date re-imports.
- Do not regenerate trend reports, send notifications, or invoke watchers.
- Do not run `make acceptance` during development. Run it once as the final gate after focused tests and real upload checks pass.
- Only `make acceptance` `PASS`, followed by redeployment of the exact accepted SHA and PID/cwd/SHA/log/HTTP verification, is review-ready.

---

### Task 1: Extract authoritative statement dates

**Files:**
- Modify: `src/open_trader/parsers/phillips.py`
- Modify: `src/open_trader/parsers/eastmoney.py`
- Modify: `tests/test_parsers_text.py`
- Modify: `tests/test_eastmoney_parser.py`

**Interfaces:**
- Produces: `PhillipsStatementParser.statement_date(path: Path) -> str`
- Produces: `EastmoneyStatementParser.statement_date(path: Path) -> str`
- Both return canonical `YYYY-MM-DD` and raise a sanitized `ValueError` when the date is missing or the PDF cannot be opened.

- [ ] **Step 1: Write failing date-extraction tests**

```python
def test_phillips_parser_extracts_issue_date(monkeypatch) -> None:
    monkeypatch.setattr(
        "open_trader.parsers.phillips.pdfplumber.open",
        fake_pdf("日期 Issue Date : 10/07/26"),
    )
    assert PhillipsStatementParser().statement_date(Path("statement.pdf")) == "2026-07-10"


def test_eastmoney_parser_extracts_print_date_without_exposing_password(monkeypatch) -> None:
    monkeypatch.setattr(
        "open_trader.parsers.eastmoney.pdfplumber.open",
        fake_pdf("打印日期：2026-07-12"),
    )
    assert EastmoneyStatementParser("secret").statement_date(Path("statement.pdf")) == "2026-07-12"
```

Add missing-date and malformed-date cases that assert the error names the broker but contains neither PDF password nor extracted account text.

- [ ] **Step 2: Run the focused parser tests and confirm RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_parsers_text.py tests/test_eastmoney_parser.py
```

Expected: failures because `statement_date` does not exist.

- [ ] **Step 3: Implement the minimum date readers**

```python
PHILLIPS_ISSUE_DATE = re.compile(
    r"(?:日期\s*)?Issue Date\s*[:：]\s*(\d{2})/(\d{2})/(\d{2})",
    re.IGNORECASE,
)

def _statement_date(text: str) -> str:
    match = PHILLIPS_ISSUE_DATE.search(text)
    if match is None:
        raise ValueError("辉立结单缺少 Issue Date")
    day, month, year = match.groups()
    return date(2000 + int(year), int(month), int(day)).isoformat()
```

Eastmoney uses a strict `打印日期：YYYY-MM-DD` regex and `date.fromisoformat`. Open only the first page for date detection and reuse each parser's existing sanitized open/decryption error wording.

- [ ] **Step 4: Re-run parser tests and confirm GREEN**

Expected: all tests in both files pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/parsers/phillips.py src/open_trader/parsers/eastmoney.py \
  tests/test_parsers_text.py tests/test_eastmoney_parser.py
git commit -m "feat: detect broker statement dates"
```

### Task 2: Add generic target-broker replacement to the import pipeline

**Files:**
- Modify: `src/open_trader/portfolio.py`
- Modify: `src/open_trader/pipeline.py`
- Modify: `tests/test_portfolio.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `replace_broker_portfolio_rows(existing_rows, new_rows, broker) -> list[dict[str, str]]`
- Produces: `run_uploaded_statement(statement_date, statement_path, parser, data_dir, portfolio_path, fx_provider) -> ImportResult`
- Existing `run_import(...)` and `merge_eastmoney_portfolio_rows(...)` remain compatible.

- [ ] **Step 1: Write failing broker-replacement tests**

```python
def test_replace_broker_rows_preserves_other_brokers_and_drops_closed_positions() -> None:
    existing = [portfolio_row(brokers="futu", symbol="A"), portfolio_row(brokers="phillips", symbol="OLD")]
    replacement = [portfolio_row(brokers="phillips", symbol="NEW")]
    rows = replace_broker_portfolio_rows(existing, replacement, "phillips")
    assert {(row["brokers"], row["symbol"]) for row in rows} == {("futu", "A"), ("phillips", "NEW")}
```

Also assert rejection of existing `futu;phillips` rows, rejection of replacement rows belonging to the wrong broker, identity collision with a preserved broker, and recalculated weights.

- [ ] **Step 2: Run portfolio tests and confirm RED**

Run `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_portfolio.py`.

- [ ] **Step 3: Generalize the existing Eastmoney merge**

```python
def replace_broker_portfolio_rows(existing_rows, new_rows, broker):
    target = broker.strip().lower()
    # Keep the existing normalization, finite-money validation, derived-field
    # recalculation, collision rejection, sorting, and weight recalculation.
    # ponytail: aggregate mixed-broker rows cannot be split safely; rebuild from
    # per-broker details if shared-symbol holdings become common.

def merge_eastmoney_portfolio_rows(existing_rows, eastmoney_rows):
    return replace_broker_portfolio_rows(existing_rows, eastmoney_rows, "eastmoney")
```

- [ ] **Step 4: Write failing dated-upload pipeline tests**

Cover a `2026-07-10` run directory and full-date statement IDs, preservation of unrelated latest rows, replacement of same-broker latest rows, same-date rerun, and rollback when latest promotion fails.

- [ ] **Step 5: Run pipeline tests and confirm RED**

Run `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_pipeline.py`.

- [ ] **Step 6: Extract a shared internal import and add the upload wrapper**

```python
def run_uploaded_statement(
    *, statement_date: str, statement_path: Path, parser: StatementParser,
    data_dir: Path, portfolio_path: Path,
    fx_provider: StaticMonthEndFxProvider,
) -> ImportResult:
    date.fromisoformat(statement_date)
    return _run_import(
        statement_period=statement_date,
        run_name=statement_date,
        statement_paths={parser.broker: statement_path},
        parsers=[parser],
        data_dir=data_dir,
        latest_path=portfolio_path,
        fx_provider=fx_provider,
        update_latest=True,
        replace_latest_broker=parser.broker,
    )
```

Keep `run_import` as the monthly wrapper. Before promoting latest, read existing latest rows and call `replace_broker_portfolio_rows` for uploaded statements. Reuse the pipeline's existing temp/backup/rollback mechanism.

- [ ] **Step 7: Re-run portfolio and pipeline tests and confirm GREEN**

- [ ] **Step 8: Commit**

```bash
git add src/open_trader/portfolio.py src/open_trader/pipeline.py \
  tests/test_portfolio.py tests/test_pipeline.py
git commit -m "feat: replace one broker during statement import"
```

### Task 3: Implement the local statement-import service

**Files:**
- Create: `src/open_trader/statement_import.py`
- Create: `tests/test_statement_import.py`

**Interfaces:**
- Produces: `StatementImportService(data_dir, portfolio_path, eastmoney_password)`
- Produces: `import_pdf(broker: str, body: bytes) -> dict[str, object]`
- Response keys: `status`, `broker`, `statement_date`, `positions`, `cash`, `warnings`.

- [ ] **Step 1: Write failing service tests**

Use injected parser factories and a temporary data directory to cover:

```python
def test_import_pdf_archives_and_replaces_only_target_broker(tmp_path) -> None:
    result = service(tmp_path, date="2026-07-10").import_pdf("phillips", PDF_BYTES)
    assert result == {
        "status": "ok", "broker": "phillips", "statement_date": "2026-07-10",
        "positions": 1, "cash": 1, "warnings": 0,
    }
    assert (tmp_path / "statements/phillips/2026-07-10/statement.pdf").read_bytes() == PDF_BYTES
```

Also cover Eastmoney's monthly archive path, password injection, empty parse rejection, older-date rejection, same-date retry, unsupported broker, archive rollback on pipeline failure, and the exact fixed FX rates.

- [ ] **Step 2: Run the service tests and confirm RED**

Run `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_statement_import.py`.

- [ ] **Step 3: Implement the small service**

```python
RATES_TO_HKD = {"phillips": {"USD": Decimal("7.8")}, "eastmoney": {"CNY": Decimal("1.08")}}

class StatementImportService:
    def import_pdf(self, broker: str, body: bytes) -> dict[str, object]:
        parser = self._parser(broker)
        with self._temporary_pdf(body) as uploaded:
            statement_date = parser.statement_date(uploaded)
            parsed = parser.parse(uploaded, statement_date)
            if not parsed.positions and not parsed.cash_balances:
                raise ValueError(f"{broker} 结单没有可导入的持仓或现金")
            self._reject_older_statement(broker, statement_date, parser)
            archive = self._archive_path(broker, statement_date)
            backup = self._promote_archive(uploaded, archive)
            try:
                result = run_uploaded_statement(
                    statement_date=statement_date,
                    statement_path=archive,
                    parser=parser,
                    data_dir=self.data_dir,
                    portfolio_path=self.portfolio_path,
                    fx_provider=StaticMonthEndFxProvider(
                        statement_date[:7], RATES_TO_HKD[broker], fx_date=statement_date,
                    ),
                )
            except Exception:
                self._restore_archive(archive, backup)
                raise
            self._discard_backup(backup)
            return self._response(broker, statement_date, result)
```

Read the current source date from the target broker's latest manifest/source PDF when possible; fall back to the latest statement ID period. Do not log source text or password.

- [ ] **Step 4: Re-run service tests and confirm GREEN**

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/statement_import.py tests/test_statement_import.py
git commit -m "feat: import uploaded broker statements"
```

### Task 4: Expose a bounded loopback-only PDF endpoint

**Files:**
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/test_dashboard_cli.py`

**Interfaces:**
- Consumes: `StatementImportService.import_pdf`.
- Produces: `POST /api/statements/phillips` and `POST /api/statements/eastmoney` with raw PDF bodies.

- [ ] **Step 1: Write failing endpoint tests**

Start the real `ThreadingHTTPServer` with an injected fake importer. Assert `200` JSON for a loopback `application/pdf` request, `400` for wrong content type or missing `%PDF-` header, `404` for unsupported broker, `413` above 20 MiB, and `403` when the factored address predicate receives a non-loopback address. Assert importer exceptions return sanitized JSON without changing the fake's prior state.

- [ ] **Step 2: Run web/CLI tests and confirm RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py tests/test_dashboard_cli.py
```

- [ ] **Step 3: Add the raw body reader and endpoint**

```python
MAX_PDF_BODY_BYTES = 20 * 1024 * 1024

def _is_loopback_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback
```

Require `Content-Length`, `application/pdf`, and `%PDF-`. Serialize `/api/quotes` account sync and statement import with one closure-level `threading.Lock` so the Dashboard cannot overwrite a just-imported portfolio with an overlapping refresh.

- [ ] **Step 4: Wire the existing local env config into Dashboard startup**

Add Dashboard `--config` with default `config/daily_premarket.env`, load it with `_load_optional_env_values`, and pass only `OPEN_TRADER_EASTMONEY_PDF_PASSWORD` to `serve_dashboard`. Keep the password out of logs and payloads.

- [ ] **Step 5: Re-run web/CLI tests and confirm GREEN**

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/dashboard_web.py src/open_trader/cli.py \
  tests/test_dashboard_web.py tests/test_dashboard_cli.py
git commit -m "feat: expose local statement upload endpoint"
```

### Task 5: Add the desktop-only right-aligned upload control

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/e2e/dashboard-warm-ledger.spec.ts`

**Interfaces:**
- Consumes: the two raw-PDF endpoints.
- Produces: one desktop control in each Phillips/Eastmoney account header.

- [ ] **Step 1: Write failing DOM and browser tests**

Assert Phillips and Eastmoney render a right-side `.statement-upload` control, Futu and Tiger do not, selecting a valid PDF performs one POST and reloads `/api/dashboard`, upload state disables the button, and server error text appears beside the button. In Playwright, assert the controls are visible at desktop width and absent at `375px`.

- [ ] **Step 2: Run JS/static and E2E focused tests and confirm RED**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard_web.py
npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts
```

- [ ] **Step 3: Add minimal upload state and event delegation**

```javascript
statementUpload: {broker: "", busy: false, message: "", error: false},

async function uploadStatement(broker, file) {
  if (!/\.pdf$/i.test(file.name)) throw new Error("请选择 PDF 文件");
  if (file.size > 20 * 1024 * 1024) throw new Error("PDF 不能超过 20 MiB");
  const response = await fetch(`/api/statements/${encodeURIComponent(broker)}`, {
    method: "POST", headers: {"Content-Type": "application/pdf"}, body: file,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || `上传失败 (${response.status})`);
  await loadDashboard();
  return payload;
}
```

Render the hidden file input and button only for Phillips/Eastmoney. While busy, show `上传中…`; on success show date and position count, then clear after four seconds; on failure leave the error visible until the next attempt.

- [ ] **Step 4: Align the action at the far right and hide it on mobile**

```css
.account-section-actions { margin-left: auto; text-align: right; }
.statement-upload-status { display: block; }
@media (max-width: 760px) { .statement-upload { display: none; } }
```

Keep keyboard focus styling and a `role="status"` message for desktop accessibility.

- [ ] **Step 5: Re-run focused JS/E2E tests and confirm GREEN**

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py \
  tests/e2e/dashboard-warm-ledger.spec.ts
git commit -m "feat: add desktop statement upload controls"
```

### Task 6: Extend acceptance and verify the real workflow

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `README.zh-CN.md`

**Interfaces:**
- Produces: acceptance assertions that desktop has the two controls and mobile has none.

- [ ] **Step 1: Write failing acceptance tests**

Add fake-page expectations for two visible desktop upload buttons and zero visible mobile upload buttons. Do not make acceptance upload or mutate data; the real mutation is checked directly before the final gate.

- [ ] **Step 2: Implement the acceptance browser assertions and document the local workflow**

Document the desktop button, local-only restriction, direct replacement semantics, fixed FX rates, password source, and archive paths. Do not document a preview or mobile flow.

- [ ] **Step 3: Run all focused automated checks**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_parsers_text.py tests/test_eastmoney_parser.py \
  tests/test_portfolio.py tests/test_pipeline.py tests/test_statement_import.py \
  tests/test_dashboard_web.py tests/test_dashboard_cli.py \
  tests/test_dashboard_acceptance.py
npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts
```

Record exact pass/fail output.

- [ ] **Step 4: Run the real direct upload workflow before acceptance**

Start the candidate Dashboard from this worktree with canonical absolute project data, reports, and config paths. Upload the existing real Phillips and Eastmoney PDFs to their endpoints with raw `application/pdf` requests. Verify both responses, target statement dates, broker totals, other-broker preservation, archive files, Dashboard refresh, process PID/cwd/SHA, and fresh log lines. Confirm no trend report or notification timestamp changed.

- [ ] **Step 5: Commit the acceptance/docs change and the final implementation state**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py README.zh-CN.md
git commit -m "test: accept desktop statement uploads"
```

- [ ] **Step 6: Run the final gate exactly once after all source commits**

Run `make acceptance` from the feature worktree against the candidate process. Required result: `PASS`. On `FAIL`, fix, recommit, restart the candidate SHA, and rerun; on `BLOCKED`, report the blocker and do not claim completion.

- [ ] **Step 7: Redeploy the exact accepted SHA**

Restart the Dashboard from `/Users/ray/projects/open_trader/.worktrees/dashboard-statement-upload` at the accepted SHA using canonical absolute project data/report/config paths. Verify the new PID, cwd, SHA, fresh timestamped logs, and HTTP `200` from the review URL. No second acceptance run is needed when source and data are unchanged.
