# LLM Hedge Annualized Entry Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admit a Polymarket threshold hedge only when its fee-aware simple annualized yield is at least 15%, while keeping lower-yield observations visible in the approved complete bilingual B layout.

**Architecture:** Put the single threshold constant in the existing prediction-arbitrage domain module. The monitor computes yield from the later of the two contract end times and owns visibility/actionability; the execution service repeats the same fail-closed admission check on fresh server data. Reuse the existing signal store, cached asynchronous title translator, Dashboard history endpoint, and existing table rather than adding a service or schema.

**Tech Stack:** Python 3, `Decimal`, asyncio, SQLite JSON payloads, pytest, vanilla JavaScript/CSS, Playwright, launchd.

## Global Constraints

- `MIN_THRESHOLD_ANNUALIZED_YIELD = Decimal("0.15")`; equality passes.
- Apply the gate only to `market_type="threshold_hedge"`.
- Keep positive below-floor observations in current, 7-day, and 30-day distributions.
- Missing, malformed, non-finite, or non-future duration data fails closed.
- Do not add a Treasury feed, settlement buffer, early-exit model, venue, dependency, panel, or translation service.
- English is the complete primary target; cached Chinese is the complete smaller second line; neither may truncate.
- Profit copy says the modeled maximum trading fee is included and does not claim every external cost is deducted.
- Do not send a live Feishu test notification.
- Run `make acceptance` only once, after all focused checks and source changes are complete.

---

### Task 1: Enforce one fee-aware annualized gate end to end

**Files:**
- Modify: `src/open_trader/prediction_arbitrage.py`
- Modify: `src/open_trader/polymarket_monitor.py`
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Test: `tests/test_polymarket_monitor.py`
- Test: `tests/test_prediction_arbitrage_execution.py`

**Interfaces:**
- Produces: `MIN_THRESHOLD_ANNUALIZED_YIELD: Decimal` in `open_trader.prediction_arbitrage`.
- Consumes: existing `simple_annualized_yield(intent, *, now, resolution_at) -> Decimal | None`.
- Produces: `annualized_yield`, `remaining_days`, `resolution_at`, and `eligibility_reason` on existing threshold opportunity rows.

- [ ] **Step 1: Write monitor tests for later-end duration, below-floor visibility, and the inclusive floor**

Add focused tests beside the existing threshold annualized-distribution tests. Use the existing `threshold_event`, `setup_threshold_books`, `FakeRelationValidator`, and `make_monitor` helpers. Mutate both fixture end dates so the test proves the later one owns the duration:

```python
def test_threshold_annualized_gate_uses_later_end_and_keeps_low_yield_visible(
    tmp_path: Path,
) -> None:
    source = threshold_event()
    source.markets[0].end_date = "2026-09-01T00:00:00Z"
    source.markets[1].end_date = "2027-09-01T00:00:00Z"
    setup_public([source])
    setup_threshold_books()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=FakeRelationValidator(),
    )

    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    monitor.refresh_once()

    row = next(
        item for item in monitor.snapshot()["opportunities"]
        if item.get("market_type") == "threshold_hedge"
    )
    assert row["resolution_at"] == "2027-09-01T00:00:00Z"
    assert row["annualized_yield"] < Decimal("0.15")
    assert row["actionable"] is False
    assert row["eligibility_reason"] == "annualized_yield_below_minimum"
    history = monitor._store.signal_history("all")
    assert history[0]["annualized_yield"] == row["annualized_yield"]
    distributions = monitor.snapshot()["relation_discovery"]["annualized_distribution"]
    assert distributions["current"]["count"] == 1
    assert distributions["7d"]["count"] == 1
    assert distributions["30d"]["count"] == 1
```

Register a ready observer in this case and assert its call list remains empty. Add a second case with one invalid end date and assert `annualized_yield is None`, `actionable is False`, and `eligibility_reason == "annualized_yield_unavailable"`. Keep the existing actionable fixture as the proof that a value above 15% continues through later checks.

- [ ] **Step 2: Run the monitor tests and verify the new assertions fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  -k 'threshold_annualized_gate or full_relation_scan_recovers_threshold_event' -q
```

Expected: the later-end and annualized admission assertions fail because the monitor currently uses market B directly and does not enforce 15%.

- [ ] **Step 3: Add the single constant and the minimal monitor gate**

In `prediction_arbitrage.py`, place the constant beside the existing threshold economics limits:

```python
MIN_THRESHOLD_ANNUALIZED_YIELD = Decimal("0.15")
```

Import it into `polymarket_monitor.py`. Replace the direct market-B end date with a fail-closed later-date selection:

```python
end_a = _timestamp_or_none(relation.market_a.end_date)
end_b = _timestamp_or_none(relation.market_b.end_date)
end_date = max(end_a, end_b) if end_a is not None and end_b is not None else None
resolution_at = (
    relation.market_a.end_date
    if end_date is not None
    and end_date == end_a
    else relation.market_b.end_date if end_date is not None else None
)
```

Keep using `simple_annualized_yield`. After all existing LLM, unwind, freshness, readiness, and funds checks, add only the annualized blockers:

```python
elif status == "approved" and annualized is None:
    eligibility_reason = "annualized_yield_unavailable"
elif status == "approved" and annualized < MIN_THRESHOLD_ANNUALIZED_YIELD:
    eligibility_reason = "annualized_yield_below_minimum"
```

Pass `resolution_at` into `_relation_row`, widen that keyword type to `str | None`, and persist `resolution_at`, `remaining_days`, `maximum_fee`, and `eligibility_reason` in `_upsert_signal`. Leave `_schedule_ready_notification` unchanged because it already requires `actionable is True`.

- [ ] **Step 4: Run the monitor tests and verify they pass**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  -k 'threshold_annualized_gate or full_relation_scan_recovers_threshold_event' -q
```

Expected: PASS, including the pre-existing actionable threshold case and distribution case.

- [ ] **Step 5: Write shared execution-admission tests for unavailable, non-finite, below-floor, and exact-floor values**

Add `annualized_yield = Decimal("0.20")` to `ThresholdMonitor`, return it from `opportunity()`, and add this parameterized test beside the existing threshold preview tests:

```python
@pytest.mark.parametrize(
    ("value", "expected_state", "expected_reason"),
    [
        (None, "rejected", "annualized_yield_unavailable"),
        ("NaN", "rejected", "annualized_yield_unavailable"),
        ("Infinity", "rejected", "annualized_yield_unavailable"),
        (Decimal("0.149999"), "rejected", "annualized_yield_below_minimum"),
        (Decimal("0.15"), "previewed", None),
    ],
)
def test_threshold_preview_enforces_annualized_floor(
    tmp_path: Path,
    value: object,
    expected_state: str,
    expected_reason: str | None,
) -> None:
    service, _, _, monitor = threshold_execution_fixture(tmp_path)
    monitor.annualized_yield = value

    result = service.preview("threshold-opp-1")

    assert result["state"] == expected_state
    if expected_reason is not None:
        assert result["reason"] == expected_reason
```

Extend `test_ready_notification_fails_closed_when_any_preflight_check_fails` with `"annualized"`, set the fixture yield below 15%, and retain the existing assertions proving no notifier or submit call occurs.

- [ ] **Step 6: Run the execution tests and verify they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage_execution.py \
  -k 'annualized_floor or ready_notification_fails_closed' -q
```

Expected: unavailable and below-floor values incorrectly reach preview/notification before implementation.

- [ ] **Step 7: Add the execution-service fail-closed guard**

Import `MIN_THRESHOLD_ANNUALIZED_YIELD` from `prediction_arbitrage`. In the existing `ThresholdHedgeIntent` branch of `_validate_opportunity`, before returning `None`, add:

```python
annualized = _decimal(opportunity.get("annualized_yield"))
if annualized is None:
    return "annualized_yield_unavailable"
if annualized < MIN_THRESHOLD_ANNUALIZED_YIELD:
    return "annualized_yield_below_minimum"
```

This one shared method covers preview, notification preparation, final notification refresh, and final execution validation. Do not duplicate the comparison in those callers.

- [ ] **Step 8: Run backend focused tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_execution.py -q
```

Expected: PASS.

Commit:

```bash
git add src/open_trader/prediction_arbitrage.py \
  src/open_trader/polymarket_monitor.py \
  src/open_trader/prediction_arbitrage_execution.py \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_execution.py
git commit -m "feat: gate threshold hedges by annualized yield"
```

---

### Task 2: Project complete bilingual targets into compact signal rows

**Files:**
- Modify: `src/open_trader/polymarket_monitor.py`
- Modify: `src/open_trader/dashboard_web.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_polymarket_monitor.py`
- Test: `tests/test_dashboard_web.py`
- Modify: `tests/e2e/serve_dashboard_fixture.py`
- Test: `tests/e2e/prediction-market.spec.ts`

**Interfaces:**
- Consumes: the existing `CodexTitleTranslator`, `cached_prediction_title_zh`, and `prediction_title_cache_key` pipeline.
- Consumes: stored `question`, `resolution_at`, `remaining_days`, `total_max_cost`, `minimum_profit`, `annualized_yield`, and `eligibility_reason`.
- Produces: the existing history API fields plus `event_title_zh`, `actionable_now`, and current threshold economics.

- [ ] **Step 1: Write translation and history-projection tests**

Extend the FIFO translation worker test with one threshold relation and assert the combined displayed question is queued after event titles without parallel translation:

```python
pair = (
    "Will Bitcoin be above $90,000 on December 31? / "
    "Will Bitcoin be above $100,000 on December 31?"
)
assert pair in translator.calls
assert translator.max_active == 1
```

In `test_prediction_history_projects_live_yes_no_actionability_and_cached_title`, add a threshold row/current opportunity case with the threshold-specific required fields and cached pair translation. Assert:

```python
assert threshold_item["event_title"] == pair
assert threshold_item["event_title_zh"] == "比特币在 12 月 31 日是否高于 9 万美元？ / 比特币在 12 月 31 日是否高于 10 万美元？"
assert threshold_item["actionable_now"] is True
assert threshold_item["annualized_yield"] == "0.20"
assert threshold_item["remaining_days"] == "47"
```

- [ ] **Step 2: Run the projection tests and verify they fail**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  tests/test_dashboard_web.py \
  -k 'title_translation_worker or history_projects_live' -q
```

Expected: the pair is not queued and threshold rows do not receive live actionability or pair translation.

- [ ] **Step 3: Reuse the existing translation queue for the exact pair title**

When `_relation_row` creates the combined `question`, read `_cached_title_zh(row["question"])`; set `title_zh` and `event_title_zh` only for a valid cached result. Call the existing `_enqueue_title_translations` with a single mapping shaped like `{"title": row["question"]}` before persisting the row. Do not create a second queue, worker, model, prompt, or cache.

In `dashboard_web.py`, make `_prediction_history_payload` copy current threshold display fields onto an open row when current state is usable:

```python
for name in (
    "annualized_yield",
    "remaining_days",
    "resolution_at",
    "total_max_cost",
    "maximum_fee",
    "eligibility_reason",
):
    if current.get(name) not in (None, ""):
        projected[name] = current[name]
```

Split `_complete` by `market_type`: keep the current standard-binary required fields unchanged, and require threshold identity, two conditions/tokens, economics, annualized yield, and freshness for a threshold row. Continue using `_prediction_attach_cached_title` so the Dashboard request never invokes Codex.

- [ ] **Step 4: Run the projection tests and verify they pass**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_polymarket_monitor.py \
  tests/test_dashboard_web.py \
  -k 'title_translation_worker or history_projects_live' -q
```

Expected: PASS.

- [ ] **Step 5: Replace the seven-column signal markup test with the approved B contract**

Update `test_prediction_yes_no_signal_renderer_has_approved_columns_and_fail_closed_actions` to feed one threshold row with a long English pair and cached Chinese pair. Assert these five headers:

```python
for label in ("出现时间（HKT）", "标的", "资金占用", "净回报", "操作"):
    assert label in html
```

Also assert:

```python
assert html.index(english_pair) < html.index(chinese_pair)
assert "152.6 天" in html
assert "年化 0.53%" in html
assert "含模型手续费" in html
assert "仅观察" in html
assert "年化低于 15% 入场门槛" in html
```

Retain the existing fail-closed action assertions for stale/error state. Add static CSS assertions that `.pm-title-en` is the stronger line, `.pm-title-zh` is smaller/muted, and none of `text-overflow: ellipsis`, `line-clamp`, or `-webkit-line-clamp` appears in the signal-title rules.

- [ ] **Step 6: Run the Dashboard renderer test and verify it fails**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_dashboard_web.py \
  -k 'signal_renderer_has_approved_columns or prediction_workspace_exists' -q
```

Expected: old columns, Chinese-first ordering, and missing annualized/capital-duration copy fail.

- [ ] **Step 7: Implement the compact B renderer with native wrapping**

In `dashboard.js`:

- render English first as `<span class="pm-title-en">` and cached Chinese second as `<span class="pm-title-zh">`;
- render `中文翻译生成中` in the second-line class when no cached translation is present;
- replace the signal table headers with the five approved columns;
- for threshold rows, render remaining days plus HKT resolution date in `资金占用`;
- render theoretical minimum profit, `年化 N%`, total maximum cost, and `含模型手续费` in `净回报`;
- map `annualized_yield_below_minimum` to `年化低于 15% 入场门槛` and `annualized_yield_unavailable` to `年化无法计算，禁止入场` in `predictionReasonLabel`;
- show `仅观察` and the reason when blocked; keep the existing `重新检查` button only when `actionable_now` and runtime readiness both pass.

For standard same-condition YES/NO rows, use the same five containers without changing meaning: `资金占用` keeps the observed signal duration, `净回报` keeps trigger/live profit, and `操作` keeps notification state plus the existing guarded recheck button. Do not invent a resolution date or annualized value for a mergeable standard pair.

In `dashboard.css`, keep the existing warm-light tokens and table. Set the English line to the normal table font and stronger weight; set Chinese to `var(--muted)` and `12px`. Use only `overflow-wrap: anywhere` and natural block flow. Update the five column widths and preserve the existing mobile table-to-card layout and 44px action target.

- [ ] **Step 8: Update the existing Playwright flow for complete desktop/mobile bilingual text**

In `tests/e2e/serve_dashboard_fixture.py`, add the approved threshold row fields and complete bilingual pair. In `tests/e2e/prediction-market.spec.ts`, update the signal-table header expectations and add assertions that both full target strings are visible in the desktop and mobile passes. Use the existing fixture server and page flow; do not add screenshots.

- [ ] **Step 9: Run focused UI tests and commit**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_dashboard_web.py \
  tests/test_polymarket_monitor.py -q
OPEN_TRADER_PYTHON="$PWD/.venv/bin/python" \
  npm exec playwright test tests/e2e/prediction-market.spec.ts --project=chromium
```

Expected: both commands PASS.

Commit:

```bash
git add src/open_trader/polymarket_monitor.py \
  src/open_trader/dashboard_web.py \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_polymarket_monitor.py \
  tests/test_dashboard_web.py \
  tests/e2e/serve_dashboard_fixture.py \
  tests/e2e/prediction-market.spec.ts
git commit -m "feat: show annualized threshold signals in bilingual layout"
```

---

### Task 3: Final gate, changelog, and exact-SHA review deployment

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: completed backend and Dashboard changes from Tasks 1 and 2.
- Produces: one accepted and deployed review SHA with runtime evidence.

- [ ] **Step 1: Add the dated operator-facing changelog entry and commit it**

Under `2026-08-01`, record that LLM threshold hedges below 15% stay observable but cannot notify or preview, duration uses the later contract end date, and the signal table now shows complete English above complete cached Chinese with fee-aware annualized return.

Commit:

```bash
git add CHANGELOG.md
git commit -m "docs: log annualized threshold entry gate"
```

- [ ] **Step 2: Run final source checks before acceptance**

Run:

```bash
git diff main...HEAD --check
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_prediction_arbitrage.py \
  tests/test_polymarket_monitor.py \
  tests/test_prediction_arbitrage_execution.py \
  tests/test_dashboard_web.py -q
```

Expected: clean diff check and all focused tests PASS.

- [ ] **Step 3: Run the repository acceptance gate once**

Run from the feature worktree:

```bash
make acceptance
```

Expected: final line `PASS`. Treat `FAIL` as unfinished and fix it before rerunning; report `BLOCKED` without substituting mocks or curl.

- [ ] **Step 4: Redeploy the exact accepted SHA**

Capture the accepted SHA, reinstall/restart the Dashboard launchd services from this worktree, and verify the new listener:

```bash
accepted_sha="$(git rev-parse HEAD)"
bash scripts/install_dashboard_launchd.sh
lsof -nP -iTCP:8766 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8766/healthz
git rev-parse HEAD
```

Confirm the launchd process PID, working directory, health payload Git SHA, source state, and fresh log timestamp all match `accepted_sha`. This restart deploys the already-accepted SHA and does not require a second acceptance run.

- [ ] **Step 5: Hand off the live review URL with evidence**

Provide `http://127.0.0.1:8766/`, the exact focused-test counts, acceptance `PASS`, deployed SHA, PID, working directory, and fresh log timestamp. Do not claim completion if any runtime field points to an older checkout or SHA.
