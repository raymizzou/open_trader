# Three-Market Allocation and Relative Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate one immutable A/HK/US allocation ranking after each A-share trading day, freeze its 6%/4%/2% new-position weight into all three market reports, and safely execute at most two 20-point relative-strength rotations per simulated account while keeping real-account rotations advisory-only.

**Architecture:** Add one small `trend_allocation.py` producer/loader around the existing Trend Animals client and JSON/launchd patterns. Gate the existing three market controllers on that shared snapshot, then extend the common report builder with allocation-aware sizing and account-specific frozen rotation pairs. Keep all simulated orders in the existing `trend_review.py` MARKET-order, reconciliation, and immutable-ledger path; add only a paired sell-then-buy branch. Project the frozen report fields into Markdown, Feishu, and the existing Dashboard renderer without live recalculation.

**Tech Stack:** Python 3.12, stdlib `dataclasses`/`Decimal`/JSON/hashlib, existing Trend Animals and Futu clients, pytest, existing Node VM and Playwright Dashboard tests, launchd, and the repository `make acceptance` gate.

## Global Constraints

- Start implementation from local `main` in a new `codex/trend-allocation-relative-rotation` branch and isolated worktree; do not implement on this documentation branch or in the dirty root checkout.
- Use the approved design at `docs/superpowers/specs/2026-08-03-trend-allocation-relative-rotation-design.md` and its desktop/mobile mockups as the contract.
- Do not add a database, dependency, scheduler framework, second execution engine, favorites mutation, whole-market scan, smoothing, cooldown, automatic legacy-position trim, or automatic top-up.
- `trendStrengthGlobalCurr` is the only cross-market and rotation comparison value. Label it `全局强度`; do not claim parity with the mini-program's integer page percentile.
- The allocation task is the sole ranking producer. Reports consume one immutable daily path plus SHA-256 and never generate or modify the shared ranking.
- A ranking refresh succeeds only when all six roots are unique and complete. On any failure, reuse the entire prior successful ranking; never mix fresh and stale roots.
- Cold start with no successful ranking keeps the existing 4% strategy and forced exits but does not publish 6%/4%/2%, create rotations, or claim the allocation version is executable.
- Rank 1/2/3 maps to a per-new-position base of 6%/4%/2%. It is not an account exposure cap and never resizes an existing position by itself.
- Forced exits remain first. If they create a slot, ordinary buys use it. Rotation is considered only when the relevant account still has ten occupied slots.
- Rotation pairs are independently planned for simulated and real accounts, use an inclusive 20-point strength gap, and are capped at two reserved pair slots per market/account/target trade date across revisions.
- Real accounts never enter an order-submission path. Simulated rotation buys occur only after broker facts prove the paired full-position MARKET sell fully filled.
- Strength and pair identity are frozen in the report. Execution may refresh account state, cash, quote, lot, session, risk, and order facts only; it may skip but never re-pair.
- A rotation buy may use the whole continuous session on the target trade date. No unfinished pair or buy retry crosses the target date.
- CN becomes v11; HK and US become v9. Keep the existing strategy-family prefix while publishing those new version identities. Eligible Kelly samples, drawdown baselines/high-water marks, pause state, and position lifecycles continue without reset.
- Add `exit_reason=relative_rotation` only after a proven completed rotation sell; preserve opening and closing strategy versions in the existing trade-sample path.
- Keep the Dashboard in the current warm, dense visual language. Place allocation below the report header and rotation after forced sells and before ordinary buys. Separate simulated-auto from real-manual groups.
- Run focused tests while developing. Run `make acceptance` only once source, tests, changelog, and candidate runtime are final.
- Only `make acceptance` `PASS` permits completion or review handoff. After PASS, deploy the exact accepted SHA and verify PID, cwd, SHA, fresh logs/status, and HTTP 200 at the review URL.
- Because the user explicitly requested screenshots, capture desktop and 375px mobile screenshots from the exact accepted and deployed SHA after the final PASS.

## File Map

- Add `src/open_trader/trend_allocation.py`: six-root discovery, snapshot validation/ranking, immutable daily/latest/status files, stale reuse metadata, report dependency loader, controller loop, and idempotent failure/recovery alert state.
- Modify `src/open_trader/trend_animals.py`: one thin `get_favorites_tickers()` wrapper over the existing authenticated `_get()` transport.
- Modify `src/open_trader/cli.py`: `trend-allocation run|once|status` routing and config/repo rebasing, matching `trend-market` conventions.
- Add `ops/launchd/com.open-trader.trend-allocation.plist.template`: one persistent dedicated allocation task.
- Modify `scripts/install_daily_premarket_launchd.sh` and `scripts/uninstall_daily_premarket_launchd.sh`: install, verify, and remove the allocation task with the three market controllers.
- Modify `src/open_trader/trend_market_controller.py`: wait for the daily allocation attempt window, load the frozen reference, validate it in reports, preserve pair reservations across report revisions, and execute simulated pairs through the existing controller.
- Modify `src/open_trader/a_share_trend.py`: strategy versions, favorite/recommendation union inputs, account-level entry weight, real-account capital facts, rotation planning, frozen payload/Markdown/replay fields, and report validation.
- Modify `src/open_trader/market_trend.py`: pass the same allocation reference and favorites into HK/US generation and move the US report deadline after the allocation window.
- Modify `src/open_trader/trend_review.py`: frozen rotation-pair validation, two-slot reservation ledger, paired simulated execution/recovery, and completed-trade exit reason.
- Modify `src/open_trader/dashboard.py`: validate and project the new frozen report fields without reading `latest.json` or calling Trend Animals.
- Modify `src/open_trader/dashboard_static/dashboard.js`: allocation cards, stale state, dates/SHA, and separate simulated/real rotation groups.
- Modify `src/open_trader/dashboard_static/dashboard.css`: approved desktop three-card and mobile stacked layout in existing report styles.
- Add `tests/test_trend_allocation.py`: API boundary, ranking/ties, persistence, stale fallback, cold start, controller status, CLI, and report dependency gate.
- Modify `tests/test_trend_animals.py`: favorites wrapper validation and secret-safe request assertions.
- Modify `tests/test_a_share_trend.py`: v11/v9 inheritance, 6%/4%/2% sizing, favorites union, rotation planner, real/sim independence, frozen report, Markdown, replay, and Kelly continuity.
- Modify `tests/test_market_trend.py`: HK/US shared snapshot use, ETF score source, and new generation timing.
- Modify `tests/test_trend_review.py`: reservation cap and the full sell/fill/buy, partial, failure, retry, crash, stale-account, and no-cross-day matrix.
- Modify `tests/test_trend_market_controller.py` and `tests/test_trend_market_cli.py`: controller selection, allocation report validation, and simulated-only execution orchestration.
- Modify `tests/test_daily_premarket.py`: fourth launchd job render/install/uninstall and process verification.
- Modify `tests/test_dashboard.py` and `tests/test_dashboard_web.py`: frozen contract and renderer assertions.
- Modify `tests/e2e/dashboard-warm-ledger.spec.ts`: desktop/mobile hierarchy, account labels, stale badge, and responsive layout.
- Modify `CHANGELOG.md`: dated operator-facing behavior and exact verification evidence before merge.

---

### Task 1: Add the Thin Favorites API Boundary

**Files:**

- Modify: `src/open_trader/trend_animals.py:74-130`
- Test: `tests/test_trend_animals.py`

**Interfaces:**

- Consumes: existing `TrendAnimalsClient._get()` authentication, response validation, URL encoding, and secret redaction.
- Produces: `TrendAnimalsClient.get_favorites_tickers() -> list[dict[str, object]]`; it requests all favorites with no category filter and performs no paid snapshot call.

- [ ] **Step 1: Write failing wrapper tests**

Add:

```python
def test_get_favorites_tickers_uses_the_free_all_favorites_endpoint(
    tmp_path: Path,
) -> None:
    rows = [{
        "tmId": 10001,
        "asset": "A股",
        "assetCategory": "大类",
        "tickername": "A股",
        "asOfDate": "2026-08-03",
    }]
    transport = FakeTransport({"getFavoritesTicker": success(rows)})
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    assert client.get_favorites_tickers() == rows
    assert len(transport.calls) == 1
    assert "/getFavoritesTicker?" in transport.calls[0]
    assert "favCategory=" not in transport.calls[0]
```

Extend the existing unsafe-response test so a favorites row containing the API key is rejected without echoing the key.

- [ ] **Step 2: Run the focused tests and confirm the expected failure**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_trend_animals.py::test_get_favorites_tickers_uses_the_free_all_favorites_endpoint \
  tests/test_trend_animals.py::test_cached_response_that_contains_current_secret_is_rejected -q
```

Expected: FAIL because `get_favorites_tickers` does not exist.

- [ ] **Step 3: Add the one-line client method**

```python
def get_favorites_tickers(self) -> list[dict[str, object]]:
    return self._get("getFavoritesTicker", {})
```

Do not add a second client, category model, cache, pagination layer, or percentile formula.

- [ ] **Step 4: Re-run the focused tests**

Expected: both tests PASS.

- [ ] **Step 5: Run the complete client suite**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_trend_animals.py -q
```

Expected: PASS with no credential, caching, mapping, or paid-request regression.

- [ ] **Step 6: Commit the API slice**

```bash
git add src/open_trader/trend_animals.py tests/test_trend_animals.py
git commit -m "feat: read Trend Animals favorites"
```

---

### Task 2: Produce and Load One Immutable Allocation Snapshot

**Files:**

- Add: `src/open_trader/trend_allocation.py`
- Add: `tests/test_trend_allocation.py`

**Interfaces:**

- Consumes: `get_update_status()`, `get_favorites_tickers()`, `get_snapshots()`, `SEARCH_ASSETS_BY_MARKET`, `_process_version()`, and the existing JSON decimal-string convention.
- Produces:
  - `build_allocation_snapshot(*, allocation_date: str, generated_at: str, git_sha: str, roots: Mapping[str, object], previous: Mapping[str, object] | None) -> dict[str, object]`
  - `write_allocation_snapshot(data_dir, snapshot, *, revision: bool = False) -> dict[str, str]`
  - `load_allocation_reference(data_dir, *, allocation_date, a_trading_days) -> dict[str, object] | None`
  - immutable `data/trend_allocation/daily/YYYY-MM-DD.json`
  - atomic `data/trend_allocation/latest.json`
  - atomic `data/trend_allocation/controller_status.json`

- [ ] **Step 1: Write failing ranking and validation tests**

Cover unique six-root discovery, ETF winning a market, direct 1/2/3 ordering, secondary-root tie breaking, prior-order preservation on a complete tie, first-run complete-tie failure, a missing root, duplicate root, invalid/non-finite strength, and allowed differing source dates across roots. Also cover explicit `-rN` allocation revisions and rejection of a revision when any of the three target execution batches is already locked.

Use this central happy-path assertion:

```python
def test_build_allocation_snapshot_ranks_by_the_stronger_root() -> None:
    snapshot = build_allocation_snapshot(
        allocation_date="2026-08-03",
        generated_at="2026-08-03T16:18:00+08:00",
        git_sha="a" * 40,
        roots=root_rows(
            cn=("62.7", "58.3"),
            hk=("78.4", "75.0"),
            us=("80.0", "95.2"),
        ),
        previous=None,
    )

    assert snapshot["markets"] == {
        "US": {
            "rank": 1, "score": "95.2", "score_source": "美国ETF",
            "entry_weight": "0.06", "nominal_weight": "0.60",
        },
        "HK": {
            "rank": 2, "score": "78.4", "score_source": "港股",
            "entry_weight": "0.04", "nominal_weight": "0.40",
        },
        "CN": {
            "rank": 3, "score": "62.7", "score_source": "A股",
            "entry_weight": "0.02", "nominal_weight": "0.20",
        },
    }
```

The fetch test must prove roots are selected by exact `asset`, joined by positive `tmId`, grouped by expected `asOfDate` for the smallest valid set of batch snapshot calls, and request only:

```python
ROOT_FIELDS = (
    "tmId", "tickerName", "asset", "asOfDate",
    "trendStrengthGlobalCurr",
)
```

- [ ] **Step 2: Run the new unit tests and confirm import failure**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_trend_allocation.py -q
```

Expected: collection FAIL because `open_trader.trend_allocation` does not exist.

- [ ] **Step 3: Implement the pure ranking contract**

Add fixed mappings, not configuration:

```python
ROOT_ASSETS = {
    "CN": ("A股", "ETF基金"),
    "HK": ("港股", "香港ETF"),
    "US": ("美股", "美国ETF"),
}
ENTRY_WEIGHTS = {1: Decimal("0.06"), 2: Decimal("0.04"), 3: Decimal("0.02")}
NOMINAL_WEIGHTS = {1: Decimal("0.60"), 2: Decimal("0.40"), 3: Decimal("0.20")}
```

Validate all six roots before sorting. For each market sort on `(max_strength, min_strength)` descending. Only if both values tie, use the previous successful market order; if there is no previous order, raise `TrendAnimalsError` instead of using the market code.

Use `Decimal` internally and serialize strengths/weights with `format(value, "f")`.

- [ ] **Step 4: Add immutable daily and atomic pointer writes**

Reuse stdlib JSON and SHA-256. Daily behavior:

```python
payload = canonical_json_bytes(snapshot)
sha256 = hashlib.sha256(payload).hexdigest()
daily = data_dir / "trend_allocation/daily" / f"{allocation_date}.json"
```

If `daily` exists with identical bytes, return its existing reference. If it differs and `revision=False`, fail closed. With `revision=True`, first prove none of the three affected report execution batches is locked, then write the next immutable `YYYY-MM-DD-rN.json`. Write `latest.json` atomically as only:

```json
{"daily_path":"data/trend_allocation/daily/2026-08-03.json","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
```

Validate the pointed bytes every time they are loaded. Do not cache the moving pointer in process memory.

The revision is inferred from the immutable daily filename; `latest.json` keeps only `daily_path` and `sha256`. Each market controller compares the frozen allocation SHA in its latest report with the new pointer and creates one normal `-rN` report revision before execution begins. Preflight all three execution locks before publishing, then keep the correction status blocking/retrying until all three reports converge on the same new SHA.

- [ ] **Step 5: Add whole-snapshot stale fallback metadata**

`load_allocation_reference()` returns the last valid snapshot plus report metadata:

```python
{
    "daily_path": reference["daily_path"],
    "sha256": reference["sha256"],
    "snapshot": snapshot,
    "reused": snapshot["allocation_date"] != allocation_date,
    "stale_a_trading_days": count_completed_a_days_after_snapshot,
    "failure_reason": status_failure_reason,
}
```

Any bad pointer, hash, schema, root, or market mapping is an error; do not silently accept a corrupt prior. A missing `latest.json` returns `None` so callers keep legacy 4% behavior.

- [ ] **Step 6: Run the new allocation suite**

Expected: PASS for all ranking, validation, persistence, idempotency, stale fallback, and cold-start cases.

- [ ] **Step 7: Commit the snapshot slice**

```bash
git add src/open_trader/trend_allocation.py tests/test_trend_allocation.py
git commit -m "feat: freeze three-market allocation ranking"
```

---

### Task 3: Run Allocation as a Dedicated Task and Gate the Three Reports

**Files:**

- Modify: `src/open_trader/trend_allocation.py`
- Modify: `src/open_trader/cli.py:509-550,1322-1595`
- Add: `ops/launchd/com.open-trader.trend-allocation.plist.template`
- Modify: `scripts/install_daily_premarket_launchd.sh`
- Modify: `scripts/uninstall_daily_premarket_launchd.sh`
- Modify: `src/open_trader/trend_market_controller.py:80-110,2605-2995`
- Modify: `src/open_trader/market_trend.py:85-90,1466-1580`
- Test: `tests/test_trend_allocation.py`
- Test: `tests/test_trend_market_cli.py`
- Test: `tests/test_trend_market_controller.py`
- Test: `tests/test_market_trend.py`
- Test: `tests/test_daily_premarket.py`

**Interfaces:**

- Consumes: existing `DailyPremarketConfig`, Trend Animals client construction, Futu CN calendar, `RunLock`, notifier composition, controller status identity, and launchd installer verification.
- Produces:
  - `open-trader trend-allocation run --config config/daily_premarket.env`
  - `open-trader trend-allocation once --date 2026-08-03 [--revision] --config config/daily_premarket.env`
  - `open-trader trend-allocation status --config config/daily_premarket.env`
  - a KeepAlive launchd task `com.open-trader.trend-allocation`
  - a terminal current-cycle status before any of the three reports generate.

- [ ] **Step 1: Write failing CLI/controller tests**

Assert parser routing, `--revision` routing, executor-host enforcement, repo/python rebasing, status read-only behavior, one-shot success, failure reuse, notification once from first failure, recovery clearing the blocker without a normal-success notification, A-holiday no-write behavior, and status fields:

```python
assert status | {
    "schema_version": "open_trader.trend_allocation.status.v1",
    "pid": status["pid"],
    "working_directory": str(checkout),
    "git_sha": "a" * 40,
    "phase": "ready",
    "attempted_for": "2026-08-03",
    "latest_daily_path": "data/trend_allocation/daily/2026-08-03.json",
    "latest_sha256": status["latest_sha256"],
    "blocker": None,
} == status
```

Add controller tests proving CN/HK/US do not submit `_generate_report` before the allocation task has recorded the current Shanghai post-close attempt, and all proceed with the same last-success reference after either success or explicit fallback. On Shanghai A-share holidays, permit the last success after the daily 16:20 gate without requiring a new daily file.

- [ ] **Step 2: Run the focused tests and confirm failures**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_trend_allocation.py \
  tests/test_trend_market_cli.py \
  tests/test_trend_market_controller.py \
  tests/test_market_trend.py -q
```

Expected: FAIL because the commands, controller loop, and report gate do not exist.

- [ ] **Step 3: Implement one persistent allocation loop**

Keep the timing fixed in code: attempt after `16:20 Asia/Shanghai`, only when the Futu CN calendar says the Shanghai date is an A-share trading day. Retry failures with the existing bounded controller backoff until `17:45`. Success makes the current snapshot available immediately. At `17:45`, an unresolved refresh becomes one terminal whole-snapshot fallback for that cycle: record the old reference and reason in status, alert, and leave `latest.json` unchanged. This terminal status releases all three reports together. A successful later A-share cycle clears the blocker without sending normal-notification noise.

Use `RunLock(data_dir / "runs/.trend_allocation.lock")`, the existing executor-host check, and the existing notifier stack. Do not create another daemon framework. `once --date` runs the same cycle body exactly once for direct verification.

- [ ] **Step 4: Gate reports on the shared attempt, not on first-run order**

Add one `allocation_reference` field to `ReportTask`. The controller determines the Shanghai allocation cycle before `_generate_report` and passes the validated reference into CN/HK/US generation. It must not let a market report call the producer.

For a cold start, pass `None` only after the dedicated allocation task has made a terminal attempt, so the legacy report can continue without claiming v11/v9 allocation behavior.

Change the CN retry deadline from `18:00` to `19:00` and the US report deadline from Shanghai noon to `19:00`; HK already uses `19:00`. Keep every target execution session unchanged. The controller-level allocation success or 17:45 terminal fallback is authoritative for all markets.

- [ ] **Step 5: Add the launchd task by copying the existing controller pattern**

The plist arguments are:

```xml
<string>OPEN_TRADER_PYTHON</string>
<string>-m</string>
<string>open_trader</string>
<string>trend-allocation</string>
<string>run</string>
<string>--config</string>
<string>OPEN_TRADER_CONFIG</string>
```

Use `RunAtLoad`, `KeepAlive`, and `ThrottleInterval=30`, with dedicated stdout/stderr logs. Extend the existing installer to render, lint, bootout/bootstrap, verify PID/cwd/SHA/status, and extend the uninstaller to remove it. Do not create a second install script.

- [ ] **Step 6: Run controller, CLI, and installer suites**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_trend_allocation.py tests/test_trend_market_cli.py \
  tests/test_trend_market_controller.py tests/test_market_trend.py \
  tests/test_daily_premarket.py -q
```

Expected: PASS, including fourth-job install/uninstall and no early report generation.

- [ ] **Step 7: Commit the dedicated task slice**

```bash
git add src/open_trader/trend_allocation.py src/open_trader/cli.py \
  src/open_trader/trend_market_controller.py src/open_trader/market_trend.py \
  ops/launchd/com.open-trader.trend-allocation.plist.template \
  scripts/install_daily_premarket_launchd.sh \
  scripts/uninstall_daily_premarket_launchd.sh \
  tests/test_trend_allocation.py tests/test_trend_market_cli.py \
  tests/test_trend_market_controller.py tests/test_market_trend.py \
  tests/test_daily_premarket.py
git commit -m "feat: run allocation before three-market reports"
```

---

### Task 4: Publish v11/v9 and Apply 6%/4%/2% Only to New Positions

**Files:**

- Modify: `src/open_trader/a_share_trend.py:125-165,473-990,3508-3965,4879-5035`
- Modify: `src/open_trader/market_trend.py:871-1425`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**

- Consumes: validated allocation reference from Task 3, existing `live_trend_strategy_snapshot()`, `_plan_buy_actions()`, Kelly/drawdown contracts, and report evidence replay.
- Produces: CN v11, HK/US v9, frozen allocation parameters, and allocation rank weight passed as the existing `position_weight` for new buys only.

- [ ] **Step 1: Write failing version and sizing tests**

Add a parameterized inheritance test:

```python
@pytest.mark.parametrize(
    ("market", "version", "rank", "weight"),
    [("CN", "v11", 1, "0.06"), ("HK", "v9", 2, "0.04"),
     ("US", "v9", 3, "0.02")],
)
def test_current_allocation_versions_freeze_rank_weight(
    market: str, version: str, rank: int, weight: str
) -> None:
    snapshot = live_trend_strategy_snapshot(
        market, "abc123", (1, 2),
        allocation=allocation_for(market, rank=rank, entry_weight=weight),
    )

    assert snapshot["strategy_version"] == version
    assert snapshot["parameters"]["allocation_rank"] == rank
    assert snapshot["parameters"]["target_weight"] == weight
    assert snapshot["parameters"]["allocation_snapshot_sha256"] == "b" * 64
```

Prove: a new buy is 6%/4%/2%; an existing 6% holding is not trimmed after rank falls; an existing 2% holding is not topped up after rank rises; a full ten-slot account may still add no position; an open slot may buy even when total exposure already exceeds nominal weight; Kelly/risk/cash/lot may reduce or block the base.

Add cold-start tests proving the prior v10/v8 4% behavior remains and no allocation fields are claimed.

- [ ] **Step 2: Run focused strategy tests and confirm failures**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'allocation or v11 or market_v9' \
  tests/test_market_trend.py -k 'allocation or v9' -q
```

Expected: FAIL because allocation-aware versions and parameters are unsupported.

- [ ] **Step 3: Extend the existing strategy snapshot, without a new sizing engine**

When a validated allocation is supplied, select v11 for CN and v9 for HK/US, add the snapshot path/SHA/rank/score/source/entry and nominal weights to `parameters` and `parameter_rows`, and replace only the existing target-weight value used for a new buy.

Historical versions remain byte-compatible. The new versions inherit all current entry, exit, Kelly, drawdown, mapping, ETF, overheat, and risk rules. Extend the existing version-validation sets and canonical replay normalization for exactly `(CN,v11)`, `(HK,v9)`, `(US,v9)`.

For CN v11 only, replace the historical temperature-keyed `target_weight` mapping with the allocation market's single `entry_weight`; both hot and boiling candidates use the same 6%/4%/2% base before Kelly/risk/cash/lot reductions. CN v1-v10 retain their frozen temperature-specific mapping.

- [ ] **Step 4: Pass the allocation weight through current report generation**

Both `_attempt_report()` and `_attempt_market_report()` pass:

```python
position_weight=Decimal(allocation_market["entry_weight"])
position_weight_source="trend_allocation_rank"
```

Do not compare account exposure with `nominal_weight`; it is display-only. Do not touch existing positions unless another existing exit rule or a later relative-rotation pair selects them.

- [ ] **Step 5: Preserve Kelly and drawdown continuity**

Extend the accepted opening/closing strategy-version families so v11/v9 read the existing eligible samples and drawdown state. Keep the current strategy-family prefix, publish the expected `/v11` or `/v9` identity, and do not reset files, copy ledgers, or start a new high-water mark.

Add assertions that sample count, selected round IDs, cap, high-water mark, pause state, and position `position_started_for` match the prior version for identical facts.

- [ ] **Step 6: Run full three-market report suites**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_kelly.py tests/test_trend_simulate_positions.py -q
```

Expected: PASS with no historical report or replay drift.

- [ ] **Step 7: Commit the strategy slice**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_kelly.py tests/test_trend_simulate_positions.py
git commit -m "feat: size new trend positions by market rank"
```

---

### Task 5: Union Favorites with Recommendations and Freeze Account-Specific Rotation Pairs

**Files:**

- Modify: `src/open_trader/a_share_trend.py:1086-1265,1340-1500,1840-1980,3508-3965,5040-5165`
- Modify: `src/open_trader/market_trend.py:871-1425`
- Modify: `src/open_trader/trend_review.py:799-1010,3040-3135`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`
- Test: `tests/test_trend_review.py`

**Interfaces:**

- Consumes: all favorites, existing component/recommendation rows, existing unique bidirectional symbol mappings, `CandidateInput.global_strength`, `HoldingSnapshot.global_strength`, ordinary eligibility results, real/sim account facts, and report locks.
- Produces:
  - candidate pool `favorites ∪ existing report recommendations`
  - `RotationPair` frozen values
  - `simulate_rotation_pairs` and `real_rotation_pairs`
  - two durable pair reservations per market/account/target date across revisions.

- [ ] **Step 1: Write failing candidate-union tests**

Prove a favorite not present in configured component pools can enter the ordinary candidate list only after passing every existing gate; duplicate `tmId` is requested once; report recommendations remain present even if not favorited; root nodes are not treated as securities; favorites are never mutated; an unverified mapping or missing global strength excludes only that symbol from rotation.

Assert the report requests snapshots for only the deduplicated union, not all market securities.

- [ ] **Step 2: Write the pure rotation-planner tests**

Use full accounts and assert:

```python
def test_rotation_pairs_weakest_with_strongest_at_inclusive_twenty_points() -> None:
    pairs = plan_rotation_pairs(
        holdings=[held("WEAK1", "10"), held("WEAK2", "20"), held("KEEP", "80")],
        candidates=[candidate("STRONG1", "90"), candidate("STRONG2", "40")],
        entry_weight=Decimal("0.04"),
        available_slots=0,
        pair_slots=(0, 1),
    )

    assert [(p.sell_symbol, p.buy_symbol, p.strength_gap) for p in pairs] == [
        ("WEAK1", "STRONG1", Decimal("80")),
        ("WEAK2", "STRONG2", Decimal("20")),
    ]
```

Also cover: gap 19.9 rejected; stable symbol tie break; one candidate used once; maximum two; forced exit/partial exit/manual review/pending/missing mapping/missing strength excluded; an ordinary free slot yields no rotations; no cooldown; sim and real holdings produce different pairs; real capital/lot/risk can reduce or block only the real proposal.

- [ ] **Step 3: Run the focused tests and confirm failures**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'favorite or rotation' \
  tests/test_market_trend.py -k 'favorite or rotation' \
  tests/test_trend_review.py -k 'rotation_reservation' -q
```

Expected: FAIL because candidate union, pair planning, and reservations do not exist.

- [ ] **Step 4: Reuse the existing candidate and holding models**

Do not create a parallel eligibility pipeline. Add favorites to the `tmId` set before the existing unified snapshot fetch, convert them through the same `_candidate_input()` and mapping validation, then feed the deduplicated rows to `build_candidate_list()`.

Use the existing `global_strength` fields. Add one frozen dataclass:

```python
@dataclass(frozen=True)
class RotationPair:
    pair_index: int
    sell_symbol: str
    sell_name: str
    sell_futu_symbol: str
    sell_global_strength: Decimal
    buy_symbol: str
    buy_name: str
    buy_futu_symbol: str
    buy_global_strength: Decimal
    strength_gap: Decimal
    target_weight: Decimal
    target_amount: Decimal
    estimated_shares: int
    lot_size: int
    atr: Decimal
    reason: str = "relative_rotation"
```

For real sizing, extend `RealHoldingInput` with its already-available broker net value, available cash, and position count. Keep it read-only.

- [ ] **Step 5: Add a two-slot immutable reservation ledger**

Store pair reservations under:

```text
data/trend_review/rotation_plans/<MARKET>/<account_key>/<execution_date>/0.json
data/trend_review/rotation_plans/<MARKET>/<account_key>/<execution_date>/1.json
```

`account_key` is `simulate-<configured id>` or `real-<broker>`. A reservation freezes the pair payload, source allocation SHA, account, market, execution date, pair index, and reservation time; it does not contain a report SHA, avoiding a circular hash dependency. Under the existing report lock:

- load prior valid reservations;
- keep their pair slots on a revision instead of computing replacements;
- fill only unused slots, to a lifetime maximum of two for that date;
- never delete or overwrite a reservation;
- reject corrupt or conflicting reservation facts.

The execution batch still selects only one report SHA, so pre-execution report revisions may carry the same reserved pair with the new report identity without expanding the two-slot budget.

- [ ] **Step 6: Add pairs after ordinary action planning**

In `build_report()`, evaluate forced exits and ordinary buys first. Plan rotations only when post-exit position count is ten and ordinary buys have no open slot. Run the same pure planner once for simulated and once for real account facts. Freeze the allocation reference, generated time, and target trade date in the report.

- [ ] **Step 7: Run report/replay suites**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_review.py -q
```

Expected: PASS, including two-slot persistence across `-rN` reports and independent real/sim plans.

- [ ] **Step 8: Commit the report-planning slice**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  src/open_trader/trend_review.py tests/test_a_share_trend.py \
  tests/test_market_trend.py tests/test_trend_review.py
git commit -m "feat: freeze relative-strength rotation pairs"
```

---

### Task 6: Validate, Render, and Deliver the Frozen Report Contract

**Files:**

- Modify: `src/open_trader/a_share_trend.py:3970-4040,4370-4645,4879-5325`
- Modify: `src/open_trader/trend_review.py:510-675,5580-5915`
- Modify: `src/open_trader/trend_market_controller.py:408-507`
- Modify: `src/open_trader/dashboard.py:650-850,1650-1815,2180-2335`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_trend_review.py`
- Test: `tests/test_trend_market_controller.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**

- Consumes: Task 5 pairs and Task 2 immutable allocation reference.
- Produces: one strict frozen JSON contract used unchanged by Markdown, Feishu, Dashboard, replay, delivery receipt, report revision, and execution selection.

- [ ] **Step 1: Write failing frozen-contract tests**

Assert `_report_payload()` contains:

```python
assert payload["allocation"] == {
    "daily_path": "data/trend_allocation/daily/2026-08-03.json",
    "sha256": "b" * 64,
    "allocation_date": "2026-08-03",
    "generated_at": "2026-08-03T16:18:00+08:00",
    "reused": False,
    "stale_a_trading_days": 0,
    "failure_reason": "",
    "roots": payload["allocation"]["roots"],
    "markets": payload["allocation"]["markets"],
}
assert payload["strategy_judgments"]["simulate_rotation_pairs"]
assert payload["strategy_judgments"]["real_rotation_pairs"]
```

Reject a wrong allocation hash, moving `latest.json` path, missing root date, duplicate market rank, malformed pair, gap below 20, more than two pairs, real pair marked automatic, sim pair marked manual, mismatched execution date, or pair symbols absent from frozen decisions/candidates.

Prove receipt recovery and replay preserve these values exactly and never read a later `latest.json`.

- [ ] **Step 2: Run the contract tests and confirm failures**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k 'allocation_payload or rotation_payload or markdown' \
  tests/test_trend_review.py -k 'rebuild and rotation' \
  tests/test_trend_market_controller.py -k 'valid_report and allocation' \
  tests/test_dashboard.py -k 'trend_report and allocation' -q
```

Expected: FAIL because consumers do not recognize the new frozen fields.

- [ ] **Step 3: Extend one canonical report payload**

Add `allocation`, `simulate_rotation_pairs`, and `real_rotation_pairs` to `TrendReport`/`_report_payload()` and validate them centrally. Keep `formal_actions` for existing forced exits and ordinary buys; rotation pairs remain a separate ordered contract so generic execution cannot buy before the paired sell fill.

Add the allocation daily JSON bytes and reference to `freeze_report_evidence()` so replay can prove the SHA without consulting the current pointer.

- [ ] **Step 4: Render Markdown and Feishu from frozen fields**

Immediately after the report dates/status, render the three market ranks with stock/ETF root strengths, score source, per-new-position base, nominal ten-slot exposure, source dates, generated time, target trade date, and short SHA. If reused, render exactly:

```text
沿用旧排名 · N 个 A 股交易日
```

After forced sells and before ordinary buys, render separate sections:

```text
模拟盘自动轮换
实盘手动轮换建议
```

Each row shows full sell, frozen sell/buy global strengths, gap, target weight/amount/quantity, MARKET sell-before-buy sequence, target trade date, and no-cross-day warning. Feishu uses the same Markdown/report object and must not query the API.

- [ ] **Step 5: Make controller and Dashboard validation strict**

`_valid_report()` checks the allocation path/SHA schema and pair contracts before a report can be selected or batch-locked. Dashboard validates and projects only the frozen values. Historical reports without allocation remain valid and render no allocation/rotation section.

- [ ] **Step 6: Run all frozen-report consumers**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_review.py tests/test_trend_market_controller.py \
  tests/test_dashboard.py tests/test_trend_delivery.py -q
```

Expected: PASS with historical compatibility and exact replay equality.

- [ ] **Step 7: Commit the frozen-contract slice**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/trend_review.py \
  src/open_trader/trend_market_controller.py src/open_trader/dashboard.py \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_review.py tests/test_trend_market_controller.py \
  tests/test_dashboard.py tests/test_trend_delivery.py
git commit -m "feat: publish allocation and rotation reports"
```

---

### Task 7: Execute Simulated Rotation Pairs Sell-Then-Buy

**Files:**

- Modify: `src/open_trader/trend_review.py:799-1010,1080-1295,3040-3515,4370-4650`
- Modify: `src/open_trader/trend_market_controller.py:760-930,2317-2455,2870-3150`
- Test: `tests/test_trend_review.py`
- Test: `tests/test_trend_market_controller.py`

**Interfaces:**

- Consumes: locked report SHA, `simulate_rotation_pairs`, current simulated account snapshot, existing `FutuSimulateOrderExecutionClient`, MARKET order request, broker order history, quote snapshots, lot/risk sizing helpers, and immutable action-event writers.
- Produces: one durable state machine per `market + account_id + execution_date + report_sha256 + pair_index`; no real-account submit path.

- [ ] **Step 1: Write the failing execution matrix**

Add tests for:

- zero candidate quantity preflight: no sell intent;
- stale account state, weak holding absent, candidate already held, or account not full: pair skipped with one durable reason;
- full MARKET sell submitted once, reconciled full fill, refreshed account/cash/quote/risk, then MARKET buy;
- sell partial/rejected/failed/uncertain: no buy;
- pair one failure does not block pair two;
- post-sell risk/cash/lot yields zero: cash retained, no buy;
- buy full fill: complete;
- buy partial fill: retain and terminal partial, no chase;
- buy zero fill/cancel: one same-session idempotent retry only;
- crash after proven sell fill resumes buy once during same session;
- restart with uncertain sell or buy never duplicates;
- late close leaves cash;
- next date marks incomplete and never carries or retries;
- CN/HK rotations may buy after 10:00 while continuous session is open;
- no `submit_order` call is reachable for `real_rotation_pairs`.

The happy path must assert:

```python
assert [request["side"] for request in client.requests] == ["SELL", "BUY"]
assert all(request["order_type"] == "MARKET" for request in client.requests)
assert client.requests[0]["quantity"] == 1_000
assert client.requests[1]["quantity"] == 600
```

- [ ] **Step 2: Run rotation execution tests and confirm failures**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_trend_review.py -k 'relative_rotation' \
  tests/test_trend_market_controller.py -k 'relative_rotation' -q
```

Expected: FAIL because paired execution is absent.

- [ ] **Step 3: Add a separate paired branch inside the existing executor module**

Implement `execute_relative_rotations(*, data_dir, report, client, market, execution_date, now, quote_prices)` in `trend_review.py`; do not create a second broker client or generic workflow engine. Store facts under:

```text
data/trend_review/ledgers/<MARKET>/rotations/<execution_date>/<pair_key>/
```

Derive `pair_key` from the five frozen identity parts. Reuse canonical JSON, immutable writes, order remark, account identity guard, broker observation, order reconciliation, and current sizing/risk helpers.

- [ ] **Step 4: Enforce sell proof before buy**

Before sell, estimate a nonzero buy from live quote, live cash, conservative net sell proceeds after `normal_cost_rate`, current target weight, lot, and risk. Submit the full currently sellable quantity as MARKET.

Only an immutable broker observation proving `filled_qty == target_qty` advances to buy. Refresh account snapshot and quote, recompute quantity with the frozen target weight and current risk/cash, and submit MARKET buy. Never use the sale's whole notional as the buy target.

- [ ] **Step 5: Add target-session and retry rules**

Use existing market time zones and session checks, but rotation buys use the full continuous session rather than `BUY_WINDOWS`. One clearly unfilled/cancelled buy may increment attempt from 1 to 2 on the same target date. Partial fill is terminal. Any date rollover writes `missed`/`incomplete` and leaves cash.

- [ ] **Step 6: Wire the market controller after ordinary actions**

After locking and validating the report, run existing `execute_trend_review_open()` for formal actions, then `execute_relative_rotations()` for simulated pairs. Merge submitted counts/artifact paths/status without passing real pairs. Extend `_execution_completed()` to audit both branches before declaring the cycle complete.

- [ ] **Step 7: Record the completed rotation trade**

On proven rotation sell completion, feed the existing close/projection path with:

```python
{
    "exit_reason": "relative_rotation",
    "opening_strategy_version": existing_opening_version,
    "closing_strategy_version": current_report_version,
}
```

Assert Kelly eligible sample count increases exactly as for another completed strategy exit and no sample history is reset.

- [ ] **Step 8: Run executor/controller suites**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_trend_review.py tests/test_trend_market_controller.py \
  tests/test_trend_kelly.py -q
```

Expected: PASS for ordinary orders, partial exits, revisions, recovery, and new rotations.

- [ ] **Step 9: Commit the execution slice**

```bash
git add src/open_trader/trend_review.py src/open_trader/trend_market_controller.py \
  tests/test_trend_review.py tests/test_trend_market_controller.py \
  tests/test_trend_kelly.py
git commit -m "feat: execute simulated relative rotations"
```

---

### Task 8: Build the Approved Dashboard Allocation and Rotation UI

**Files:**

- Modify: `src/open_trader/dashboard_static/dashboard.js:4260-4785`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1651-1915,4983-5065`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/e2e/dashboard-warm-ledger.spec.ts`

**Interfaces:**

- Consumes: only `report.allocation`, `report.simulate_rotation_pairs`, and `report.real_rotation_pairs` from Dashboard projection.
- Produces: approved report hierarchy with allocation cards below header and rotation groups between forced sell and ordinary buy.

- [ ] **Step 1: Write failing renderer tests**

In the Node VM test, render a report with US rank 1, HK rank 2, CN rank 3, one simulated pair, and a different real pair. Assert ordering:

```javascript
assert(html.indexOf('trend-allocation-panel') > html.indexOf('trend-report-header'));
assert(html.indexOf('cn-trend-sell') < html.indexOf('trend-rotation-panel'));
assert(html.indexOf('trend-rotation-panel') < html.indexOf('cn-trend-buy'));
assert(html.includes('模拟盘自动'));
assert(html.includes('实盘手动'));
assert(html.includes('全局强度'));
assert(html.includes('6%'));
```

Assert reused copy includes `沿用旧排名 · 2 个 A 股交易日`, source dates, report generated time, target trade date, and short SHA. Historical reports without allocation must remain unchanged.

- [ ] **Step 2: Add failing Playwright desktop/mobile checks**

At desktop width, assert three allocation cards form three columns and both rotation groups are separately labeled. At 375px, assert cards stack to one column, neither table/card overflows horizontally, touch controls retain 44px minimums, and allocation precedes rotation in the accessibility/DOM order.

- [ ] **Step 3: Run UI tests and confirm failures**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest tests/test_dashboard_web.py -k 'allocation or rotation' -q
npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts \
  --grep "allocation|relative rotation"
```

Expected: FAIL because the sections and styles do not exist.

- [ ] **Step 4: Add two small render helpers**

Add `renderTrendAllocation(report)` and `renderTrendRotations(report)`. Keep HTML generation in `dashboard.js`; do not introduce a component framework or client-side ranking. Escape every frozen value through existing helpers.

Cards show rank, market, score source, stock/ETF global strengths, 6%/4%/2% base, 60%/40%/20% nominal ten-slot exposure, source dates, status badge, generated/target dates, and short SHA.

Rotation rows show sell, buy, frozen strengths, gap, target weight/amount/quantity, and MARKET sell-before-buy sequence. Render sim and real arrays independently.

- [ ] **Step 5: Add minimal CSS in existing report tokens**

Use existing background, border, text, radius, spacing, and responsive breakpoints. Desktop:

```css
.trend-allocation-cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
```

At the existing mobile breakpoint:

```css
.trend-allocation-cards { grid-template-columns: minmax(0, 1fr); }
```

Do not add charts, gradients, animation, tabs, filters, live polling, or a new color system.

- [ ] **Step 6: Run Dashboard unit and browser tests**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_dashboard.py tests/test_dashboard_web.py -q
npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts
```

Expected: PASS on current desktop/mobile report flows and new hierarchy.

- [ ] **Step 7: Commit the UI slice**

```bash
git add src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard_web.py tests/e2e/dashboard-warm-ledger.spec.ts
git commit -m "feat: show allocation and rotation reports"
```

---

### Task 9: Prove the Complete Workflow, Update the Merge Log, and Deploy the Accepted SHA

**Files:**

- Modify: `CHANGELOG.md`
- Verify: all files from Tasks 1-8

**Interfaces:**

- Consumes: final implementation candidate.
- Produces: exact test evidence, live allocation/report/execution evidence, accepted/deployed SHA, review URL, and requested desktop/mobile screenshots.

- [ ] **Step 1: Run the complete focused behavior suite**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  .venv/bin/python -m pytest \
  tests/test_trend_animals.py tests/test_trend_allocation.py \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_review.py tests/test_trend_market_controller.py \
  tests/test_trend_market_cli.py tests/test_daily_premarket.py \
  tests/test_trend_kelly.py tests/test_trend_delivery.py \
  tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: PASS. Record the exact count and duration for the changelog/handoff.

- [ ] **Step 2: Run deterministic direct workflow checks**

With a temporary data/reports/log root and fixed fake provider/broker inputs, invoke the real CLI/controller entry points and prove:

- allocation daily file, latest pointer, status, and SHA are valid/idempotent;
- all three report JSON files freeze the same allocation SHA;
- CN/HK target the next session and US targets that evening's session;
- a stale provider attempt leaves `latest.json` unchanged and all reports display prior rank plus stale A-day count;
- one simulated rotation submits MARKET sell, reconciles full fill, refreshes, then submits MARKET buy;
- a real pair produces no order request.

Use the test fakes through the direct Python entry points; do not add a production `--fixture` flag.

- [ ] **Step 3: Run one bounded live allocation read**

On the executor checkout, run:

```bash
.venv/bin/python -m open_trader trend-allocation once \
  --date 2026-08-03 --config config/daily_premarket.env
```

Verify six exact roots, six parseable global strengths, source dates, immutable daily path, `latest.json` SHA, controller status, redacted logs, and no favorites mutation. If 2026-08-03 already has a referenced snapshot, the command must return the identical file or fail closed rather than overwrite it.

- [ ] **Step 4: Generate the three live reports and inspect the shared parameter**

Let the dedicated task and three market controllers generate the next normal reports. Verify all three JSON artifacts contain the same daily path/SHA, correct ranks and weights, explicit generated/target dates, independent real/sim pairs, and no real order intent.

Do not force a simulated live rotation merely for acceptance. The deterministic broker-fact workflow from Step 2 proves submission behavior without creating an unrequested market trade.

- [ ] **Step 5: Update and commit the merge log before merge**

Add a concise `## 2026-08-03` operator-facing entry to `CHANGELOG.md` describing the allocation task, 6%/4%/2% new-position sizing, max-two simulated rotation behavior, real manual boundary, stale fallback, schedule change, and exact verification results.

```bash
git add CHANGELOG.md
git commit -m "docs: log trend allocation and rotation"
```

- [ ] **Step 6: Run the full repository tests before the final Dashboard gate**

```bash
make test
```

Expected: PASS. Fix every failure before continuing.

- [ ] **Step 7: Install/restart the candidate background tasks and verify fresh runtime identity**

Use the repository installer for the candidate checkout. Verify allocation plus CN/HK/US launchd labels, new PIDs, working directory, candidate Git SHA, fresh status timestamps, fresh stdout/stderr logs, and no old controller process using pre-change code.

- [ ] **Step 8: Run the one final Dashboard acceptance gate**

```bash
make acceptance
```

Expected: `PASS`. `FAIL` must be fixed and rerun. `BLOCKED` must be reported as blocked and cannot be replaced with fixtures, curl, unit tests, or screenshots.

- [ ] **Step 9: Commit any acceptance-only deterministic fix and rerun the gates**

If acceptance required a source/test fix, commit it, rerun the focused suite, `make test`, restart the candidate services, and rerun `make acceptance`. Do not amend evidence onto a different untested SHA.

- [ ] **Step 10: Merge only after the changelog commit exists**

Use the repository's normal non-interactive merge workflow. Preserve unrelated dirty-root files. Confirm `main` contains all feature commits and the dated changelog entry.

- [ ] **Step 11: Redeploy the exact accepted SHA and verify review readiness**

Redeploy the exact SHA that returned acceptance PASS. Verify:

```text
allocation PID / cwd / SHA / fresh status and log
CN controller PID / cwd / SHA / fresh status and log
HK controller PID / cwd / SHA / fresh status and log
US controller PID / cwd / SHA / fresh status and log
Dashboard frontend/backend PID / cwd / SHA / fresh logs
HTTP 200 from http://127.0.0.1:8766/
```

The post-acceptance restart needs no second acceptance run when it deploys the exact accepted SHA with no source/data mutation.

- [ ] **Step 12: Capture requested screenshots and hand off**

From `http://127.0.0.1:8766/`, open one current report containing the new fields and capture desktop plus 375px mobile screenshots from the exact deployed SHA. Provide both images, the URL, final SHA, acceptance PASS summary, live allocation snapshot path/SHA, report generated and target trade dates, controller identities, and note that simulated execution is automatic while real execution remains manual.

---

## Self-Review Checklist

- [ ] Every approved design rule has a task and runnable verification step.
- [ ] The allocation task is the sole producer and all reports freeze a daily path plus verified SHA; explicit `-rN` allocation correction moves all three reports together before execution lock.
- [ ] All six roots are required; failure reuses one whole prior ranking and trading does not freeze.
- [ ] Cold start preserves legacy 4% behavior without claiming allocation readiness.
- [ ] Rank sizing changes new positions only and never enforces 60%/40%/20% as caps.
- [ ] Favorites and report recommendations share the existing eligibility and mapping pipeline.
- [ ] Rotation occurs only at ten occupied slots, at inclusive 20 points, and at most two reserved pairs per account/date across revisions.
- [ ] Real and simulated pairs are independently sized and clearly labeled.
- [ ] Simulated buy is impossible before proven full sell fill; real submit is unreachable.
- [ ] Retry/restart/date boundaries are deterministic and idempotent.
- [ ] v11/v9 continue Kelly, drawdown, and lifecycle history without reset.
- [ ] Markdown, Feishu, Dashboard, replay, delivery receipts, and execution consume the same frozen fields.
- [ ] The UI follows the approved hierarchy on desktop and 375px mobile.
- [ ] No deferred marker, new dependency, speculative abstraction, or duplicate engine remains.
- [ ] Changelog precedes merge; final acceptance and exact-SHA deployment evidence are included.
