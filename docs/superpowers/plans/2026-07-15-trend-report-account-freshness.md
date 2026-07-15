# Trend Report Account Freshness Decoupling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep valid trend BUY actions visible when the latest structurally valid account snapshot is non-real-time, and size all US/HK/CN trend entries at the approved 4% fallback weight until the separate per-market Kelly project is implemented.

**Architecture:** Keep account freshness as presentation/audit metadata only. Pass an explicit position weight into the existing report builder, record that weight in report metadata, and let the existing candidate/lot/cash/slot rules remain the sole BUY filters. Make Feishu, Dashboard projection, and acceptance projection derive actions identically from the frozen report, while showing one non-blocking warning for a non-real-time account.

**Tech Stack:** Python 3.12, `Decimal`, pytest, existing Open Trader CLI/report artifacts, Dashboard HTTP server, Playwright-based `make acceptance`.

## Global Constraints

- Preserve the user's unrelated untracked files:
  - `docs/superpowers/plans/2026-07-13-systematic-trading-plan-core.md`
  - `docs/superpowers/specs/2026-07-13-systematic-trading-plan-design.md`
  - `src/open_trader/dashboard_static/trend-report-prototype.html`
- Do not add Kelly sample collection or Kelly calculation. `fallback_4pct` is the only sizing source in this change.
- Do not change candidate eligibility, sell/hold rules, lot-size rules, cash caps, ten-position cap, unknown-action review routing, routes, dependencies, or persisted schema.
- A missing or malformed account snapshot must still fail through the existing validation path; only a valid snapshot whose `fresh` field is not exactly `true` becomes non-blocking.
- Use the exact warning text `账户数据非实时，执行前核对现金与持仓` in Feishu and Dashboard.
- Do not mutate `reports/trend_hk_phillips/2026-07-14-r1.*`. A current-day correction must be a new revision.
- Do not send or backfill a second Feishu notification for the corrected report.
- Use `apply_patch` for source and test edits.
- Run `make acceptance` as the final gate. Only `PASS` permits completion language.

---

### Task 1: Make BUY generation freshness-independent and parameterize the 4% weight

**Files:**

- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_market_trend.py`
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/market_trend.py`

- [ ] **Step 1: Replace the old 1% and stale-account expectations with failing 4% contract tests**

In `tests/test_a_share_trend.py`, update direct calls to `estimate_buy_actions` so they pass `position_weight=Decimal("0.04")`, remove `account_fresh` and `require_fresh_account`, and replace the stale-suppression test with this contract:

```python
def test_buy_actions_use_four_percent_even_when_account_is_stale() -> None:
    actions = estimate_buy_actions(
        ranked=[candidate("600001")],
        net_value=Decimal("100000"),
        available_cash=Decimal("10000"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
    )

    assert [
        (item.symbol, item.target_amount, item.estimated_shares)
        for item in actions
    ] == [("600001", Decimal("4000.00"), 400)]
```

Retain the existing cash-cap, slot-cap, unaffordable-candidate, US whole-share, and HK board-lot tests, but update their expected target amounts/shares for 4%. In particular, add the real HK regression values:

```python
def test_hk_four_percent_weight_can_buy_one_board_lot() -> None:
    hk = replace(
        candidate("600002", close="127.6"),
        symbol="06821",
        exchange="HK",
    )

    actions = estimate_buy_actions(
        ranked=[hk],
        net_value=Decimal("628554.06"),
        available_cash=Decimal("55053.79"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
        market="HK",
        lot_sizes={"06821": 100},
    )

    assert len(actions) == 1
    assert actions[0].target_amount == Decimal("25142.16")
    assert actions[0].estimated_shares == 100
```

Add a report metadata assertion to an existing `build_report` test:

```python
assert built.metadata["position_weight"] == "0.04"
assert built.metadata["position_weight_source"] == "fallback_4pct"
```

In `tests/test_market_trend.py`, rename `test_hk_report_suppresses_buys_when_statement_is_stale` to `test_hk_report_keeps_buys_when_statement_is_stale`. Change its expectations to:

```python
assert "今日动作：卖出 0｜买入 1｜持有 1｜复核 0" in message
assert "账户数据非实时，执行前核对现金与持仓" in message
assert "\n买入\n" in message
assert actions[0]["action"] == "BUY"
assert actions[0]["symbol"] == "02800"
assert actions[0]["target_amount"] == "4000.00"
assert actions[0]["estimated_shares"] == 400
assert payload["account"]["fresh"] is False
assert payload["metadata"]["position_weight"] == "0.04"
assert payload["metadata"]["position_weight_source"] == "fallback_4pct"
assert payload["protection_state"]["managed_symbols"] == ["00700", "02800"]
```

- [ ] **Step 2: Run the focused tests and confirm they fail for the intended reasons**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py -x
```

Expected before implementation: failure because `estimate_buy_actions` does not accept `position_weight`, still requires `account_fresh`, uses 1%, and the HK integration report has no BUY.

- [ ] **Step 3: Implement the minimal generator interface**

In `src/open_trader/a_share_trend.py`, change the signature and target calculation to:

```python
def estimate_buy_actions(
    *,
    ranked: Sequence[CandidateInput],
    net_value: Decimal,
    available_cash: Decimal,
    current_position_count: int,
    position_weight: Decimal,
    market: str = "CN",
    lot_sizes: Mapping[str, int] | None = None,
) -> list[BuyAction]:
    slots = max(0, 10 - current_position_count)
    if slots == 0:
        return []
    target = (net_value * position_weight).quantize(Decimal("0.01"))
```

Delete `account_fresh` and `require_fresh_account` from this function. Do not add a replacement freshness check.

Change `build_report` to accept:

```python
position_weight: Decimal = Decimal("0.04"),
position_weight_source: str = "fallback_4pct",
```

Remove `require_fresh_account`, call `estimate_buy_actions` with `position_weight`, and merge the actual sizing inputs into metadata at the single return boundary:

```python
metadata={
    **dict(metadata or {}),
    "position_weight": str(position_weight),
    "position_weight_source": position_weight_source,
},
```

In both daily entry points, pass the approved values explicitly:

```python
position_weight=Decimal("0.04"),
position_weight_source="fallback_4pct",
```

The entry points are the A-share `build_report` call in `src/open_trader/a_share_trend.py` and the US/HK `build_report` call in `src/open_trader/market_trend.py`. Remove `require_fresh_account=True` from the latter.

- [ ] **Step 4: Run the core tests and confirm green**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py
```

Expected: all tests in both files pass, including stale HK account + 4% board-lot coverage.

- [ ] **Step 5: Commit the core behavior**

Run:

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "fix: decouple trend buys from account freshness"
```

---

### Task 2: Keep Feishu, Dashboard, and acceptance projections identical

**Files:**

- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `tests/test_dashboard_web.py`
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_acceptance.py`

- [ ] **Step 1: Write failing presentation and trust-boundary tests**

In `tests/test_a_share_trend.py`, rename `test_trend_feishu_text_never_lists_buy_without_explicit_fresh_account` to `test_trend_feishu_text_keeps_buy_for_non_realtime_account`. Keep the existing parameterization over `False`, missing, `None`, and a non-boolean value, but assert:

```python
assert "账户状态：账户数据非实时，执行前核对现金与持仓" in message
assert "今日动作：卖出 0｜买入 1｜持有 0｜复核 0" in message
assert "\n买入\n" in message
assert "02800 盈富基金" in message
assert "禁止买入" not in message
```

Update `test_trend_feishu_text_uses_short_no_trade_template` so the non-real-time status line is exactly:

```text
账户状态：账户数据非实时，执行前核对现金与持仓
```

In `tests/test_dashboard.py`, rename `test_dashboard_trend_report_never_projects_unconfirmed_account_buy_as_actionable` to `test_dashboard_trend_report_keeps_buy_for_non_realtime_account`. For every non-true freshness representation, assert:

```python
assert report["account_fresh"] is False
assert report["account_status"] == "账户数据非实时，执行前核对现金与持仓"
assert report["buy_actions"] == [stale_buy]
assert report["review_actions"] == []
assert report["counts"]["buy"] == 1
assert report["counts"]["review"] == 0
```

Update the multi-broker projection test so Phillips keeps the BUY and reports the same warning. Retain all malformed-report and unknown-action review tests unchanged.

In `tests/test_dashboard_acceptance.py`, invert `test_acceptance_rejects_actionable_buy_without_explicit_fresh_account`: rename it to `test_acceptance_accepts_actionable_buy_for_non_realtime_account`, remove `pytest.raises`, and call `_check_trend_artifact_projection(...)` directly. The same frozen BUY and projected BUY must now match for all four non-true freshness representations.

In `tests/test_dashboard_web.py`, add the non-real-time warning to the trend report fixture and assert the rendered workspace contains the exact warning while retaining the BUY row. No JavaScript behavior change should be necessary.

- [ ] **Step 2: Run the focused tests and confirm the stale BUY tests fail**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py \
  tests/test_dashboard.py \
  tests/test_dashboard_acceptance.py \
  tests/test_dashboard_web.py -x
```

Expected before implementation: stale BUY is moved to review or removed, and the old `已过期，禁止买入`/`已过期` copy is still emitted.

- [ ] **Step 3: Implement one shared non-blocking warning and remove freshness action gates**

In `src/open_trader/a_share_trend.py`, define beside the existing report text constants:

```python
NON_REALTIME_ACCOUNT_WARNING = "账户数据非实时，执行前核对现金与持仓"
```

In `render_trend_feishu_text`:

- Keep `fresh = account.get("fresh") is True` only for the status line.
- Select valid BUY items without `and fresh`.
- Add items to review only when `_trend_action_needs_review(item)` is true.
- Set status to `"已更新" if fresh else NON_REALTIME_ACCOUNT_WARNING`.
- Remove all `已过期` and `禁止买入` branches.

In `src/open_trader/dashboard.py`, import `NON_REALTIME_ACCOUNT_WARNING`, then:

- Select valid BUY actions without `and account_fresh`.
- Do not route BUY to review because freshness is false.
- Set `account_status` to `"已更新" if account_fresh else NON_REALTIME_ACCOUNT_WARNING`.
- Keep the `account_fresh` boolean in the API for audit/UI styling.

In `src/open_trader/dashboard_acceptance.py`, derive expected BUY and review collections from action/reason validity only. Do not reuse the Dashboard projection helper; this remains an independent frozen-artifact check. Remove `buy_allowed` and both freshness conditions.

- [ ] **Step 4: Run all presentation and acceptance-unit tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_dashboard.py \
  tests/test_dashboard_acceptance.py \
  tests/test_dashboard_web.py
```

Expected: all selected tests pass; unknown actions/reasons still route to review; non-real-time BUY remains actionable in the report; no test expects `禁止买入`.

- [ ] **Step 5: Search for obsolete behavior and commit the projection change**

Run:

```bash
rg -n "已过期，禁止买入|require_fresh_account|account_fresh=.*estimate_buy_actions|and account_fresh|and fresh" \
  src/open_trader tests
```

Expected: no freshness-based BUY gate remains. Unrelated uses of freshness outside trend-report action projection may remain and must not be changed.

Then commit:

```bash
git add src/open_trader/a_share_trend.py src/open_trader/dashboard.py \
  src/open_trader/dashboard_acceptance.py tests/test_a_share_trend.py \
  tests/test_dashboard.py tests/test_dashboard_acceptance.py \
  tests/test_dashboard_web.py
git commit -m "fix: show stale-account trend actions consistently"
```

---

### Task 3: Verify the real current HK report as a revision without sending Feishu again

**Files:**

- Read: `reports/trend_hk_phillips/2026-07-14-r1.json`
- Create at runtime: `data/trend_hk_phillips/daily_delivery/2026-07-14.json`
- Create at runtime: `reports/trend_hk_phillips/2026-07-14-r2.json`
- Create at runtime: `reports/trend_hk_phillips/2026-07-14-r2.md`

- [ ] **Step 1: Prove the old artifact was already delivered before backfilling its missing ledger**

Run:

```bash
jq -e '
  .execution_date == "2026-07-15" and
  .as_of_date == "2026-07-14" and
  .metadata.market == "HK" and
  .metadata.broker == "phillips" and
  .metadata.delivery_status == "sent"
' reports/trend_hk_phillips/2026-07-14-r1.json
test ! -e data/trend_hk_phillips/daily_delivery/2026-07-14.json
```

Expected: both commands return zero. If the artifact is not marked sent, stop; do not infer permission to suppress or send a notification.

- [ ] **Step 2: Backfill a sent ledger for the already-sent v1 text**

Run this one-off operational migration:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path

from open_trader.trend_delivery import _write_ledger

title = "【辉立｜港股趋势报告｜2026-07-15】"
message = "\n".join([
    "数据截至：2026-07-14",
    "账户状态：已过期",
    "今日无买卖动作｜持有 0｜复核 0",
    "",
    "请人工确认，不自动下单。",
])
_write_ledger(
    Path("data/trend_hk_phillips/daily_delivery/2026-07-14.json"),
    "sent",
    title,
    message,
)
PY
```

Record its hash:

```bash
LEDGER_HASH_BEFORE=$(shasum -a 256 data/trend_hk_phillips/daily_delivery/2026-07-14.json | awk '{print $1}')
```

- [ ] **Step 3: Run the real HK report workflow as a revision**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader.cli trend-market-report \
  --market HK \
  --date 2026-07-14 \
  --config config/daily_premarket.env \
  --revision
```

Expected: status `generated`, new `2026-07-14-r2.md/json` artifacts, and no Feishu delivery attempt because the daily ledger is already `sent`.

- [ ] **Step 4: Verify the real candidate now becomes a 4% BUY and the ledger was untouched**

Run:

```bash
jq -e '
  .account.fresh == false and
  .metadata.position_weight == "0.04" and
  .metadata.position_weight_source == "fallback_4pct" and
  ([.strategy_judgments.formal_actions[] |
    select(
      .action == "BUY" and
      .symbol == "06821" and
      .estimated_shares == 100 and
      .target_amount == "25142.16"
    )] | length) == 1
' reports/trend_hk_phillips/2026-07-14-r2.json

LEDGER_HASH_AFTER=$(shasum -a 256 data/trend_hk_phillips/daily_delivery/2026-07-14.json | awk '{print $1}')
test "$LEDGER_HASH_BEFORE" = "$LEDGER_HASH_AFTER"
```

Also inspect the newest run log lines:

```bash
tail -20 data/trend_hk_phillips/run.log
```

Expected: a fresh generated event for the revision, no delivery error, and the ledger hash is unchanged.

---

### Task 4: Run the completion gate and redeploy the exact accepted SHA

**Files:**

- Verify: entire repository
- Runtime: `/tmp/open_trader_dashboard_8769.log`
- Runtime URL: `http://127.0.0.1:8769/`

- [ ] **Step 1: Run the full automated suite before the browser gate**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: the entire suite passes. Record the exact pytest summary.

- [ ] **Step 2: Restart the review Dashboard on the candidate Git SHA**

Capture and stop only the process listening on the review port, then start the Dashboard from this repository:

```bash
OLD_PID=$(lsof -tiTCP:8769 -sTCP:LISTEN)
test -n "$OLD_PID"
kill "$OLD_PID"
while lsof -tiTCP:8769 -sTCP:LISTEN >/dev/null; do sleep 1; done

PYTHONPATH=src nohup .venv/bin/python -u -m open_trader dashboard \
  --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv \
  --data-dir /Users/ray/projects/open_trader/data \
  --reports-dir /Users/ray/projects/open_trader/reports \
  --poll-seconds 5 \
  --host 127.0.0.1 \
  --port 8769 \
  > /tmp/open_trader_dashboard_8769.log 2>&1 &
```

Wait for HTTP 200, then capture the PID and verify its working directory:

```bash
until curl -fsS -o /dev/null http://127.0.0.1:8769/; do sleep 1; done
NEW_PID=$(lsof -tiTCP:8769 -sTCP:LISTEN)
lsof -a -p "$NEW_PID" -d cwd
ps -p "$NEW_PID" -o pid=,lstart=,command=
git rev-parse HEAD
```

Expected: the working directory is `/Users/ray/projects/open_trader`, and the process command is the port-8769 Dashboard command above.

- [ ] **Step 3: Run `make acceptance` as the final verification gate**

Run:

```bash
DASHBOARD_URL=http://127.0.0.1:8769 \
DASHBOARD_LOG=/tmp/open_trader_dashboard_8769.log \
make acceptance
```

Expected final line/status: `PASS`. On `FAIL`, diagnose, fix, rerun focused tests, restart the changed process, and rerun `make acceptance`. On `BLOCKED`, report the external/browser blocker and do not substitute curl, mocks, screenshots, or unit tests.

- [ ] **Step 4: Record and redeploy the exact accepted Git SHA**

After `PASS`:

```bash
ACCEPTED_SHA=$(git rev-parse HEAD)
test -n "$ACCEPTED_SHA"
```

Restart the review Dashboard once more using the exact same source tree and command from Step 2. Do not edit source or data between acceptance and this restart.

- [ ] **Step 5: Verify the post-acceptance deployment**

Run:

```bash
DEPLOYED_PID=$(lsof -tiTCP:8769 -sTCP:LISTEN)
ps -p "$DEPLOYED_PID" -o pid=,lstart=,command=
lsof -a -p "$DEPLOYED_PID" -d cwd
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
tail -40 /tmp/open_trader_dashboard_8769.log
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8769/
```

Expected: a new PID and fresh start timestamp, cwd `/Users/ray/projects/open_trader`, Git SHA equal to `ACCEPTED_SHA`, fresh logs without startup errors, and HTTP `200`.

- [ ] **Step 6: Report the accepted result**

Provide:

- exact pytest summary;
- `make acceptance` status `PASS`;
- accepted/deployed Git SHA;
- new Dashboard PID and start timestamp;
- confirmation that `2026-07-14-r2.json` contains HK BUY `06821`, 100 shares, `25142.16`, while the sent ledger hash remained unchanged;
- review URL `http://127.0.0.1:8769/`.

