# Controller-Owned Dashboard Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the account-sync controller publish every monetary, price, profit, and weight field used by the Dashboard account view so the backend only maps the published projection and the browser only formats and renders it.

**Architecture:** Keep accepted broker and statement facts unchanged in `account_sync_state.json`, and add one validated `dashboard_projection` built by controller-owned code from those facts plus published quotes. Account and quote loops publish only complete projections and retain the last complete projection on failure; `dashboard.py` maps the projection into the API while continuing to use raw accepted facts for frozen trend-report overlays. The browser groups projected rows and formats labels, but performs no FX, market-value, P&L, account-weight, or portfolio-weight arithmetic.

**Tech Stack:** Python 3.12, `Decimal`, atomic JSON/CSV publication, pytest, vanilla JavaScript, Playwright, launchd, existing `make acceptance`.

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/controller-owned-dashboard-fields` on branch `fix/controller-owned-dashboard-fields`, based on local `main` SHA `4aeb1c7`.
- The approved design is `docs/superpowers/specs/2026-07-31-controller-owned-dashboard-fields-design.md`.
- Do not add a database, queue, dependency, or new file under `data/latest`.
- Preserve `brokers.*.positions`, `cash`, `fx_rates`, and `summary` as accepted source facts; quotes must never overwrite them.
- Do not change `portfolio.csv` semantics or route live projection values into strategy, execution, Kelly, trend-report hashes, or frozen real-holding facts.
- Reuse accepted source FX first; statement sources may use HKD `1`, USD `7.8`, and CNY `1.08`; live sources without accepted FX fail closed.
- Standard US options use a multiplier of `100`; all other rows use `1`.
- Statement positions never consume live quotes. Live positions use a valid accepted quote when available and otherwise retain the accepted account snapshot.
- `price_kind` is one of `live`, `overnight`, `pre_market`, `after_hours`, `statement`, or `account_snapshot`. The addition of `overnight` preserves the current US night-session label and is the only contract clarification found during plan review.
- Money, price, and quantity fields are decimal strings. Percentages are strings ending in `%`. Unknown optional facts are empty strings, never numeric zero.
- Every published non-cash position must have `market_value_hkd`, `account_weight_hkd`, and `portfolio_weight_hkd`; incomplete denominators block projection publication.
- Frontend work may change data plumbing and non-visible test selectors, but must not change account-table columns, wording, color, or responsive layout.
- Use test-driven development for each behavior task and make one focused commit per task.
- Do not run `make acceptance` during development. Run it once, as the final gate on the final committed candidate SHA.
- Before that final SHA, add the dated operator-facing `CHANGELOG.md` entry. After `PASS`, redeploy the exact accepted SHA and prove new Dashboard and controller PIDs, cwd, SHA, fresh logs, and HTTP 200.
- Capture live screenshots of the affected Tiger account and one statement account after the exact accepted SHA is redeployed.

## Baseline Evidence

The isolated worktree was checked before implementation with:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_account_sync_state.py \
  tests/test_account_sync_controller.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py -q
```

Expected and observed baseline: `555 passed in 39.66s`.

---

### Task 1: Define and validate the state-file projection contract

**Files:**

- Modify: `src/open_trader/account_sync_state.py:20-81`
- Modify: `src/open_trader/account_sync_state.py:274-311`
- Modify: `src/open_trader/account_sync_state.py:587-642`
- Test: `tests/test_account_sync_state.py:1-180`
- Test: `tests/test_account_sync_state.py:270-365`

**Interfaces:**

- Consumes: existing state version `1`, `REQUIRED_BROKERS`, raw accepted source validation, and controller heartbeat dictionaries.
- Produces: `dashboard_projection_from_state(state: Mapping[str, object]) -> dict[str, object] | None`, normalized legacy-state loading, projection schema constants, and health reasons `dashboard_projection_missing`, `account_loop_failed`, and `quote_loop_failed`.

- [ ] **Step 1: Write failing contract and migration tests**

Add these tests to `tests/test_account_sync_state.py`:

```python
def test_legacy_state_keeps_accepted_sources_and_exposes_no_projection(
    tmp_path: Path,
) -> None:
    legacy = accept_candidate(
        load_account_sync_state(tmp_path / "missing.json"),
        _candidate(),
        attempted_at="2026-07-31T08:00:00+08:00",
    )
    legacy.pop("dashboard_projection", None)
    path = tmp_path / "state.json"
    write_json_atomic(path, legacy)

    loaded = load_account_sync_state(path)

    assert loaded["brokers"]["futu"] == legacy["brokers"]["futu"]
    assert loaded["dashboard_projection"] == {}
    assert dashboard_projection_from_state(loaded) is None


def test_invalid_projection_is_dropped_without_discarding_accepted_sources(
    tmp_path: Path,
) -> None:
    accepted = accept_candidate(
        load_account_sync_state(tmp_path / "missing.json"),
        _candidate(),
        attempted_at="2026-07-31T08:00:00+08:00",
    )
    accepted["dashboard_projection"] = {
        "generated_at": "2026-07-31T08:00:00+08:00",
        "quote_as_of": "",
        "summary": {},
        "broker_summaries": [],
        "broker_positions": [{"broker": "futu"}],
        "cash_details": [],
    }
    path = tmp_path / "state.json"
    write_json_atomic(path, accepted)

    loaded = load_account_sync_state(path)

    assert loaded["brokers"]["futu"]["positions"]
    assert loaded["dashboard_projection"] == {}
    assert dashboard_projection_from_state(loaded) is None
```

Extend `test_health_is_ok_only_with_current_controller_sources_quotes_and_generation` so a generated state without a valid projection returns:

```python
assert abnormal["status"] == "abnormal"
assert abnormal["reason"] == "dashboard_projection_missing"
```

Add loop-failure cases using controller status payloads whose `account_loop` or `quote_loop` has `{"status": "failed"}` and assert the corresponding health reason.

- [ ] **Step 2: Run the new tests and verify red**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_account_sync_state.py::test_legacy_state_keeps_accepted_sources_and_exposes_no_projection \
  tests/test_account_sync_state.py::test_invalid_projection_is_dropped_without_discarding_accepted_sources \
  tests/test_account_sync_state.py::test_health_is_ok_only_with_current_controller_sources_quotes_and_generation -q
```

Expected: collection fails because `dashboard_projection_from_state` does not exist, or assertions fail because the loaded state has no normalized projection and health does not inspect it.

- [ ] **Step 3: Add the exact projection schema and legacy normalization**

Add these public constants and accessor in `account_sync_state.py`:

```python
DASHBOARD_SUMMARY_FIELDS = (
    "holding_value_hkd",
    "cash_like_value_hkd",
    "portfolio_value_hkd",
    "holding_weight_hkd",
    "cash_like_weight_hkd",
)
DASHBOARD_POSITION_FIELDS = (
    "broker", "account_alias", "market", "asset_class", "symbol", "name",
    "currency", "quantity", "cost_price", "cost_value", "last_price",
    "price_kind", "price_as_of", "market_value", "market_value_usd",
    "market_value_hkd", "cost_value_hkd", "unrealized_pnl",
    "unrealized_pnl_pct", "account_weight_hkd", "portfolio_weight_hkd",
    "statement_id", "confidence", "notes",
)
DASHBOARD_CASH_FIELDS = (
    "broker", "account_alias", "currency", "cash_balance",
    "available_balance", "cash_balance_hkd", "available_balance_hkd",
    "statement_id", "confidence", "notes",
)
PRICE_KINDS = {
    "live", "overnight", "pre_market", "after_hours",
    "statement", "account_snapshot",
}


def dashboard_projection_from_state(
    state: Mapping[str, object],
) -> dict[str, object] | None:
    projection = state.get("dashboard_projection")
    return deepcopy(projection) if _is_valid_dashboard_projection(projection) else None
```

Make `empty_account_sync_state()` include `"dashboard_projection": {}`. Keep raw-source validity independent from projection validity. After parsing a valid v1 raw state, normalize it with:

```python
normalized = deepcopy(payload)
projection = dashboard_projection_from_state(normalized)
normalized["dashboard_projection"] = projection or {}
return normalized
```

The validator must require:

- aware ISO timestamps in `generated_at`, and in `quote_as_of` when non-empty;
- a summary with the five decimal/percentage fields above plus integer `holding_count` and `broker_count`;
- exactly four broker summaries, in `REQUIRED_BROKERS` order, each with `broker`, `label`, `source_kind`, boolean `detail_available`, three HKD totals, and integer `holding_count`;
- every position key in `DASHBOARD_POSITION_FIELDS`, string values, an allowed `price_kind`, and non-empty mandatory HKD/weight fields;
- every cash key in `DASHBOARD_CASH_FIELDS`, with string values;
- only lists of mappings for `broker_summaries`, `broker_positions`, and `cash_details`.

Do not make an invalid projection invalidate or erase accepted broker facts.

- [ ] **Step 4: Make projection health explicit**

Extend `_project_controller_status()` to expose shallow copies of `account_loop` and `quote_loop`. In `project_account_sync_health()`, check in this order:

```python
if controller["status"] != "ok":
    reason = f"controller_{controller['status']}"
elif controller["account_loop"].get("status") in {"failed", "publication_failed"}:
    reason = "account_loop_failed"
elif controller["quote_loop"].get("status") in {"failed", "publication_failed"}:
    reason = "quote_loop_failed"
# existing per-broker and quote freshness checks follow
elif valid_state["generation"] and dashboard_projection_from_state(valid_state) is None:
    reason = "dashboard_projection_missing"
```

An entirely empty state continues to report `portfolio_missing`; a generated state with no complete projection reports `dashboard_projection_missing`.

- [ ] **Step 5: Run state tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_account_sync_state.py -q
```

Expected: all `tests/test_account_sync_state.py` tests pass.

- [ ] **Step 6: Commit the contract**

```bash
git add src/open_trader/account_sync_state.py tests/test_account_sync_state.py
git commit -m "feat: define dashboard projection contract"
```

---

### Task 2: Build complete Dashboard fields from accepted facts and quotes

**Files:**

- Modify: `src/open_trader/account_sync_state.py:313-585`
- Test: `tests/test_account_sync_state.py`

**Interfaces:**

- Consumes: raw accepted v1 state, published quote payloads, `PortfolioBuildError`, and `money()`, `number()`, and `pct()` from `portfolio.py`.
- Produces:
  - `build_dashboard_projection(state: Mapping[str, object], quotes: Mapping[str, object], *, generated_at: str) -> dict[str, object]`
  - `with_dashboard_projection(state: Mapping[str, object], quotes: Mapping[str, object], *, generated_at: str) -> dict[str, object]`

- [ ] **Step 1: Write failing mixed-source calculation tests**

Add fixture helpers that accept four broker candidates: one Futu live source, one Tiger live source with explicit account FX, one Phillips statement source without `fx_rates`, and one Eastmoney statement source without `fx_rates`.

Add:

```python
def test_dashboard_projection_computes_live_and_statement_hkd_and_weights() -> None:
    state = _four_broker_projection_state()
    projection = build_dashboard_projection(
        state,
        {"status": "ok", "last_success_at": "2026-07-31T08:30:05+08:00",
         "stale": False, "quotes": {}},
        generated_at="2026-07-31T08:30:05+08:00",
    )

    tiger = next(row for row in projection["broker_positions"]
                 if row["broker"] == "tiger")
    phillips = next(row for row in projection["broker_positions"]
                    if row["broker"] == "phillips")
    eastmoney = next(row for row in projection["broker_positions"]
                     if row["broker"] == "eastmoney")

    assert tiger["market_value_hkd"]
    assert tiger["account_weight_hkd"].endswith("%")
    assert tiger["portfolio_weight_hkd"].endswith("%")
    assert phillips["market_value_hkd"]
    assert phillips["account_weight_hkd"].endswith("%")
    assert phillips["portfolio_weight_hkd"].endswith("%")
    assert eastmoney["market_value_hkd"]
    assert sum(Decimal(row["portfolio_value_hkd"])
               for row in projection["broker_summaries"]) == Decimal(
                   projection["summary"]["portfolio_value_hkd"]
               )
```

Use exact fixture values to assert statement conversion, including HKD `1` and CNY `1.08`, and to prove that a statement row cannot make Tiger's portfolio weight blank.

- [ ] **Step 2: Write failing quote ownership and option tests**

Add:

```python
def test_dashboard_projection_reprices_live_but_not_statement_positions() -> None:
    state = _four_broker_projection_state(
        tiger_symbol="ADP",
        tiger_quantity="11",
        tiger_cost_value="3067.9",
        tiger_market_value="2902.57",
        phillips_symbol="00200",
        phillips_quantity="522",
        phillips_market_value="1973.16",
    )
    quotes = _quotes(
        ("US", "ADP", "300", "after_hours"),
        ("HK", "00200", "99", ""),
    )

    projection = build_dashboard_projection(
        state, quotes, generated_at="2026-07-31T08:31:00+08:00"
    )
    rows = {(row["broker"], row["symbol"]): row
            for row in projection["broker_positions"]}

    assert rows[("tiger", "ADP")]["last_price"] == "300"
    assert rows[("tiger", "ADP")]["price_kind"] == "after_hours"
    assert rows[("tiger", "ADP")]["market_value"] == "3300.00"
    assert rows[("phillips", "00200")]["last_price"] != "99"
    assert rows[("phillips", "00200")]["price_kind"] == "statement"
```

Parametrize standard US option long and short cases and assert multiplier `100`, signed market value, P&L, and P&L percentage. Add an overnight quote and assert `price_kind == "overnight"`.

- [ ] **Step 3: Write failing fail-closed tests**

Add tests for:

```python
with pytest.raises(PortfolioBuildError, match="live FX missing"):
    build_dashboard_projection(
        _four_broker_projection_state(tiger_fx_rates=()),
        _quotes(),
        generated_at="2026-07-31T08:31:00+08:00",
    )

with pytest.raises(PortfolioBuildError, match="portfolio HKD total"):
    build_dashboard_projection(
        _four_broker_projection_state(all_values_zero=True),
        _quotes(),
        generated_at="2026-07-31T08:31:00+08:00",
    )
```

Also assert that an unknown source blocks a projection, while a source marked `failed` with retained accepted rows remains usable so another successful broker can produce a complete new projection.

- [ ] **Step 4: Run the builder tests and verify red**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_account_sync_state.py -k 'dashboard_projection' -q
```

Expected: tests fail because `build_dashboard_projection` and `with_dashboard_projection` do not exist.

- [ ] **Step 5: Implement deterministic row projection**

Implement the public functions with this shape:

```python
def build_dashboard_projection(
    state: Mapping[str, object],
    quotes: Mapping[str, object],
    *,
    generated_at: str,
) -> dict[str, object]:
    accepted = state if _is_valid_state(state) else empty_account_sync_state()
    position_rows, cash_rows = _project_dashboard_rows(accepted, quotes)
    broker_summaries = [
        _project_dashboard_broker_summary(broker, position_rows, cash_rows)
        for broker in REQUIRED_BROKERS
    ]
    summary = _project_dashboard_summary(position_rows, cash_rows)
    _apply_dashboard_weights(position_rows, broker_summaries, summary)
    projection = {
        "generated_at": generated_at,
        "quote_as_of": str(quotes.get("last_success_at") or ""),
        "summary": summary,
        "broker_summaries": broker_summaries,
        "broker_positions": position_rows,
        "cash_details": cash_rows,
    }
    if not _is_valid_dashboard_projection(projection):
        raise PortfolioBuildError("invalid dashboard projection")
    return projection


def with_dashboard_projection(
    state: Mapping[str, object],
    quotes: Mapping[str, object],
    *,
    generated_at: str,
) -> dict[str, object]:
    projected = deepcopy(state)
    projected["dashboard_projection"] = build_dashboard_projection(
        projected, quotes, generated_at=generated_at
    )
    return projected
```

The private row builder must:

1. Require all four sources to have accepted facts (`status != "unknown"`).
2. Build a quote lookup from quote-row `market` and `symbol`, not from dictionary keys.
3. Use quotes only for live sources and only when quote-row `status == "ok"` with a finite positive `last_price`.
4. Use `price_session` values `overnight`, `pre_market`, and `after_hours` directly; map regular or empty accepted quote sessions to `live`.
5. Use `price_time` when present, otherwise quote-row `fetched_at`; use source `data_as_of` for statement or account-snapshot prices.
6. Compute quote-owned market value as `price * quantity * multiplier`; retain source market value when no valid live quote is available.
7. Use explicit source FX for live rows. Use explicit source FX first and the three approved static rates second for statement rows.
8. Compute `cost_value_hkd` only when cost is known. Recompute P&L from the selected final market value and known cost; use `abs(cost_value)` as the P&L percentage denominator so short standard options retain the current sign semantics.
9. Set `market_value_usd` only when `currency == "USD"`.
10. Project cash and available cash into HKD with the same source-specific FX rule.

- [ ] **Step 6: Implement summaries and weights from the final rows**

For each broker:

```python
portfolio_value = holding_value + cash_like_value
account_weight = market_value_hkd / portfolio_value
```

For the global projection:

```python
portfolio_value = sum(
    Decimal(summary["portfolio_value_hkd"])
    for summary in broker_summaries
)
portfolio_weight = market_value_hkd / portfolio_value
```

Treat `cash`, `money_market_fund`, and `market == "CASH"` as cash-like. Publish all four broker summaries in canonical order. Require positive finite broker denominators for brokers with holdings and a positive finite global denominator. Use `money()`, `number()`, and `pct()` for the exact two-decimal output contract.

- [ ] **Step 7: Run state and portfolio regression tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_account_sync_state.py tests/test_portfolio.py -q
```

Expected: all tests pass, including mixed live/statement, quote update, statement isolation, option multiplier, missing live FX, and incomplete denominator cases.

- [ ] **Step 8: Commit the projection builder**

```bash
git add src/open_trader/account_sync_state.py tests/test_account_sync_state.py
git commit -m "feat: build controller dashboard projection"
```

---

### Task 3: Publish and retain the projection in both controller loops

**Files:**

- Modify: `src/open_trader/account_sync_controller.py:15-28`
- Modify: `src/open_trader/account_sync_controller.py:87-183`
- Test: `tests/test_account_sync_controller.py:1-290`

**Interfaces:**

- Consumes: `with_dashboard_projection()`, `load_published_quotes()`, existing atomic writers, accepted-source retention, and controller heartbeat loop results.
- Produces: account-cycle projection publication, quote-cycle projection publication, and explicit `dashboard_projection_failed`/`dashboard_projection_publish_failed` loop failures.

- [ ] **Step 1: Write failing account-loop publication tests**

Extend the full-generation test to seed quotes and assert that the final state write has:

```python
projection = published["dashboard_projection"]
assert projection["generated_at"] == "2026-07-31T08:30:00+08:00"
assert projection["broker_positions"]
assert all(row["portfolio_weight_hkd"] for row in projection["broker_positions"])
```

Add a test where one source refresh fails but has prior accepted data. Assert:

- the failed source's accepted facts remain unchanged;
- another broker's new accepted facts enter the new projection;
- the last complete four-broker denominator is retained;
- the source failure remains visible in health.

- [ ] **Step 2: Write failing quote-loop and retention tests**

Add:

```python
def test_quote_success_rebuilds_projection_without_mutating_accepted_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed four accepted sources and an older complete projection.
    # Refresh ADP from 263.87 to 300.
    before_sources = load_account_sync_state(state_path)["brokers"]

    result = controller.sync_quotes_once()
    published = load_account_sync_state(state_path)
    adp = next(row for row in published["dashboard_projection"]["broker_positions"]
               if row["broker"] == "tiger" and row["symbol"] == "ADP")

    assert result["status"] == "ok"
    assert adp["last_price"] == "300"
    assert published["brokers"] == before_sources
```

Add a projection-builder failure after a successful quote refresh and assert:

```python
assert result["status"] == "failed"
assert result["blocker"] == "dashboard_projection_failed"
assert load_account_sync_state(state_path)["dashboard_projection"] == old_projection
```

Keep the existing quote-refresh-failure assertion that the account-state bytes are identical.

- [ ] **Step 3: Run controller tests and verify red**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_account_sync_controller.py -q
```

Expected: new assertions fail because neither loop updates `dashboard_projection`.

- [ ] **Step 4: Integrate projection publication after the full account cycle**

Keep each broker candidate's current validation and `portfolio.csv`/state publication order. Do not mark an accepted broker failed merely because another source is still unknown during startup. After all broker attempts, build the projection once from the final retained state and currently published quotes:

```python
quotes = load_published_quotes(
    self._quotes_path(), now=datetime.now(SHANGHAI_TZ)
)
try:
    projected_state = with_dashboard_projection(
        state, quotes, generated_at=attempted_at
    )
    write_json_atomic(state_path, projected_state)
    state = projected_state
except OSError as exc:
    return {
        "status": "publication_failed",
        "blocker": "dashboard_projection_publish_failed",
        "message": sanitize_sync_error(
            str(exc), sensitive_roots=(self.config.data_dir,)
        ),
        "brokers": results,
    }
except Exception as exc:
    return {
        "status": "failed",
        "blocker": "dashboard_projection_failed",
        "message": sanitize_sync_error(
            str(exc),
            sensitive_roots=(
                self.config.data_dir,
                self.config.reports_dir,
                self.config.tiger_config_dir,
            ),
        ),
        "brokers": results,
    }
```

This final write either replaces the old projection with a complete one or leaves the last complete projection intact. Source failures continue to use `record_source_failure()` and retain accepted facts.

- [ ] **Step 5: Integrate projection publication after successful quote refresh**

Preserve the existing quote payload publication. If quote refresh returns `failed`, return immediately and leave account-state bytes unchanged. For `ok` or `partial` quote payloads:

```python
write_json_atomic(self._quotes_path(), payload)
state_path = self.config.data_dir / "latest" / "account_sync_state.json"
state = load_account_sync_state(state_path)
try:
    projected_state = with_dashboard_projection(
        state, payload, generated_at=self.now_text()
    )
    write_json_atomic(state_path, projected_state)
except OSError as exc:
    return {
        **payload,
        "status": "publication_failed",
        "blocker": "dashboard_projection_publish_failed",
        "message": sanitize_sync_error(
            str(exc), sensitive_roots=(self.config.data_dir,)
        ),
    }
except Exception as exc:
    return {
        **payload,
        "status": "failed",
        "blocker": "dashboard_projection_failed",
        "message": sanitize_sync_error(str(exc), sensitive_roots=(self.config.data_dir,)),
    }
return payload
```

Return `publication_failed` when the state atomic write itself fails. That returned loop state is persisted by the existing heartbeat and makes Dashboard health abnormal without corrupting accepted facts or the last complete projection.

- [ ] **Step 6: Run controller and health tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_account_sync_controller.py tests/test_account_sync_state.py -q
```

Expected: all tests pass; quote failure still leaves account state byte-identical, quote success changes only the projection, and account partial failure retains a complete denominator.

- [ ] **Step 7: Commit controller publication**

```bash
git add src/open_trader/account_sync_controller.py tests/test_account_sync_controller.py
git commit -m "feat: publish dashboard projection from controller"
```

---

### Task 4: Make the Dashboard backend map final controller fields

**Files:**

- Modify: `src/open_trader/dashboard.py:249-385`
- Modify: `src/open_trader/dashboard.py:406-466`
- Preserve for trend overlays: `src/open_trader/dashboard.py:2380-2565`
- Preserve for trend overlays: `src/open_trader/dashboard.py:4329-4380`
- Test: `tests/test_dashboard.py:140-210`
- Test: `tests/test_dashboard.py:4360-4470`
- Test: `tests/test_dashboard.py:6140-6205`

**Interfaces:**

- Consumes: `dashboard_projection_from_state()` and raw `_accepted_broker_details()`.
- Produces: API `summary`, `broker_summaries`, `broker_positions`, and `cash_details` copied from the valid controller projection, with raw accepted rows reserved for trend-report and holding-detail joins.

- [ ] **Step 1: Write a failing pass-through test with deliberately inconsistent raw values**

Create an accepted state whose raw Tiger row says `market_value="100"` but whose valid projection says:

```python
projected_position = {
    "broker": "tiger",
    "account_alias": "tiger_main",
    "market": "US",
    "asset_class": "stock",
    "symbol": "ADP",
    "name": "Automatic Data Processing",
    "currency": "USD",
    "quantity": "11",
    "cost_price": "278.9",
    "cost_value": "3067.9",
    "last_price": "263.87",
    "price_kind": "after_hours",
    "price_as_of": "2026-07-31T19:34:00-04:00",
    "market_value": "2902.57",
    "market_value_usd": "2902.57",
    "market_value_hkd": "22764.93",
    "cost_value_hkd": "24052.34",
    "unrealized_pnl": "-165.33",
    "unrealized_pnl_pct": "-5.39%",
    "account_weight_hkd": "3.22%",
    "portfolio_weight_hkd": "0.79%",
    "statement_id": "2026-07-31-tiger-live",
    "confidence": "high",
    "notes": "",
}
```

After `load_dashboard_state(config).to_dict()`, assert the four projection-owned API sections equal the state-file projection exactly. This proves the backend did not recompute from the raw `100`.

- [ ] **Step 2: Write missing-projection and trend-boundary tests**

Add a state with valid accepted rows but no projection and assert:

```python
assert state["broker_positions"] == []
assert state["cash_details"] == []
assert state["account_sync"]["status"] == "abnormal"
assert state["account_sync"]["reason"] == "dashboard_projection_missing"
```

Add a trend-overlay regression where raw accepted quantity differs from the projected display quantity. Assert `_project_trend_actual_overlay()` and frozen report facts continue to use the raw accepted quantity. This guards against routing quote-adjusted Dashboard display values into strategy or report decisions.

- [ ] **Step 3: Run Dashboard backend tests and verify red**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard.py -k 'projection or broker_summary or account_sync' -q
```

Expected: the pass-through test fails because `load_dashboard_state()` still calls `_build_summary()`, `_build_broker_summaries()`, `_accepted_broker_details()`, and `_cash_detail_row()` for the account API.

- [ ] **Step 4: Split display projection from raw accepted facts**

At the start of `load_dashboard_state()`:

```python
projection = dashboard_projection_from_state(account_state)
raw_broker_positions, raw_cash_details = _accepted_broker_details(account_state)
display_summary = dict(projection["summary"]) if projection else {
    "holding_count": 0,
    "portfolio_value_hkd": "",
    "holding_value_hkd": "",
    "cash_like_value_hkd": "",
    "holding_weight_hkd": "",
    "cash_like_weight_hkd": "",
    "broker_count": 0,
}
display_broker_summaries = (
    [dict(row) for row in projection["broker_summaries"]] if projection else []
)
display_broker_positions = (
    [dict(row) for row in projection["broker_positions"]] if projection else []
)
display_cash_details = (
    [dict(row) for row in projection["cash_details"]] if projection else []
)
```

Use:

- `display_*` only for `DashboardState.summary`, `broker_summaries`, `broker_positions`, and `cash_details`;
- `raw_broker_positions` for `_group_by_market_symbol()` and `_merge_holding()` metadata;
- raw positions and cash for `_load_trend_reports()` and trend actual overlays.

Do not fall back to `portfolio.csv`, runtime-directory scanning, `_build_broker_summaries()`, or `_cash_detail_row()` for the account view when the projection is missing.

- [ ] **Step 5: Retain only non-account calculation callers**

Do not delete `_build_broker_summary()`, `_detail_fx_to_hkd()`, or related helpers while they are still used by frozen trend actual overlays. Remove their account-page call sites and update names/comments only where needed to make the retained trend-only ownership explicit.

- [ ] **Step 6: Run backend and trend-report regression tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard.py \
  tests/test_trend_report.py \
  tests/test_trend_report_history.py -q
```

Expected: all selected tests pass, and the pass-through test proves projection/API equality.

- [ ] **Step 7: Commit backend mapping**

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "refactor: map controller fields into dashboard api"
```

---

### Task 5: Remove financial calculations from the browser account view

**Files:**

- Modify: `src/open_trader/dashboard_static/dashboard.js:4946-4980`
- Modify: `src/open_trader/dashboard_static/dashboard.js:8302-8489`
- Modify: `src/open_trader/dashboard_static/dashboard.js:8705-8795`
- Test: `tests/test_dashboard_web.py:3852-4259`
- Test: `tests/test_dashboard_web.py:4730-5050`

**Interfaces:**

- Consumes: final `broker_positions` fields and non-financial holding metadata used by detail panels.
- Produces: render-only `accountHoldingGroups()`, final-field price rendering, unchanged financial values from `getHoldings()`, and stable `data-broker`/`data-symbol` row selectors for acceptance.

- [ ] **Step 1: Replace calculation tests with final-field ownership tests**

Replace the tests that call `acceptedPositionForDisplay()`, `quoteAdjustedTotal()`, `accountDisplayRow()`, `percentValue()`, and `quoteAdjustedHolding()` with:

```python
def test_account_groups_use_projected_fields_without_fx_or_quote_inputs() -> None:
    output = run_dashboard_js(r'''
state.dashboard = {
  summary: {portfolio_value_hkd: "999999"},
  broker_summaries: [{broker: "tiger", portfolio_value_hkd: "888888"}],
  broker_positions: [{
    broker: "tiger", account_alias: "tiger_main", market: "US",
    asset_class: "stock", symbol: "ADP", name: "Automatic Data Processing",
    currency: "USD", quantity: "11", cost_price: "278.9",
    cost_value: "3067.9", last_price: "263.87",
    price_kind: "after_hours", price_as_of: "2026-07-31 19:34:00",
    market_value: "2902.57", market_value_usd: "2902.57",
    market_value_hkd: "22764.93", cost_value_hkd: "24052.34",
    unrealized_pnl: "-165.33", unrealized_pnl_pct: "-5.39%",
    account_weight_hkd: "3.22%", portfolio_weight_hkd: "0.79%",
    statement_id: "2026-07-31-tiger-live", confidence: "high", notes: "",
  }],
  holdings: [{market: "US", symbol: "ADP", strategy: {status: "ready"}}],
};
state.quotes = {conflict: {market: "US", symbol: "ADP", last_price: "999"}};
const row = accountHoldingGroups().find(group => group.broker === "tiger").rows[0];
console.log(JSON.stringify(row));
''')

    row = json.loads(output)
    assert row["display"]["market_value_hkd"] == "22764.93"
    assert row["display"]["account_weight_hkd"] == "3.22%"
    assert row["display"]["portfolio_weight_hkd"] == "0.79%"
    assert row["display"]["unrealized_pnl_pct"] == "-5.39%"
    assert row["holding"]["strategy"] == {"status": "ready"}
```

Add a `getHoldings()` test with a conflicting quote and assert every financial field is returned unchanged.

- [ ] **Step 2: Add price-kind and source-scan tests**

Test `renderAccountHoldingPrice()` for `overnight`, `pre_market`, `live`, `after_hours`, `statement`, and `account_snapshot`, including timestamp formatting where present.

Add a static source test:

```python
script = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
for retired in (
    "function acceptedPositionForDisplay(",
    "function quoteAdjustedTotal(",
    "function accountDisplayRow(",
    "function quoteAdjustedHolding(",
    "function percentValue(",
):
    assert retired not in script
```

The existing `numericValue()` remains because charts, indicator ranges, sorting, and formatting still use it.

- [ ] **Step 3: Run web tests and verify red**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -k 'account_groups or projected_fields or price_kind or get_holdings' -q
```

Expected: tests fail because the account view still derives HKD values, reprices from `state.quotes`, and recomputes both weight denominators.

- [ ] **Step 4: Make account grouping a pure projection**

Reduce `getHoldings()` to:

```javascript
function getHoldings() {
  return (state.dashboard && Array.isArray(state.dashboard.holdings))
    ? state.dashboard.holdings
    : [];
}
```

In `accountHoldingGroups()`, find matching aggregate holdings only to carry non-financial detail metadata. Let the controller row win all overlapping fields:

```javascript
const holding = {...matching, ...position, brokers: broker};
return {
  key: accountHoldingKey(broker, holding, index),
  broker,
  holding,
  display: position,
  index,
};
```

Remove all account and global denominator loops. Delete `acceptedPositionForDisplay()`, `quoteAdjustedTotal()`, `accountDisplayRow()`, `percentValue()`, and `quoteAdjustedHolding()` after `rg` confirms no remaining callers.

- [ ] **Step 5: Render canonical controller field names directly**

Update `renderAccountHoldingRow()`:

```javascript
const cells = `<tr class="account-holding-row ${isSelected ? "active-row" : ""}"
  data-broker="${escapeHtml(row.broker)}"
  data-symbol="${escapeHtml(String(display.symbol || "").toUpperCase())}">
  ...
  ${escapeHtml(formatDisplayNumber(display.quantity))}
  ${escapeHtml(formatDisplayNumber(display.cost_price))}
  ${renderAccountHoldingPrice(display)}
  ${escapeHtml(hasValue(display.market_value_usd)
    ? formatMoney(display.market_value_usd, "USD") : "-")}
  ${escapeHtml(formatMoney(display.market_value_hkd, "HKD"))}
  ${escapeHtml(formatPlain(display.account_weight_hkd))}
  ${escapeHtml(formatPlain(display.portfolio_weight_hkd))}
  ${escapeHtml(formatSignedPnl(display.unrealized_pnl_pct))}
  ...
</tr>`;
```

`renderAccountHoldingPrice(display)` maps `price_kind` to the existing Chinese session labels and formats `price_as_of`; it must not read `state.quotes`. `quoteForHolding()` can be deleted after confirming it has no remaining callers.

- [ ] **Step 6: Run focused and complete Dashboard web tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_web.py -q
```

Expected: all web tests pass. No account-view test supplies `fx_to_hkd`, a portfolio denominator, or a quote to obtain final monetary fields.

- [ ] **Step 7: Commit browser simplification**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "refactor: render controller-owned account fields"
```

---

### Task 6: Make acceptance prove state-to-API-to-DOM equality

**Files:**

- Modify: `src/open_trader/dashboard_acceptance.py:230-365`
- Modify: `src/open_trader/dashboard_acceptance.py:2900-2980`
- Modify: `src/open_trader/dashboard_acceptance.py:4420-4535`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**

- Consumes: published `account_sync_state.json`, `/api/dashboard`, stable account-row data selectors, and Playwright.
- Produces: exact projection/API equality checks and API/DOM weight equality for Tiger ADP and one accepted statement position.

- [ ] **Step 1: Write failing projection equality tests**

Add a valid state projection and payload with one altered `portfolio_weight_hkd`. Call the acceptance validator and assert:

```python
assert errors == [
    "Dashboard broker_positions 与控制器 dashboard_projection 不一致"
]
```

Cover all four projected sections independently:

- `summary`
- `broker_summaries`
- `broker_positions`
- `cash_details`

Also add a matching case with no errors.

- [ ] **Step 2: Run acceptance tests and verify red**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py -k 'dashboard_projection' -q
```

Expected: tests fail because the validator does not read or compare controller projection fields.

- [ ] **Step 3: Add a race-safe published-projection comparison**

Add:

```python
def _dashboard_projection_errors(
    payload: Mapping[str, Any],
    account_state: Mapping[str, Any],
) -> list[str]:
    projection = account_state.get("dashboard_projection")
    if not isinstance(projection, Mapping):
        return ["控制器 dashboard_projection 缺失"]
    labels = {
        "summary": "summary",
        "broker_summaries": "broker_summaries",
        "broker_positions": "broker_positions",
        "cash_details": "cash_details",
    }
    return [
        f"Dashboard {label} 与控制器 dashboard_projection 不一致"
        for field, label in labels.items()
        if payload.get(field) != projection.get(field)
    ]
```

Around each live API fetch, read `account_sync_state.json` before and after the fetch. Compare only when `dashboard_projection.generated_at` is unchanged; otherwise retry the sample up to three times. If it never stabilizes, report `控制器投影在验收采样期间持续变化`.

- [ ] **Step 4: Assert projected weights in the live DOM**

In `_check_account_holdings()`:

1. Select Tiger.
2. Find the projected ADP row by `data-broker="tiger"` and `data-symbol="ADP"`.
3. Compare the account-weight and portfolio-weight cells with the exact API row strings.
4. Select Phillips; if it has no positions, select Eastmoney.
5. Use the first accepted statement API position's symbol and compare both weight cells.
6. Require non-empty, non-`-` values for HKD market value and both weights.

These assertions prove that no browser denominator or FX input is needed to display controller values.

- [ ] **Step 5: Run acceptance-focused tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_dashboard_acceptance.py \
  tests/test_dashboard_web.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit acceptance ownership checks**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: verify controller dashboard field ownership"
```

---

### Task 7: Record, verify, deploy, and capture the exact accepted SHA

**Files:**

- Modify: `CHANGELOG.md`
- Runtime evidence: `data/latest/account_sync_state.json`
- Runtime evidence: `data/account_sync/controller_status.json`
- Runtime evidence: `logs/account_sync/launchd.out.log`
- Runtime evidence: `logs/account_sync/launchd.err.log`
- Runtime evidence: `logs/dashboard/launchd.out.log`
- Runtime evidence: `logs/dashboard/launchd.err.log`
- Screenshot output: `output/controller-owned-dashboard-fields-tiger.png`
- Screenshot output: `output/controller-owned-dashboard-fields-statement.png`

**Interfaces:**

- Consumes: all committed implementation tasks, launchd installers, real shared runtime data, and `make acceptance`.
- Produces: final candidate commit, one final `PASS`, exact-SHA redeployment proof, HTTP 200 review URL, and two live screenshots.

- [ ] **Step 1: Add the dated operator-facing changelog entry**

Under `## 2026-07-31`, add:

```markdown
- Moved Dashboard account monetary fields, live-price selection, P&L, and both
  account and portfolio weights into the sole account-sync controller. The
  Dashboard now maps and renders the controller's complete projection, while
  accepted broker/statement facts and trend-report decisions remain unchanged.
  Acceptance verifies controller-state, API, and live DOM field equality for
  Tiger and statement accounts.
```

- [ ] **Step 2: Run focused suites and commit the final candidate**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_account_sync_state.py \
  tests/test_account_sync_controller.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: all focused tests pass.

Then:

```bash
git add CHANGELOG.md
git commit -m "docs: record controller-owned dashboard fields"
git status --short
git rev-parse HEAD
```

Expected: clean tracked worktree and one final candidate SHA. Record it as `ACCEPTED_SHA_CANDIDATE`.

- [ ] **Step 3: Deploy the candidate controller and Dashboard from the worktree**

Prepare the ignored virtual-environment link if absent:

```bash
test -e .venv || ln -s /Users/ray/projects/open_trader/.venv .venv
```

Install both launchd services with code from the worktree and shared runtime data from the main workspace:

```bash
./scripts/install_account_sync_launchd.sh \
  --repo-root /Users/ray/projects/open_trader/.worktrees/controller-owned-dashboard-fields \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python

./scripts/install_dashboard_launchd.sh \
  --repo-root /Users/ray/projects/open_trader/.worktrees/controller-owned-dashboard-fields \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

Expected: both installers report ready; the controller writes a fresh heartbeat from the worktree SHA and the Dashboard returns `http://127.0.0.1:8766/`.

- [ ] **Step 4: Verify the real projection before the final gate**

Inspect:

```bash
/Users/ray/projects/open_trader/.venv/bin/python - <<'PY'
import json
from pathlib import Path

state = json.loads(Path(
    "/Users/ray/projects/open_trader/data/latest/account_sync_state.json"
).read_text(encoding="utf-8"))
projection = state["dashboard_projection"]
positions = projection["broker_positions"]
for broker in ("futu", "tiger", "phillips", "eastmoney"):
    rows = [row for row in positions if row["broker"] == broker]
    assert rows, broker
    for row in rows:
        assert row["market_value_hkd"], (broker, row["symbol"])
        assert row["account_weight_hkd"], (broker, row["symbol"])
        assert row["portfolio_weight_hkd"], (broker, row["symbol"])
print({
    broker: len([row for row in positions if row["broker"] == broker])
    for broker in ("futu", "tiger", "phillips", "eastmoney")
})
PY
```

Also compare one Tiger row and one statement row between state and `/api/dashboard`. Expected: exact equality and no `-`/blank HKD or weight fields.

- [ ] **Step 5: Run the one final acceptance gate**

Run only now:

```bash
DASHBOARD_URL=http://127.0.0.1:8766 \
DASHBOARD_LOG=/Users/ray/projects/open_trader/.worktrees/controller-owned-dashboard-fields/logs/dashboard/launchd.out.log \
make acceptance
```

Expected terminal line: `PASS`. `FAIL` must be diagnosed and fixed, followed by a new committed candidate SHA and a rerun. `BLOCKED` must be reported as blocked and cannot be replaced with fixtures, curl, or screenshots.

After `PASS`, record:

```bash
git rev-parse HEAD
```

This exact value is `ACCEPTED_SHA`.

- [ ] **Step 6: Redeploy the exact accepted SHA**

Without source or data edits, run both installers again with the same arguments from Step 3. Verify:

```bash
launchctl print "gui/$UID/com.open-trader.account-sync-controller"
launchctl print "gui/$UID/com.open-trader.dashboard"
lsof -nP -iTCP:8766 -sTCP:LISTEN
curl --fail --silent --show-error -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8766/
```

Read `data/account_sync/controller_status.json` and prove:

- new controller PID;
- cwd is `/Users/ray/projects/open_trader/.worktrees/controller-owned-dashboard-fields`;
- `git_sha == ACCEPTED_SHA`;
- heartbeat is newer than the redeploy start.

Use `lsof -a -p <dashboard-pid> -d cwd -Fn`, `git -C <cwd> rev-parse HEAD`, and fresh log tails to prove the Dashboard has a new PID, the same cwd/SHA, no traceback, and HTTP `200`.

- [ ] **Step 7: Capture the two required live screenshots**

Open `http://127.0.0.1:8766/` with Playwright after redeployment. Capture:

1. Tiger account tab showing ADP with HKD market value, account weight, portfolio weight, and P&L.
2. Phillips account tab; if Phillips has no accepted position, use Eastmoney. The screenshot must show at least one statement row with HKD market value and both weights.

Save:

```text
output/controller-owned-dashboard-fields-tiger.png
output/controller-owned-dashboard-fields-statement.png
```

Inspect both images for readability and confirm their values match the exact accepted projection and API.

- [ ] **Step 8: Hand off for review**

Only after every preceding check succeeds, report:

- focused and full test counts;
- `make acceptance` `PASS`;
- exact accepted SHA;
- new controller and Dashboard PIDs, cwd, SHA, heartbeat/log timestamps;
- HTTP `200`;
- review URL `http://127.0.0.1:8766/`;
- both live screenshots inline.

Do not merge or push until the user reviews the exact accepted deployment or explicitly asks to integrate it.
