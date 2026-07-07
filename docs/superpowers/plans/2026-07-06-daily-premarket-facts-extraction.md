# Daily Premarket Facts Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make daily premarket keep running non-blocking while still producing current `decision_facts`, `technical_facts`, and extraction status without overwriting usable latest caches with skipped placeholders.

**Architecture:** Split fact generation from the critical daily premarket path. Daily premarket writes advice, plans, and trade actions synchronously, records a pending facts job, and promotes latest facts only when a facts artifact is usable or intentionally newer. A separate command/job processes pending facts, retries transient LLM schema failures, and writes explicit status for the dashboard and daily run report.

**Tech Stack:** Python stdlib, existing `open_trader` CLI, pytest, existing JSON/CSV artifact layout under `data/runs/<date>/<market>` and `data/latest/<market>`.

---

### Task 1: Define Usable Fact Promotion Policy

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Test: `tests/test_daily_premarket.py`

- [ ] **Step 1: Write failing tests for promotion policy**

Add tests near `test_promote_latest_set_skips_non_blocking_fact_placeholders`:

```python
def test_should_promote_latest_fact_accepts_successful_records(tmp_path: Path) -> None:
    path = tmp_path / "decision_facts.json"
    path.write_text(
        json.dumps({"records": [{"symbol": "MSFT"}]}),
        encoding="utf-8",
    )

    assert daily_premarket._should_promote_latest_fact(path) is True


def test_should_promote_latest_fact_rejects_non_blocking_placeholder(tmp_path: Path) -> None:
    path = tmp_path / "decision_facts.json"
    path.write_text(
        json.dumps(
            {
                "status": "skipped",
                "reason": "daily_premarket_non_blocking",
                "records": [],
            }
        ),
        encoding="utf-8",
    )

    assert daily_premarket._should_promote_latest_fact(path) is False
```

- [ ] **Step 2: Run the tests and verify the new helper behavior**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_should_promote_latest_fact_accepts_successful_records tests/test_daily_premarket.py::test_should_promote_latest_fact_rejects_non_blocking_placeholder -q
```

Expected: both tests pass after the short-term helper exists.

- [ ] **Step 3: Keep promotion policy small and documented**

Ensure `_should_promote_latest_fact()` only rejects the non-blocking placeholder shape:

```python
def _should_promote_latest_fact(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    return not (
        payload.get("status") == "skipped"
        and payload.get("reason") == "daily_premarket_non_blocking"
    )
```

- [ ] **Step 4: Verify daily premarket tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -q
```

Expected: all tests pass.

### Task 2: Add Pending Facts Job Artifact

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Test: `tests/test_daily_premarket.py`

- [ ] **Step 1: Write failing test for pending facts job**

Add a test that runs `DailyPremarketRunner.run()` and expects a job file:

```python
def test_daily_runner_writes_pending_facts_job(tmp_path: Path) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=tmp_path / ".venv/bin/python",
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        dry_run=False,
    )
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text("symbol\nMSFT\n", encoding="utf-8")

    result = _daily_runner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        trade_action_generator=FakeTradeActionGenerator(),
        notifier=NullNotifier(),
    ).run("2026-07-06", market="US")

    job_path = tmp_path / "data/runs/2026-07-06/US/facts_job.json"
    payload = json.loads(job_path.read_text(encoding="utf-8"))
    assert result.status == "success"
    assert payload["status"] == "pending"
    assert payload["run_date"] == "2026-07-06"
    assert payload["market"] == "US"
    assert payload["advice_path"].endswith("data/runs/2026-07-06/US/trading_advice.csv")
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_daily_runner_writes_pending_facts_job -q
```

Expected: fail because `facts_job.json` does not exist.

- [ ] **Step 3: Implement job writer**

Add helper in `src/open_trader/daily_premarket.py`:

```python
def _write_pending_facts_job(
    *,
    data_dir: Path,
    run_date: str,
    market: str,
    advice_path: Path,
) -> Path:
    run_dir = data_dir / "runs" / run_date / market
    job_path = run_dir / "facts_job.json"
    payload = {
        "schema_version": "open_trader.facts_job.v1",
        "run_date": run_date,
        "market": market,
        "status": "pending",
        "advice_path": str(advice_path),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(job_path, payload)
    return job_path
```

Call it after `advice_path` is known and before report/status writing:

```python
facts_job_path = _write_pending_facts_job(
    data_dir=config.data_dir,
    run_date=run_date,
    market=market,
    advice_path=advice_path,
)
```

- [ ] **Step 4: Include job path in artifacts**

Add an artifact key:

```python
"facts_job": str(facts_job_path),
```

- [ ] **Step 5: Verify**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_daily_runner_writes_pending_facts_job tests/test_daily_premarket.py -q
```

Expected: all selected tests pass.

### Task 3: Add CLI Command To Process Pending Facts

**Files:**
- Modify: `src/open_trader/cli.py`
- Modify: `src/open_trader/daily_premarket.py`
- Test: `tests/test_premarket_cli.py` or `tests/test_daily_premarket.py`

- [ ] **Step 1: Write failing test for CLI parser**

Add a CLI test that invokes:

```bash
.venv/bin/python -m open_trader process-facts-job --job data/runs/2026-07-06/US/facts_job.json
```

Expected output contains:

```text
facts_job: data/runs/2026-07-06/US/facts_job.json
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py::test_process_facts_job_cli -q
```

Expected: fail because command does not exist.

- [ ] **Step 3: Implement command**

Add parser in `src/open_trader/cli.py`:

```python
process_facts_job_parser = subparsers.add_parser(
    "process-facts-job",
    help="Extract technical and decision facts for a pending daily premarket facts job",
)
process_facts_job_parser.add_argument("--job", type=Path, required=True)
```

Add command handler:

```python
if args.command == "process-facts-job":
    result = process_facts_job(args.job)
    print(f"facts_job: {result.job_path}")
    print(f"decision_facts: {result.decision_records}")
    print(f"technical_facts: {result.technical_records}")
    return 0
```

- [ ] **Step 4: Implement `process_facts_job()`**

In `src/open_trader/daily_premarket.py`, load the job, call existing `generate_decision_facts()` and `generate_technical_facts()` with `update_latest=True`, then update job status to `complete` or `failed`.

- [ ] **Step 5: Verify**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py tests/test_daily_premarket.py -q
```

Expected: all selected tests pass.

### Task 4: Retry Transient LLM Schema Failures

**Files:**
- Modify: `src/open_trader/technical_facts.py`
- Modify: `src/open_trader/decision_facts.py`
- Test: `tests/test_technical_facts.py`
- Test: `tests/test_decision_facts.py`

- [ ] **Step 1: Write failing tests for retry**

Add extractor fake that fails once with invalid schema and succeeds on second call:

```python
class FlakyExtractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, source):
        self.calls += 1
        if self.calls == 1:
            raise ValueError("technical facts schema_version is invalid")
        return valid_technical_facts_payload()
```

Assert generation succeeds with one retry and records retry count.

- [ ] **Step 2: Run RED tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_technical_facts.py::test_generate_technical_facts_retries_schema_failure tests/test_decision_facts.py::test_generate_decision_facts_retries_schema_failure -q
```

Expected: fail because retry is not implemented.

- [ ] **Step 3: Implement bounded retry**

Add `max_attempts=2` inside record build for both generators. Retry only `ValueError` messages from schema/status validation; do not retry missing source.

- [ ] **Step 4: Verify**

Run:

```bash
.venv/bin/python -m pytest tests/test_technical_facts.py tests/test_decision_facts.py -q
```

Expected: all selected tests pass.

### Task 5: Surface Facts Job Status In Dashboard

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Write failing dashboard state test**

Add a test that writes `facts_job.json` with `status: "pending"` and asserts dashboard state includes `facts_job.status == "pending"`.

- [ ] **Step 2: Run RED test**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_attaches_facts_job_status -q
```

Expected: fail because dashboard does not load job status.

- [ ] **Step 3: Load job status in dashboard backend**

Add a small loader for `data/latest/<market>/facts_job.json` or latest run job path, returning:

```python
{
    "status": "pending",
    "run_date": "2026-07-06",
    "market": "US",
    "error": "",
}
```

- [ ] **Step 4: Render non-blocking status**

In `dashboard_static/dashboard.js`, show a small status line near decision cards:

```javascript
facts: "抽取中"
```

Only show it when status is `pending` or `failed`; do not replace existing facts rows with placeholder text.

- [ ] **Step 5: Verify**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: all selected tests pass.

### Task 6: Update Operational Docs

**Files:**
- Modify: `README.zh-CN.md`
- Modify: `README.md`

- [ ] **Step 1: Add exact commands**

Document:

```bash
.venv/bin/python -m open_trader process-facts-job \
  --job data/runs/2026-07-06/US/facts_job.json
```

and the manual fallback:

```bash
.venv/bin/python -m open_trader extract-decision-facts \
  --advice data/latest/US/trading_advice.csv \
  --data-dir data \
  --date 2026-07-06 \
  --market US \
  --update-latest
```

- [ ] **Step 2: Explain latest safety**

State that `daily_premarket_non_blocking` skipped placeholders are diagnostic run artifacts and must not overwrite `data/latest/<market>/*_facts.json`.

- [ ] **Step 3: Verify docs references**

Run:

```bash
rg -n "process-facts-job|daily_premarket_non_blocking|extract-decision-facts" README.md README.zh-CN.md
```

Expected: both READMEs mention the new command and the safety rule.

