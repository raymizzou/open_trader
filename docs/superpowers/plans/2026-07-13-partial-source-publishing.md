# Partial Source Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish every generated current data source even when another source fails, keep failures red and actionable, and reuse the existing market-and-source extractor commands for independent retry.

**Architecture:** Keep the existing synchronous HK/US daily jobs and existing JSON artifacts. Isolate failures at the four source-generator boundaries, evaluate all eight canonical source names, and promote every artifact that was actually generated; a missing artifact retains its old latest file, which existing date/hash checks mark unavailable. No scheduler, queue, per-symbol retry, or new dependency is added.

**Tech Stack:** Python 3.12, pytest, existing Open Trader CLI and Dashboard.

## Global Constraints

- The current advice rows in `data/latest/<MARKET>/trading_advice.csv` define the strict symbol scope.
- The canonical source names are `tradingagents_summary`, `technical_facts`, `decision_facts.kline`, `decision_facts.news_sentiment`, `futu_skill_facts.news_sentiment`, `futu_skill_facts.technical_anomaly`, `futu_skill_facts.capital_anomaly`, and `futu_skill_facts.derivatives_anomaly`.
- Every generated artifact is promoted even if it contains failed records; successful records remain visible and failed records remain red with their real error.
- If a generator produces no artifact, retain that source's prior latest file; stale date/hash checks must keep it unavailable.
- Daily status remains `failed` and `blocked` while any required source is unavailable.
- Independent retry granularity is market plus data source, using the four existing `extract-*` commands with `--update-latest`.
- Do not add a second service, queue, scheduler, dependency, or per-symbol filtering.
- `make acceptance` must return `PASS` before user review.

---

### Task 1: Publish successful sources and provide independent retry commands

**Files:**
- Modify: `src/open_trader/advice/premarket.py:51-285`
- Modify: `src/open_trader/daily_premarket.py:640-840`
- Modify: `src/open_trader/daily_premarket.py:1250-1620`
- Modify: `src/open_trader/decision_source_availability.py:95-165`
- Test: `tests/test_premarket_pipeline.py`
- Test: `tests/test_daily_premarket.py`
- Test: `tests/test_decision_source_availability.py`

**Interfaces:**
- Consumes: existing `generate_technical_facts`, `generate_decision_facts`, `generate_tradingagents_summary`, `generate_futu_skill_facts`, `_promote_latest_set`, and four existing `extract-* --update-latest` CLI commands.
- Produces: canonical `SourceFailure.source` values; optional `PremarketResult.technical_facts_error` and `decision_facts_error` strings; `_source_retry_command(source: str, market: str, run_date: str) -> str`.

- [ ] **Step 1: Write failing canonical-name and retry-command tests**

Change the evaluator expectation to the exact contract:

```python
assert [failure.source for failure in failures] == [
    "tradingagents_summary",
    "technical_facts",
    "decision_facts.kline",
    "decision_facts.news_sentiment",
    "futu_skill_facts.news_sentiment",
    "futu_skill_facts.technical_anomaly",
    "futu_skill_facts.capital_anomaly",
    "futu_skill_facts.derivatives_anomaly",
]
```

Add a daily notification test that requires one deduplicated source-level retry command for a decision failure:

```python
assert (
    ".venv/bin/python -m open_trader extract-decision-facts "
    "--advice data/latest/US/trading_advice.csv --data-dir data "
    "--date 2026-06-19 --market US --update-latest"
) in notifier.messages[-1][1]
```

Add equivalent parameterized expectations for the technical, TradingAgents-summary, and Futu command families. Two failed modules from the same artifact must render the command once.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_decision_source_availability.py tests/test_daily_premarket.py -k 'source_names or retry_command'
```

Expected: failures show the old non-canonical names and missing extractor commands.

- [ ] **Step 3: Use canonical source names and map them to existing CLIs**

In `decision_source_availability.py`, use these names verbatim:

```python
"tradingagents_summary"
"technical_facts"
"decision_facts.kline"
"decision_facts.news_sentiment"
f"futu_skill_facts.{name}"
```

In `daily_premarket.py`, add one direct mapping helper:

```python
def _source_retry_command(source: str, market: str, run_date: str) -> str:
    latest = f"data/latest/{market}"
    if source == "technical_facts":
        return (
            ".venv/bin/python -m open_trader extract-technical-facts "
            f"--advice {latest}/trading_advice.csv --data-dir data "
            f"--date {run_date} --market {market} --update-latest"
        )
    if source.startswith("decision_facts."):
        return (
            ".venv/bin/python -m open_trader extract-decision-facts "
            f"--advice {latest}/trading_advice.csv --data-dir data "
            f"--date {run_date} --market {market} --update-latest"
        )
    if source == "tradingagents_summary":
        return (
            ".venv/bin/python -m open_trader extract-tradingagents-summary "
            f"--advice {latest}/trading_advice.csv --plan {latest}/trading_plan.csv "
            f"--actions {latest}/trade_actions.csv --data-dir data "
            f"--date {run_date} --market {market} --update-latest"
        )
    return (
        ".venv/bin/python -m open_trader extract-futu-skill-facts "
        "--portfolio data/latest/portfolio.csv --data-dir data "
        f"--date {run_date} --market {market} --update-latest"
    )
```

Render commands for the distinct failed artifact families in `_blocker_notification_message`. Retain the existing `launchctl kickstart` command as the full-run fallback.

- [ ] **Step 4: Write failing partial-publication tests**

Add a daily test where `decision_facts.kline` is unavailable but technical facts and Futu facts are valid. Seed each latest file with `old`, run the daily runner, and assert:

```python
assert result.status == "failed"
assert latest_technical.read_text() == run_technical.read_text()
assert latest_decision.read_text() == run_decision.read_text()
assert latest_futu.read_text() == run_futu.read_text()
assert "stale-old-marker" not in latest_technical.read_text()
```

This intentionally proves the failed decision artifact is also published so its failed record is visible rather than replaced by stale success.

Add a summary-generator exception case:

```python
assert result.status == "failed"
assert latest_summary.read_text() == old_summary
assert latest_technical.read_text() == run_technical.read_text()
assert latest_decision.read_text() == run_decision.read_text()
assert latest_futu.read_text() == run_futu.read_text()
assert status["source_failures"][0]["error"] == "summary service unavailable"
```

- [ ] **Step 5: Run the publication tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_daily_premarket.py -k 'publishes_successful_sources or retains_only_missing_source_latest'
```

Expected: generated artifacts remain unpromoted because the current code gates `_promote_latest_set` on an empty failure list, and the summary exception detail is not propagated.

- [ ] **Step 6: Isolate source-generator exceptions**

Extend `PremarketResult` with backward-compatible defaults:

```python
technical_facts_error: str = ""
decision_facts_error: str = ""
```

In `run_premarket`, call the technical and decision generators in separate `try` blocks and return each available path and error:

```python
technical_facts_result: TechnicalFactsResult | None = None
technical_facts_error = ""
try:
    technical_facts_result = _generate_technical_facts_after_advice(
        advice_path=advice_path,
        data_dir=data_dir,
        run_date=run_date,
        update_latest=False,
        market=market_scope,
        technical_facts_generator=technical_facts_generator,
    )
except Exception as exc:
    technical_facts_error = str(exc) or exc.__class__.__name__

decision_facts_result: DecisionFactsResult | None = None
decision_facts_error = ""
try:
    decision_facts_result = _generate_decision_facts_after_advice(
        advice_path=advice_path,
        data_dir=data_dir,
        run_date=run_date,
        update_latest=False,
        market=market_scope,
        decision_facts_generator=decision_facts_generator,
    )
except Exception as exc:
    decision_facts_error = str(exc) or exc.__class__.__name__
```

Set result fields with `technical_facts_result.run_path if technical_facts_result else None` and the equivalent decision expression. Make `_promote_latest_outputs` accept optional fact paths and append each fact promotion only when its path is not `None`, matching the existing daily promotion behavior:

```python
if technical_facts_path is not None:
    promotions.append(
        _LatestPromotion(
            source_path=technical_facts_path,
            latest_path=latest_dir / "technical_facts.json",
        )
    )
if decision_facts_path is not None:
    promotions.append(
        _LatestPromotion(
            source_path=decision_facts_path,
            latest_path=latest_dir / "decision_facts.json",
        )
    )
```

In `DailyPremarketRunner._run_locked`, catch Futu generator exceptions exactly as the summary generator is already caught:

```python
futu_skill_facts_path: Path | None = None
futu_facts_error = ""
try:
    futu_facts_result = self.futu_facts_generator(
        portfolio_path=portfolio_path,
        data_dir=config.data_dir,
        run_date=run_date,
        market=market,
        extractor=self.futu_facts_extractor_factory(),
        update_latest=False,
    )
except Exception as exc:
    futu_facts_error = str(exc) or exc.__class__.__name__
    LOGGER.warning("Futu skill facts generation failed", exc_info=True)
else:
    futu_skill_facts_path = Path(futu_facts_result.run_path)
```

Store the actual summary exception string instead of only a Boolean. Pass the technical, decision, Futu, and summary errors to `_evaluate_source_failures`; when the corresponding record/path is missing, replace `"数据未生成"` with that captured error for every affected canonical source.

- [ ] **Step 7: Promote every artifact that exists**

Remove only the completeness condition:

```python
if not dry_run:
    _promote_latest_set(
        advice_path=advice_path,
        actions_path=actions_path,
        plan_path=plan_result.plan_path,
        trade_actions_path=trade_actions_result.actions_path,
        technical_facts_path=technical_facts_path,
        decision_facts_path=decision_facts_path,
        tradingagents_summary_path=tradingagents_summary_path,
        futu_skill_facts_path=futu_skill_facts_path,
        data_dir=config.data_dir,
        market=market,
    )
```

Do not merge old and new records. `_promote_latest_set` already skips optional paths that have no new artifact and transactionally publishes every path it receives.

- [ ] **Step 8: Run focused and full affected suites**

Run:

```bash
.venv/bin/pytest -q tests/test_premarket_pipeline.py tests/test_decision_source_availability.py tests/test_daily_premarket.py tests/test_dashboard.py
```

Expected: all selected tests pass with canonical names, real errors, partial publication, and source-level retry commands.

Run:

```bash
make test
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add src/open_trader/advice/premarket.py src/open_trader/daily_premarket.py src/open_trader/decision_source_availability.py tests/test_premarket_pipeline.py tests/test_daily_premarket.py tests/test_decision_source_availability.py
git commit -m "fix: publish successful decision sources"
```

- [ ] **Step 10: Run the Dashboard gate after restarting committed code**

Restart the Dashboard screen from the feature worktree using canonical live data/report paths, verify its PID/SHA/log, then run:

```bash
make acceptance
```

Expected: `"status": "PASS"`, no errors, no blocker. A `FAIL` must be fixed and rerun; a `BLOCKED` must be reported as blocked.
