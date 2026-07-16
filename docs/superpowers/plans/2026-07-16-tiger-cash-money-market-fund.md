# Tiger Cash and Money-Market Fund Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Tiger Dashboard cash figure include real money-market fund positions, exclude them from securities holdings, and show Tiger's separate trade-available amount.

**Architecture:** Extend the existing Tiger snapshot path to query `STK` and `FUND`, classify each result through the existing `detect_asset_class`, and persist Tiger's API FX/account metrics with the existing run artifacts. Reuse the Dashboard's existing cash-like classification to split broker detail positions; add only the Tiger trade-available field to the existing broker summary and account header.

**Tech Stack:** Python 3.12 stdlib, Tiger OpenAPI SDK already installed in `.venv`, vanilla JavaScript, pytest, existing Dashboard acceptance runner, `screen` deployment.

## Global Constraints

- No new dependencies or configuration.
- Only money-market funds count as cash; ordinary funds remain holdings.
- Trade-available amount is separate from cash and buying power.
- Do not restore the global cash tab or change SMA200 behavior.
- `make acceptance` is the final gate; only `PASS` may be deployed for review.
- After `PASS`, redeploy the exact accepted Git SHA and verify PID, cwd, SHA, fresh logs, and HTTP 200.

---

### Task 1: Fetch and classify Tiger funds

**Files:**
- Modify: `src/open_trader/tiger_account.py`
- Test: `tests/test_tiger_account.py`

**Interfaces:**
- Consumes: `TigerAccountClient._fetch_position_records(account)` and existing `detect_asset_class(symbol, name)`.
- Produces: snapshot position records for both `STK` and `FUND`; fund records carry `sec_type="FUND"`; `_asset_class_from_record()` returns `money_market_fund` only for money-market names.

- [ ] **Step 1: Write the failing fund-query test**

Add a fake client whose `get_positions()` returns the existing stock for `STK` and `华泰港元货币市场基金A` for `FUND`, then assert both explicit calls and both records:

```python
assert client.trade_client.position_calls == [
    {"account": "123456789", "sec_type": "STK"},
    {"account": "123456789", "sec_type": "FUND"},
]
assert [row["symbol"] for row in snapshot.position_records] == [
    "MSFT", "HK0000951506.HKD",
]
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_tiger_account.py::test_tiger_account_client_fetches_stock_and_fund_positions -q
```

Expected: FAIL because the client currently calls `get_positions(account=...)` once and never requests `FUND`.

- [ ] **Step 3: Implement the minimum explicit queries**

Use SDK string values to avoid importing a new enum into production code:

```python
records = []
for sec_type in ("STK", "FUND"):
    positions = self.trade_client.get_positions(
        account=account.account,
        sec_type=sec_type,
    )
    records.extend(self._position_record(account, position) for position in positions)
return records
```

Keep the existing sanitized `position_query_failed` error around the entire operation so a failed fund query cannot publish a partial snapshot.

- [ ] **Step 4: Write the failing classification test**

Map a real-shaped Tiger fund record and assert:

```python
assert positions[0].asset_class == AssetClass.MONEY_MARKET_FUND
assert positions[0].name == "华泰港元货币市场基金A"
```

Add an ordinary `FUND` name assertion returning `AssetClass.FUND` so not every fund becomes cash.

- [ ] **Step 5: Run the classification tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_tiger_account.py -k 'fund and asset_class' -q
```

Expected: FAIL because `_asset_class_from_record()` currently returns `unknown` for `FUND`.

- [ ] **Step 6: Reuse the existing classifier**

Implement the fund branch without a symbol list or new configuration:

```python
if raw_type == "FUND":
    return detect_asset_class(
        _text(record, "symbol"),
        _text(record, "name"),
    )
```

- [ ] **Step 7: Verify Task 1 GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_tiger_account.py -q
```

Expected: all Tiger account tests pass.

---

### Task 2: Persist Tiger FX and account trade-available metrics

**Files:**
- Modify: `src/open_trader/tiger_account.py`
- Test: `tests/test_tiger_account.py`

**Interfaces:**
- Consumes: Prime `Segment.currency`, `cash_balance`, `cash_available_for_trade`, and each `CurrencyAsset.forex_rate`.
- Produces: sanitized `account_total` metadata containing base cash, trade-available amount, and `fx_to_hkd`; Tiger detail CSV rows containing `fx_to_hkd` while older/non-Tiger rows remain compatible.

- [ ] **Step 1: Write failing snapshot metadata assertions**

Extend `FakePrimeAssets` with base currency fields and HKD `forex_rate`, then assert the account-total record contains:

```python
assert total["cash_balance"] == "-3980.76"
assert total["cash_available_for_trade"] == "62320.21"
assert Decimal(total["fx_to_hkd"]) == Decimal("1") / Decimal("0.1275578")
```

Also assert each currency cash record contains its computed `fx_to_hkd`.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_tiger_account.py -k 'prime_asset and fx' -q
```

Expected: FAIL because these fields are not captured.

- [ ] **Step 3: Add one conversion helper and persist the fields**

Compute `currency -> HKD` from Tiger's `currency -> base` rates:

```python
fx_to_hkd = currency_forex_rate / hkd_forex_rate
```

Reject blank, non-finite, zero, or negative rates. Record base cash/trade-available only on the ignored `account_total` record so portfolio cash is not double-counted.

- [ ] **Step 4: Write a failing sync artifact test**

Sync a snapshot containing stock, money-market fund, per-currency cash, and aligned account total. Assert:

```python
assert fund_row["asset_class"] == "money_market_fund"
assert fund_row["name"] == "华泰港元货币市场基金A"
assert all(row["symbol"] != "TIGER_UNMAPPED_ASSETS" for row in rows)
assert tiger_usd_detail["fx_to_hkd"] == "7.84"
```

- [ ] **Step 5: Run and verify RED**

Run the exact new test with `pytest -q`; expect missing `fx_to_hkd` and/or a reconciliation placeholder.

- [ ] **Step 6: Enrich only Tiger detail output**

Add `fx_to_hkd` to Tiger detail field names and pass the snapshot rate map into `_position_to_detail_row()` / `_cash_to_detail_row()`. Preserve blank rates for imported statement rows so old data continues to use the Dashboard fallback.

- [ ] **Step 7: Verify Task 2 GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_tiger_account.py -q
```

Expected: all tests pass with the fund represented by its real row and no matching residual placeholder.

---

### Task 3: Correct Tiger broker summary semantics

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: detail Position rows with `asset_class` and optional `fx_to_hkd`; latest `tiger_account_snapshot.json` account-total metrics.
- Produces: Tiger `broker_summaries[]` with securities-only `holding_value_hkd`/`holding_count`, cash plus money-market-fund `cash_like_value_hkd`, and separate `available_to_trade_hkd`.

- [ ] **Step 1: Write the failing broker-summary test**

Add one Tiger stock, one Tiger money-market fund, one ordinary Tiger fund, and Tiger cash. Assert:

```python
assert summary["holding_count"] == 2
assert summary["holding_value_hkd"] == "984.00"  # USD 100 * 7.84 + HKD 200
assert summary["cash_like_value_hkd"] == "921.60"  # HKD 1,000 - USD 10 * 7.84
```

Give the USD cash row `fx_to_hkd="7.84"` and assert that rate wins over the legacy `7.8` constant.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py -k 'broker_summaries and money_market' -q
```

Expected: FAIL because every detail Position currently counts as a holding and row FX is ignored.

- [ ] **Step 3: Split detail positions with the existing cash-like predicate**

In `_build_broker_summary()` partition detail positions by `_is_cash_like_row()`. Sum cash-like positions with cash detail rows; count and sum only the remaining positions. Do not add a Tiger-only asset classifier.

- [ ] **Step 4: Prefer row FX without breaking old artifacts**

In `_detail_value_hkd_for_summary()` and `_detail_value_hkd()`, parse a positive finite `row["fx_to_hkd"]` first, then fall back to `DETAIL_FX_TO_HKD[currency]` when the field is absent.

- [ ] **Step 5: Write the failing available-to-trade test**

Write a latest Tiger snapshot fixture with:

```json
{"record_type":"account_total","currency":"USD",
 "cash_available_for_trade":"62249.01","fx_to_hkd":"7.84"}
```

Assert `summary["available_to_trade_hkd"] == "488032.24"` and that malformed/missing metadata yields an empty value rather than a guessed amount.

- [ ] **Step 6: Load the latest Tiger metric minimally**

Search dated run directories newest-first for `tiger_account_snapshot.json`, parse the first valid `account_total`, and attach only `available_to_trade_hkd` to the Tiger summary. Catch file/JSON/value errors and return an empty field.

- [ ] **Step 7: Verify Task 3 GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py -q
```

Expected: all Dashboard state tests pass.

---

### Task 4: Show the separate trade-available amount

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard_acceptance.py` only if fake browser expectations require the new exact text

**Interfaces:**
- Consumes: `group.summary.available_to_trade_hkd`.
- Produces: an additional Tiger-only account header line such as `可交易额度 HKD 488,032.24`; existing `现金 HKD 451,097.00` remains the cash-like total.

- [ ] **Step 1: Write the failing JavaScript rendering assertion**

Give the Tiger group `available_to_trade_hkd: "488032.24"`, render `renderAccountSection(group)`, and assert:

```javascript
if (!html.includes("现金 HKD 451,097") ||
    !html.includes("可交易额度 HKD 488,032.24")) throw new Error(html);
```

Also assert a Futu group does not render `可交易额度`.

- [ ] **Step 2: Run and verify RED**

Run the exact `tests/test_dashboard_web.py` test; expect missing `可交易额度`.

- [ ] **Step 3: Add one conditional meta span**

Render the field only for Tiger and only when it has a value:

```javascript
${group.broker === "tiger" && hasValue(group.summary.available_to_trade_hkd)
  ? `<span>可交易额度 ${escapeHtml(formatMoney(group.summary.available_to_trade_hkd, "HKD"))}</span>`
  : ""}
```

- [ ] **Step 4: Verify Task 4 GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
```

Expected: all static rendering and acceptance harness tests pass.

---

### Task 5: Review, real workflow, acceptance, and exact-SHA deployment

**Files:**
- Review: all changed source and test files
- Runtime data: `data/runs/2026-07-16/` and `data/latest/` (Git-ignored)
- Runtime log: `/tmp/open_trader_dashboard_8766.log`

**Interfaces:**
- Consumes: committed implementation and real Tiger OpenAPI account.
- Produces: verified live artifacts, final `PASS`, exact accepted SHA deployment, and review URL.

- [ ] **Step 1: Run focused and full automated checks**

```bash
.venv/bin/python -m pytest tests/test_tiger_account.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest -q
git diff --check
```

Expected: zero failures/errors.

- [ ] **Step 2: Run the code-review skill and fix every actionable finding**

Review against the design commit `f1c21ce`, focusing on standards and spec compliance. Add a failing regression test before any behavior fix found during review.

- [ ] **Step 3: Commit the implementation intentionally**

Stage only files from this plan; preserve the user's unrelated untracked files.

```bash
git commit -m "fix: count Tiger money market fund as cash"
```

- [ ] **Step 4: Run the real Tiger sync**

Use the existing `sync-tiger-portfolio` command for `2026-07-16` with `--update-latest`. Verify the new snapshot and CSV show:

- `STK` and `FUND` records;
- `华泰港元货币市场基金A` as `money_market_fund`;
- no fund-sized `TIGER_UNMAPPED_ASSETS` row;
- securities holding count excludes the fund;
- cash equals net currency cash plus the fund;
- trade-available is separate and timestamped.

- [ ] **Step 5: Replace stale Dashboard processes with the candidate SHA**

Inspect `screen -ls`, `launchctl list`, `lsof -nP -iTCP:8766 -sTCP:LISTEN`, and matching process command lines. Stop only stale Dashboard port-8766 processes, clear the runtime log, and start the documented Dashboard command from `/Users/ray/projects/open_trader`.

- [ ] **Step 6: Run the final acceptance gate exactly once after development**

```bash
DASHBOARD_LOG=/tmp/open_trader_dashboard_8766.log make acceptance
```

Expected: exact status `PASS`. On `FAIL`, diagnose, add/adjust a regression test, fix, commit, refresh/restart, and rerun. On `BLOCKED`, report the blocker without substitutes.

- [ ] **Step 7: Redeploy the exact accepted SHA**

Record `ACCEPTED_SHA=$(git rev-parse HEAD)`, restart the same `screen` command without source/data changes, then verify:

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
ps -p "$NEW_PID" -o pid=,lstart=,command=
lsof -a -p "$NEW_PID" -d cwd -Fn
git rev-parse HEAD
tail -n 100 /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/api/dashboard
```

Expected: new PID, cwd `/Users/ray/projects/open_trader`, exact accepted SHA, fresh error-free logs, and HTTP `200` for both URLs.

- [ ] **Step 8: Deliver the review URL**

Provide `http://127.0.0.1:8766/`, accepted SHA, exact acceptance status, and deployed PID only after Step 7 succeeds.
