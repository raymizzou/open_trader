# Stale Trend Component Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ignore individually stale `getComponentTicker` rows while retaining current-date rows, disclose every ignored symbol/date in report audit facts, and regenerate the 2026-07-16 Futu US trend report.

**Architecture:** Put date partitioning in the existing shared `TrendAnimalsClient` so CN, US, and HK use one rule. Expose immutable ignored-row facts on the client, then reuse one report-fact formatter from both report runners. Keep all snapshot and price-date checks strict.

**Tech Stack:** Python 3.12 stdlib (`datetime.date`), pytest, existing Trend Animals client/report builders, launchd, screen, dashboard acceptance runner.

## Global Constraints

- Only `getComponentTicker` may ignore rows older than the expected report date.
- Missing, invalid, or future `asOfDate` values remain errors.
- A response with no current-date component rows remains an error.
- Mixed-date component responses are not cached as complete current-date responses.
- `getTickerSnapshot` and all price/trading data retain exact-date validation.
- Ignored rows appear as `忽略旧成分 N 条：SYMBOL（YYYY-MM-DD）` in report data audit.
- No new dependency, configuration value, threshold, or notification channel.
- Preserve the existing sent daily Feishu ledger and the user's unrelated untracked files.
- Run `make acceptance` after all modifications; only `PASS` is complete.

---

### Task 1: Partition stale component rows in the shared client

**Files:**
- Modify: `src/open_trader/trend_animals.py:1-240`
- Test: `tests/test_trend_animals.py`

**Interfaces:**
- Consumes: `TrendAnimalsClient.get_components(*, tm_id: int, expected_date: str) -> list[dict[str, object]]`
- Produces: `TrendAnimalsClient.ignored_stale_components -> tuple[dict[str, str], ...]`
- Preserves: strict behavior of `get_snapshots()` and `_cached_rows()` unless `ignore_older=True` is passed by `get_components()`.

- [ ] **Step 1: Write the failing mixed-response test**

Add a test that uses the real client and fake transport:

```python
def test_components_ignore_older_rows_and_do_not_cache_mixed_response(tmp_path: Path) -> None:
    client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=FakeTransport({
            "getComponentTicker": success([
                {"tmId": 1, "tickerSymbol": "NVDA", "asOfDate": "2026-07-15"},
                {"tmId": 2, "tickerSymbol": "NUVL", "asOfDate": "2026-07-14"},
            ])
        }),
    )

    rows = client.get_components(tm_id=622460, expected_date="2026-07-15")

    assert rows == [
        {"tmId": 1, "tickerSymbol": "NVDA", "asOfDate": "2026-07-15"}
    ]
    assert client.ignored_stale_components == (
        {"tickerSymbol": "NUVL", "asOfDate": "2026-07-14"},
    )
    assert not list((tmp_path / "responses").glob("*.json"))
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_trend_animals.py::test_components_ignore_older_rows_and_do_not_cache_mixed_response
```

Expected: FAIL because the current client raises `getComponentTicker returned data for '2026-07-14'`.

- [ ] **Step 3: Add strict boundary tests before production code**

Add one parameterized test for all-old, future, invalid, and missing dates. All-old must raise an error matching `no current-date rows`; the other cases must retain the existing `returned data for` failure.

```python
@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([{"tmId": 2, "tickerSymbol": "NUVL", "asOfDate": "2026-07-14"}], "no current-date rows"),
        ([{"tmId": 2, "tickerSymbol": "NUVL", "asOfDate": "2026-07-16"}], "returned data for"),
        ([{"tmId": 2, "tickerSymbol": "NUVL", "asOfDate": "not-a-date"}], "returned data for"),
        ([{"tmId": 2, "tickerSymbol": "NUVL"}], "returned data for"),
    ],
)
def test_components_reject_unusable_date_sets(
    rows: list[dict[str, object]], message: str, tmp_path: Path
) -> None:
    client = TrendAnimalsClient(
        api_key="secret-value",
        cache_dir=tmp_path,
        transport=FakeTransport({"getComponentTicker": success(rows)}),
    )

    with pytest.raises(TrendAnimalsError, match=message):
        client.get_components(tm_id=622460, expected_date="2026-07-15")
```

- [ ] **Step 4: Implement the minimum shared partition**

Import `date`, initialize `_ignored_stale_components`, expose a defensive tuple property, call `_cached_rows(..., ignore_older=True)` only from `get_components()`, and extend `_cached_rows()` with an internal keyword:

```python
from datetime import date

self._ignored_stale_components: list[dict[str, str]] = []

@property
def ignored_stale_components(self) -> tuple[dict[str, str], ...]:
    return tuple(dict(row) for row in self._ignored_stale_components)

def _cached_rows(
    self,
    endpoint: str,
    params: Mapping[str, str],
    expected_date: str,
    *,
    ignore_older: bool = False,
) -> list[dict[str, object]]:
    cache_identity = {
        "date": expected_date,
        "endpoint": endpoint,
        "params": dict(sorted(params.items())),
    }
    digest = hashlib.sha256(
        json.dumps(
            cache_identity, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    cache_path = self.cache_dir / "responses" / f"{digest}.json"
    cached = self._read_cache(cache_path)
    self._paid_cache_events.append(
        {"endpoint": endpoint, "cache": "hit" if cached is not None else "miss"}
    )
    if cached is not None:
        if not isinstance(cached, list) or any(
            not isinstance(row, dict) or not _is_json_value(row) for row in cached
        ):
            raise TrendAnimalsError("response cache has an invalid shape")
        rows = list(cached)
    else:
        rows = self._get(endpoint, params)
    if self._contains_secret(rows):
        raise TrendAnimalsError(f"{endpoint} returned unsafe data")
    current_rows: list[dict[str, object]] = []
    ignored_rows: list[dict[str, str]] = []
    expected_day = date.fromisoformat(expected_date)
    for row in rows:
        actual_date = row.get("asOfDate")
        if actual_date == expected_date:
            current_rows.append(row)
            continue
        try:
            actual_day = date.fromisoformat(actual_date) if isinstance(actual_date, str) else None
        except ValueError:
            actual_day = None
        symbol = row.get("tickerSymbol")
        if (
            ignore_older
            and actual_day is not None
            and actual_day < expected_day
            and isinstance(symbol, str)
            and symbol.strip()
        ):
            ignored_rows.append({"tickerSymbol": symbol.strip(), "asOfDate": actual_date})
            continue
        safe_actual = self._redact(actual_date) if isinstance(actual_date, str) else actual_date
        raise TrendAnimalsError(
            f"{endpoint} returned data for {safe_actual!r}; "
            f"expected {self._redact(expected_date)}"
        )
    if ignore_older and not current_rows:
        raise TrendAnimalsError(f"{endpoint} returned no current-date rows")
    self._ignored_stale_components.extend(ignored_rows)
    if cached is None and not ignored_rows:
        self._write_cache(cache_path, current_rows)
    return current_rows
```

Change `get_components()` to pass `ignore_older=True`; leave `get_snapshots()` unchanged so it uses the default `False`.

- [ ] **Step 5: Verify GREEN and snapshot strictness**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_trend_animals.py
```

Expected: all Trend Animals tests PASS, including existing snapshot exact-date and secret-redaction tests.

- [ ] **Step 6: Commit the client discipline**

```bash
git add src/open_trader/trend_animals.py tests/test_trend_animals.py
git commit -m "fix: ignore individually stale trend components"
```

---

### Task 2: Disclose ignored components in every report

**Files:**
- Modify: `src/open_trader/a_share_trend.py:1085-1110,2390-2430`
- Modify: `src/open_trader/market_trend.py:585-610`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Consumes: `api.ignored_stale_components`, defaulting to an empty tuple for existing fakes.
- Produces: `_component_api_facts(api: object, row_count: int) -> tuple[str, ...]` shared by CN and US/HK report builders.
- Produces audit copy: `忽略旧成分 N 条：SYMBOL（YYYY-MM-DD）`.

- [ ] **Step 1: Write failing shared formatting and runner tests**

Extend the markdown fact test with this raw fact and assertion:

```python
api_facts=(
    "getComponentTicker rows=39 cache=client-managed",
    "忽略旧成分 1 条：NUVL（2026-07-14）",
)
assert "忽略旧成分 1 条：NUVL（2026-07-14）" in markdown
```

In one A-share report-run fake and one market report-run fake, expose:

```python
ignored_stale_components = (
    {"tickerSymbol": "NUVL", "asOfDate": "2026-07-14"},
)
```

Assert each frozen JSON payload contains the exact audit fact. The market runner covers both US/HK because they share `_attempt_market_report`; the A-share runner covers CN.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py::test_markdown_translates_exclusion_and_api_facts_without_paths \
  tests/test_market_trend.py
```

Expected: at least one assertion FAILS because ignored components are not yet added to `api_facts` or rendered verbatim.

- [ ] **Step 3: Add one shared fact formatter and reuse it**

Add to `a_share_trend.py`:

```python
def _component_api_facts(api: object, row_count: int) -> tuple[str, ...]:
    facts = [f"getComponentTicker rows={row_count} cache=client-managed"]
    ignored = tuple(getattr(api, "ignored_stale_components", ()))
    if ignored:
        details = "、".join(
            f"{row['tickerSymbol']}（{row['asOfDate']}）" for row in ignored
        )
        facts.append(f"忽略旧成分 {len(ignored)} 条：{details}")
    return tuple(facts)
```

Allow the already-rendered safe fact through `_api_fact_label()`:

```python
if value.startswith("忽略旧成分 "):
    return value
```

Replace the single component fact in the A-share `api_facts` tuple with `*_component_api_facts(api, len(component_rows))`. Import `_component_api_facts` in `market_trend.py` and make the same replacement there. Do not create market-specific variants.

- [ ] **Step 4: Verify focused and full automated tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_trend_animals.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py
.venv/bin/python -m pytest -q
```

Expected: all focused tests and the full suite PASS.

- [ ] **Step 5: Commit report disclosure**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py \
  tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "feat: disclose ignored stale trend components"
```

---

### Task 3: Regenerate, deploy, and accept the live workflow

**Files:**
- Runtime artifact: `reports/trend_us_futu/2026-07-15.json`
- Runtime artifact: `reports/trend_us_futu/2026-07-15.md`
- Inspect without changing: `data/trend_us_futu/daily_delivery/2026-07-16.json`
- Inspect: `data/trend_us_futu/run.log`
- Inspect: `/tmp/open_trader_dashboard_8766.log`

**Interfaces:**
- Consumes: committed source SHA, real Trend Animals API, Futu OpenD, existing sent daily ledger.
- Produces: frozen US/Futu report with `NUVL（2026-07-14）` in audit facts and no duplicate Feishu message.

- [ ] **Step 1: Record immutable live state**

```bash
git status --short
git rev-parse HEAD
shasum -a 256 data/trend_us_futu/daily_delivery/2026-07-16.json
```

Expected: only the user's three pre-existing untracked files are unrelated; record the ledger hash for comparison.

- [ ] **Step 2: Run the real report workflow directly**

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-market-report \
  --market US --date 2026-07-16 --config config/daily_premarket.env
```

Expected: JSON result status `generated`; canonical report paths use `2026-07-15`; no new Feishu success message replaces the already-sent failure ledger.

- [ ] **Step 3: Validate the generated report and immutable ledger**

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path

report = json.loads(Path("reports/trend_us_futu/2026-07-15.json").read_text())
assert report["as_of_date"] == "2026-07-15"
assert report["execution_date"] == "2026-07-16"
assert "忽略旧成分 1 条：NUVL（2026-07-14）" in report["api_facts"]
assert report["metadata"]["broker"] == "futu"
print(report["metadata"]["process_version"])
PY
shasum -a 256 data/trend_us_futu/daily_delivery/2026-07-16.json
tail -n 20 data/trend_us_futu/run.log
```

Expected: process version equals `git rev-parse HEAD`; ledger hash is unchanged; fresh log ends with `generated`.

- [ ] **Step 4: Restart the US watcher on committed code**

```bash
launchctl kickstart -k gui/$(id -u)/com.open-trader.trend-us-watch
launchctl print gui/$(id -u)/com.open-trader.trend-us-watch
```

Verify a new PID, working directory `/Users/ray/projects/open_trader`, current Git SHA, and fresh watcher logs. Confirm the daily ledger hash remains unchanged.

- [ ] **Step 5: Deploy the candidate dashboard before acceptance**

Stop only screen session `open_trader_dashboard_8766`, ensure its old listener exits, clear `/tmp/open_trader_dashboard_8766.log`, and start the existing dashboard command from `/Users/ray/projects/open_trader` with `PYTHONPATH=src`, real `data`, and real `reports`. Verify the new PID, cwd, current SHA, fresh log, and HTTP 200 from `http://127.0.0.1:8766/`.

- [ ] **Step 6: Run the final acceptance gate**

```bash
DASHBOARD_LOG=/tmp/open_trader_dashboard_8766.log make acceptance
```

Expected final output:

```json
{"status": "PASS", "errors": [], "blocker": null}
```

On `FAIL`, diagnose and fix, then rerun. On `BLOCKED`, report the external blocker without substituting mocks, curl, or screenshots.

- [ ] **Step 7: Redeploy the exact accepted SHA**

After `PASS`, restart screen session `open_trader_dashboard_8766` once more without source or data changes. Verify the new PID, cwd, exact accepted SHA, fresh error-free log, and HTTP 200 for both `/` and `/api/dashboard`. Provide `http://127.0.0.1:8766/` for review.
