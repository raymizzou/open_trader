# Trend ETF Candidate Pools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Include eligible mainland-China, US, and Hong Kong ETFs in the existing Trend Animals trend-selection workflow without changing historical replay results.

**Architecture:** Keep the existing candidate evaluation, ranking, sizing, and risk modules. CN passes the frozen strategy version into the existing asset gate so only current v9 accepts `ETF基金`; US adds a fixed pool through the existing multi-pool configuration; HK resolves the current `温转热(香港ETF)` child beneath stable root `707617` before using the same multi-pool loader.

**Tech Stack:** Python 3.12, pytest, existing `TrendAnimalsClient`, Futu market/account clients, launchd.

## Global Constraints

- Start and remain on worktree `/Users/ray/projects/open_trader/.worktrees/trend-etf-candidate-pools`, branch `feat/trend-etf-candidate-pools`, based on local `main`.
- Add no dependency, module, selector, scoring model, or ETF-specific risk rule.
- CN ETFs keep every current CN v9 strength, market-cap, turnover, industry, phase, ATR, sizing, and risk gate.
- US and HK ETFs keep every current US/HK v6 strength, right-side-days, turnover, ATR, sizing, and risk gate.
- Current CN v9, US v6, and HK v6 remain effective from `2026-07-27`; do not create new strategy versions.
- CN v8 and earlier historical evidence must continue to exclude `ETF基金`.
- HK missing `温转热(香港ETF)` means zero ETF candidates and must not block the report.
- ETF update dates must match the report data date; stale or malformed supplier data fails closed.
- Never print or persist `TREND_ANIMALS_API_KEY`.
- Do not run `make acceptance`; this is not a Dashboard task.

## File Map

- `src/open_trader/a_share_trend.py`: version-aware CN asset permission and propagation through candidate ordering, industry context, report serialization, and historical replay.
- `tests/test_a_share_trend.py`: CN current-versus-historical ETF behavior.
- `src/open_trader/market_trend.py`: US/HK ETF readiness, stable HK root resolution, candidate loading, request accounting, and audit facts.
- `tests/test_market_trend.py`: readiness and HK dynamic-pool behavior, plus updated complete supplier fixtures.
- `config/daily_premarket.env.example`: official US and HK candidate pool configuration.
- `CHANGELOG.md`: dated operator-facing entry required before merge.
- `config/daily_premarket.env` (ignored live file, after merge): deployed US/HK pool IDs.

---

### Task 1: Admit CN ETFs Only Under Current v9

**Files:**
- Modify: `src/open_trader/a_share_trend.py:625-668`
- Modify: `src/open_trader/a_share_trend.py:1557-1623`
- Modify: `src/open_trader/a_share_trend.py:1818-1858`
- Modify: `src/open_trader/a_share_trend.py:1861-1995`
- Modify: `src/open_trader/a_share_trend.py:2631-3110`
- Modify: `src/open_trader/a_share_trend.py:5018-5315`
- Test: `tests/test_a_share_trend.py:790-845`
- Test: `tests/test_a_share_trend.py:1442-1537`

**Interfaces:**
- Consumes: frozen `strategy_snapshot["strategy_version"]`.
- Produces: `_candidate_reasons(..., strategy_version: str | None = None)`, `build_candidate_list(..., strategy_version: str | None = None)`, and `collect_industry_contexts(..., strategy_version: str | None = None)`.
- Preserves: omitting `strategy_version` keeps legacy CN stock-only behavior.

- [ ] **Step 1: Write the failing current-versus-historical report test**

Add a report-level regression test so the strategy version must reach the real candidate decision, rather than only testing a helper:

```python
def test_cn_v9_accepts_etf_without_rewriting_v8() -> None:
    item = candidate("511020", asset="ETF基金")

    def build(version: str) -> TrendReport:
        snapshot = trend_module.live_trend_strategy_snapshot(
            "CN",
            "abc123",
            (622466, 697199),
            strategy_version=version,
        )
        return build_report(
            as_of_date="2026-07-14",
            execution_date="2026-07-15",
            account=account(),
            candidates=(item,),
            holding_snapshots={},
            bars_by_symbol={},
            process_version="abc123",
            candidate_pool_ids=(622466, 697199),
            strategy_snapshot=snapshot,
        )

    current = build("v9")
    historical = build("v8")

    assert [candidate.symbol for candidate in current.candidates] == ["511020"]
    assert "511020" not in current.excluded
    assert historical.candidates == ()
    assert historical.excluded["511020"] == ["a_share_only"]
```

Also extend the existing current-v9 snapshot assertion:

```python
assert snapshot["parameters"]["allowed_assets"] == ["A股", "ETF基金"]
assert {
    row["value"]
    for row in snapshot["parameter_rows"]
    if row["name"] == "交易市场"
} == {"沪深 A 股及境内 ETF；排除北交所、ST、*ST 和退市标记"}
```

The production mutation these tests catch is either globally allowing ETFs and corrupting v8 replay, or failing to allow them in v9.

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_cn_v9_accepts_etf_without_rewriting_v8 -q
```

Expected: FAIL because the v9 report still excludes `ETF基金` as `a_share_only`.

- [ ] **Step 3: Add the minimal version-aware asset gate**

In `live_trend_strategy_snapshot`, amend only CN v9:

```python
if market == "CN" and version == "v9":
    parameters["allowed_assets"] = ["A股", "ETF基金"]
    for row in rows:
        if row["name"] == "交易市场":
            row["value"] = "沪深 A 股及境内 ETF；排除北交所、ST、*ST 和退市标记"
```

Extend `_candidate_reasons` and `build_candidate_list`:

```python
def _candidate_reasons(
    item: CandidateInput,
    held_symbols: set[str],
    expected_date: str | None = None,
    *,
    market: str = "CN",
    strategy_version: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    if market == "CN":
        allowed_assets = (
            {"A股", "ETF基金"} if strategy_version == "v9" else {"A股"}
        )
        if item.asset not in allowed_assets:
            reasons.append("a_share_only")
```

```python
def build_candidate_list(
    rows: Sequence[CandidateInput],
    *,
    held_symbols: set[str],
    expected_date: str | None = None,
    market: str = "CN",
    industry_contexts: Mapping[int, IndustryContext] | None = None,
    strategy_version: str | None = None,
) -> CandidateDecision:
```

Pass `strategy_version` into every internal `_candidate_reasons` call.

- [ ] **Step 4: Propagate the frozen version through report and industry context**

Add `strategy_version` to `collect_industry_contexts` and both internal
`build_candidate_list` calls. In `build_report`, pass the already-resolved
`snapshot_version` to candidate ordering and candidate-signal exclusion
serialization:

```python
candidate_decision = build_candidate_list(
    candidates,
    held_symbols=held_symbols,
    expected_date=as_of_date,
    market=market,
    industry_contexts=industry_context_map,
    strategy_version=snapshot_version,
)
```

```python
"excluded_reasons": _candidate_reasons(
    item,
    held_symbols,
    as_of_date,
    market=market,
    strategy_version=snapshot_version,
),
```

In `_attempt_report`, construct the already-required live strategy snapshot
immediately after `candidate_pool_ids` and before industry-context collection:

```python
strategy_snapshot = live_trend_strategy_snapshot(
    "CN",
    process_version,
    candidate_pool_ids,
    execution_date=execution_date,
)
strategy_version = str(strategy_snapshot["strategy_version"])
```

Pass `strategy_version` to `collect_industry_contexts`, and reuse this snapshot
later rather than constructing a second one.

- [ ] **Step 5: Verify GREEN and historical replay**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_cn_v9_accepts_etf_without_rewriting_v8 \
  tests/test_a_share_trend.py::test_build_report_upgrades_exact_repository_legacy_snapshot \
  tests/test_trend_review.py::test_repository_legacy_snapshots_adapt_without_rewrite -q
```

Expected: `5 passed`.

Then run:

```bash
.venv/bin/python -m pytest tests/test_a_share_trend.py -q
```

Expected: PASS with zero failures.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "fix: admit CN ETFs in current trend selection"
```

---

### Task 2: Load US ETF Pool and Resolve the Dynamic HK ETF Pool

**Files:**
- Modify: `src/open_trader/market_trend.py:64-84`
- Modify: `src/open_trader/market_trend.py:492-504`
- Modify: `src/open_trader/market_trend.py:850-1030`
- Modify: `src/open_trader/market_trend.py:1052-1140`
- Test: `tests/test_market_trend.py:16-35`
- Test: `tests/test_market_trend.py:580-590`
- Test: `tests/test_market_trend.py:680-920`
- Test: `tests/test_market_trend.py:1029-1248`

**Interfaces:**
- Consumes: `TrendAnimalsClient.get_components(tm_id: int, expected_date: str)`.
- Produces: `_candidate_pool_components(api: object, *, market: str, pool_id: int, expected_date: str) -> tuple[list[Mapping[str, object]], int | None]`.
- Stable IDs: US ETF `705013`; HK ETF root `707617`.
- Dynamic exact name: `温转热(香港ETF)`.

- [ ] **Step 1: Write failing readiness tests**

Replace the base-asset-only test with literal complete and incomplete cases:

```python
@pytest.mark.parametrize(
    ("market", "as_of_date", "rows"),
    [
        (
            "US",
            "2026-07-24",
            [
                {"asset": "美股", "asOfDate": "2026-07-24"},
                {"asset": "美国ETF", "asOfDate": "2026-07-24"},
            ],
        ),
        (
            "HK",
            "2026-07-24",
            [
                {"asset": "港股", "asOfDate": "2026-07-24"},
                {"asset": "香港ETF", "asOfDate": "2026-07-24"},
            ],
        ),
    ],
)
def test_updates_ready_requires_stock_and_etf_dates(
    market: str,
    as_of_date: str,
    rows: list[dict[str, object]],
) -> None:
    assert updates_ready(rows, market=market, as_of_date=as_of_date) is True
    assert updates_ready(rows[:-1], market=market, as_of_date=as_of_date) is False
    rows[-1]["asOfDate"] = "2026-07-23"
    assert updates_ready(rows, market=market, as_of_date=as_of_date) is False
```

The mutation caught is accepting a report after only stock data updates.

- [ ] **Step 2: Write failing HK dynamic-pool tests**

Import `_candidate_pool_components` and `TrendAnimalsError`, then add:

```python
def test_hk_etf_root_missing_warm_to_hot_is_empty() -> None:
    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            assert (tm_id, expected_date) == (707617, "2026-07-24")
            return [{
                "tmId": 707815,
                "tickerName": "行业趋势龙头(香港ETF)",
                "asOfDate": expected_date,
            }]

    assert _candidate_pool_components(
        Api(),
        market="HK",
        pool_id=707617,
        expected_date="2026-07-24",
    ) == ([], None)
```

```python
def test_hk_etf_root_loads_unique_warm_to_hot_child() -> None:
    security = {
        "tmId": 708001,
        "tickerSymbol": "02800.HK",
        "asOfDate": "2026-07-24",
    }

    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            return {
                707617: [{
                    "tmId": 707900,
                    "tickerName": "温转热(香港ETF)",
                    "asOfDate": expected_date,
                }],
                707900: [security],
            }[tm_id]

    assert _candidate_pool_components(
        Api(),
        market="HK",
        pool_id=707617,
        expected_date="2026-07-24",
    ) == ([security], 707900)
```

```python
def test_hk_etf_root_rejects_duplicate_warm_to_hot_children() -> None:
    class Api:
        def get_components(
            self, *, tm_id: int, expected_date: str
        ) -> list[dict[str, object]]:
            return [
                {
                    "tmId": child_id,
                    "tickerName": "温转热(香港ETF)",
                    "asOfDate": expected_date,
                }
                for child_id in (707900, 707901)
            ]

    with pytest.raises(
        TrendAnimalsError,
        match="HK ETF warm-to-hot pool is not unique",
    ):
        _candidate_pool_components(
            Api(),
            market="HK",
            pool_id=707617,
            expected_date="2026-07-24",
        )
```

These tests catch treating the stable root's child combinations as securities,
blocking on a normal empty day, or guessing between duplicate supplier rows.

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_market_trend.py::test_updates_ready_requires_stock_and_etf_dates \
  tests/test_market_trend.py::test_hk_etf_root_missing_warm_to_hot_is_empty \
  tests/test_market_trend.py::test_hk_etf_root_loads_unique_warm_to_hot_child \
  tests/test_market_trend.py::test_hk_etf_root_rejects_duplicate_warm_to_hot_children -q
```

Expected: collection/error failures because `_candidate_pool_components` does not
exist and readiness still accepts one asset.

- [ ] **Step 4: Implement ETF readiness and the minimum resolver**

Import `TrendAnimalsError` and add:

```python
MARKET_UPDATE_ASSETS = {
    "US": ("美股", "美国ETF"),
    "HK": ("港股", "香港ETF"),
}
HK_ETF_ROOT_TM_ID = 707617
HK_ETF_WARM_TO_HOT_NAME = "温转热(香港ETF)"
```

Replace `updates_ready` with:

```python
def updates_ready(
    rows: Sequence[Mapping[str, object]], *, market: str, as_of_date: str
) -> bool:
    required = MARKET_UPDATE_ASSETS[_market(market)]
    dates = {
        str(row.get("asset")): _status_date(row)
        for row in rows
        if row.get("asset") in required
    }
    return all(dates.get(asset) == as_of_date for asset in required)
```

Add:

```python
def _candidate_pool_components(
    api: object,
    *,
    market: str,
    pool_id: int,
    expected_date: str,
) -> tuple[list[Mapping[str, object]], int | None]:
    rows = api.get_components(  # type: ignore[attr-defined]
        tm_id=pool_id,
        expected_date=expected_date,
    )
    if market != "HK" or pool_id != HK_ETF_ROOT_TM_ID:
        return list(rows), pool_id
    matches = [
        row for row in rows
        if row.get("tickerName") == HK_ETF_WARM_TO_HOT_NAME
    ]
    if not matches:
        return [], None
    if len(matches) != 1:
        raise TrendAnimalsError("HK ETF warm-to-hot pool is not unique")
    resolved_id = _row_tm_id(matches[0])
    return (
        list(api.get_components(  # type: ignore[attr-defined]
            tm_id=resolved_id,
            expected_date=expected_date,
        )),
        resolved_id,
    )
```

- [ ] **Step 5: Route configured pools through the resolver and audit HK resolution**

Replace the direct candidate component call inside `_attempt_market_report`:

```python
component_rows: list[Mapping[str, object]] = []
component_pools: defaultdict[int, set[str]] = defaultdict(set)
pool_resolution_facts: list[str] = []
extra_component_requests = 0
for pool_id in pool_ids:
    rows, resolved_pool_id = _candidate_pool_components(
        api,
        market=market,
        pool_id=pool_id,
        expected_date=as_of_date,
    )
    if market == "HK" and pool_id == HK_ETF_ROOT_TM_ID:
        extra_component_requests += int(resolved_pool_id is not None)
        pool_resolution_facts.append(
            "getComponentTicker configured_pool=707617 "
            f"resolved_pool={resolved_pool_id or 'none'}"
        )
    component_rows.extend(rows)
    for row in rows:
        component_pools[_row_tm_id(row)].add(str(pool_id))
```

Include `extra_component_requests` in cache-event accounting:

```python
expected_component_requests = (
    len(pool_ids)
    + extra_component_requests
    + int(industry_facts["component_requests"])
)
```

Append `*pool_resolution_facts` to the report's `api_facts`. Keep
`candidate_pool_ids=pool_ids` and per-candidate `pools` tied to stable configured
ID `707617`; the resolved dynamic ID belongs only in audit facts.

- [ ] **Step 6: Complete existing external-market fixtures**

Every fake `get_update_status` in `tests/test_market_trend.py` must mirror the
real complete response:

```python
return [
    {"asset": "港股", "asOfDate": "2026-07-15"},
    {"asset": "香港ETF", "asOfDate": "2026-07-15"},
]
```

and:

```python
return [
    {"asset": "美股", "asOfDate": "2026-07-14"},
    {"asset": "美国ETF", "asOfDate": "2026-07-14"},
]
```

Do not weaken `updates_ready` to keep partial mocks passing.

- [ ] **Step 7: Verify GREEN and the full external-market flow**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_market_trend.py::test_updates_ready_requires_stock_and_etf_dates \
  tests/test_market_trend.py::test_hk_etf_root_missing_warm_to_hot_is_empty \
  tests/test_market_trend.py::test_hk_etf_root_loads_unique_warm_to_hot_child \
  tests/test_market_trend.py::test_hk_etf_root_rejects_duplicate_warm_to_hot_children -q
```

Expected: `5 passed` because the readiness test is parametrized twice.

Then run:

```bash
.venv/bin/python -m pytest tests/test_market_trend.py -q
```

Expected: PASS with zero failures.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/open_trader/market_trend.py tests/test_market_trend.py
git commit -m "feat: load US and HK ETF trend pools"
```

---

### Task 3: Configure the Official Pools and Verify the Branch

**Files:**
- Modify: `config/daily_premarket.env.example:44-48`
- Modify: `CHANGELOG.md:6-26`
- Test: `tests/test_daily_premarket.py:70-115`

**Interfaces:**
- Produces configured stable pool lists `(622460, 705013)` for US and `(622494, 707617)` for HK.
- Does not modify the ignored live `config/daily_premarket.env` until the accepted branch is merged.

- [ ] **Step 1: Update the configuration parser fixture**

Use the real official IDs in the existing environment parsing test:

```text
TREND_ANIMALS_WARM_TO_HOT_US_TM_IDS=622460,705013
TREND_ANIMALS_WARM_TO_HOT_HK_TM_IDS=622494,707617
```

and assert:

```python
assert config.trend_animals_us_tm_ids == (622460, 705013)
assert config.trend_animals_hk_tm_ids == (622494, 707617)
```

- [ ] **Step 2: Run the parser test before changing the example**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_daily_premarket.py::test_load_env_config_parses_required_values_and_executor_host -q
```

Expected: PASS; this characterizes the already-existing multi-ID parser and
proves no parser implementation is needed.

- [ ] **Step 3: Update the checked-in example**

Change only:

```text
TREND_ANIMALS_WARM_TO_HOT_US_TM_IDS=622460,705013
TREND_ANIMALS_WARM_TO_HOT_HK_TM_IDS=622494,707617
```

- [ ] **Step 4: Run focused suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_daily_premarket.py \
  tests/test_trend_review.py -q
```

Expected: PASS with zero failures.

- [ ] **Step 5: Run the full automated gate**

Run:

```bash
make test
```

Expected: all tests pass with zero failures. Record the exact total and elapsed
time for the final report.

- [ ] **Step 6: Run the real supplier API check without secrets**

Link the ignored live config into the worktree for this read-only check:

```bash
ln -s ../../../config/daily_premarket.env config/daily_premarket.env
```

Run:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path

from open_trader.daily_premarket import load_env_config
from open_trader.market_trend import _candidate_pool_components
from open_trader.trend_animals import TrendAnimalsClient

config = load_env_config(Path("config/daily_premarket.env"))
api = TrendAnimalsClient(
    api_key=config.trend_animals_api_key,
    cache_dir=config.data_dir / "trend_animals/cache",
)
dates = {
    row["asset"]: row["asOfDate"]
    for row in api.get_update_status()
    if row.get("asset") in {"美国ETF", "香港ETF"}
}
us_rows, us_pool = _candidate_pool_components(
    api,
    market="US",
    pool_id=705013,
    expected_date=dates["美国ETF"],
)
hk_rows, hk_pool = _candidate_pool_components(
    api,
    market="HK",
    pool_id=707617,
    expected_date=dates["香港ETF"],
)
print({
    "US": {"date": dates["美国ETF"], "resolved_pool": us_pool, "count": len(us_rows)},
    "HK": {"date": dates["香港ETF"], "resolved_pool": hk_pool, "count": len(hk_rows)},
})
PY
```

Expected on an HK zero-match day: US resolves to `705013` with a nonnegative
count; HK resolves to `None` with count `0`. A later HK match may instead return
one child `tmId` and a nonnegative count. The output must contain no API key.

- [ ] **Step 7: Add the merge-gate changelog entry**

Under `## 2026-07-25`, add:

```markdown
- Expanded trend selection to mainland-China, US, and Hong Kong ETFs: CN v9
  now admits eligible ETF-fund candidates while preserving historical replay;
  US loads the fixed ETF warm-to-hot pool; HK resolves its dynamic warm-to-hot
  child from the stable ETF root and treats no match as an empty candidate set.
  Verified focused/full tests and the live supplier pool resolution.
```

- [ ] **Step 8: Commit Task 3**

```bash
git add config/daily_premarket.env.example tests/test_daily_premarket.py CHANGELOG.md
git commit -m "docs: configure trend ETF candidate pools"
```

---

### Task 4: Merge, Deploy the Live Configuration, and Verify Fresh Controllers

**Files:**
- Modify after merge (ignored operational file): `/Users/ray/projects/open_trader/config/daily_premarket.env`
- Inspect: `/Users/ray/projects/open_trader/logs/daily_premarket/launchd-trend-controller-*.out.log`
- Inspect: `/Users/ray/projects/open_trader/logs/daily_premarket/launchd-trend-controller-*.err.log`

**Interfaces:**
- Consumes: the verified, changelog-bearing branch SHA.
- Produces: live `main` configuration and fresh CN/HK/US controller processes using the merged SHA.

- [ ] **Step 1: Inspect branch state before integration**

Run:

```bash
git status --short --branch
git log --oneline --decorate -5
git diff main...HEAD --check
```

Expected: clean branch, the design and implementation commits visible, and no
diff-check errors.

- [ ] **Step 2: Invoke the branch-finishing workflow**

Use `superpowers:finishing-a-development-branch`. Merge only after confirming
the dated `CHANGELOG.md` entry is part of the branch. If the user chooses not to
merge, stop here and report that live behavior has not changed.

- [ ] **Step 3: Update the ignored live configuration after merge**

In `/Users/ray/projects/open_trader/config/daily_premarket.env`, use
`apply_patch` to change only:

```text
TREND_ANIMALS_WARM_TO_HOT_US_TM_IDS=622460,705013
TREND_ANIMALS_WARM_TO_HOT_HK_TM_IDS=622494,707617
```

Verify without printing secrets:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
from open_trader.daily_premarket import load_env_config

config = load_env_config(Path("config/daily_premarket.env"))
print(config.trend_animals_us_tm_ids)
print(config.trend_animals_hk_tm_ids)
PY
```

Expected:

```text
(622460, 705013)
(622494, 707617)
```

- [ ] **Step 4: Inspect old controller processes before restart**

Run:

```bash
launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)'
ps -axo pid,lstart,command | rg '[o]pen_trader trend-market run'
```

Record the old CN/HK/US PIDs and command working paths.

- [ ] **Step 5: Reinstall and restart the trend controllers**

From merged `/Users/ray/projects/open_trader`:

```bash
./scripts/install_daily_premarket_launchd.sh --trend-only --market all
launchctl kickstart -k "gui/$(id -u)/com.open-trader.trend-market-controller.cn"
launchctl kickstart -k "gui/$(id -u)/com.open-trader.trend-market-controller.hk"
launchctl kickstart -k "gui/$(id -u)/com.open-trader.trend-market-controller.us"
```

- [ ] **Step 6: Verify fresh processes, SHA, and logs**

Run:

```bash
git rev-parse HEAD
launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)'
ps -axo pid,lstart,command | rg '[o]pen_trader trend-market run'
tail -n 100 logs/daily_premarket/launchd-trend-controller-cn.out.log
tail -n 100 logs/daily_premarket/launchd-trend-controller-hk.out.log
tail -n 100 logs/daily_premarket/launchd-trend-controller-us.out.log
tail -n 100 logs/daily_premarket/launchd-trend-controller-cn.err.log
tail -n 100 logs/daily_premarket/launchd-trend-controller-hk.err.log
tail -n 100 logs/daily_premarket/launchd-trend-controller-us.err.log
```

Expected:

- all three controller PIDs differ from the recorded old PIDs;
- commands use `/Users/ray/projects/open_trader`;
- the process/version evidence uses the merged `git rev-parse HEAD`;
- fresh timestamped logs show normal heartbeat/market-closed or report activity;
- no fresh traceback or stale-code path appears.

- [ ] **Step 7: Final evidence review**

Run the focused ETF tests once more from merged `main`:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_cn_v9_accepts_etf_without_rewriting_v8 \
  tests/test_market_trend.py::test_updates_ready_requires_stock_and_etf_dates \
  tests/test_market_trend.py::test_hk_etf_root_missing_warm_to_hot_is_empty \
  tests/test_market_trend.py::test_hk_etf_root_loads_unique_warm_to_hot_child \
  tests/test_market_trend.py::test_hk_etf_root_rejects_duplicate_warm_to_hot_children -q
```

Expected: `6 passed`. Only then report the ETF coverage as live.
