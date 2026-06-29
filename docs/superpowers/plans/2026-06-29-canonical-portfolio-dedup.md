# Canonical Portfolio Deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `portfolio.csv` a canonical current-holdings artifact with no duplicate positions caused by broker asset-class differences.

**Architecture:** Move canonical position identity into `src/open_trader/portfolio.py` so every caller that uses `build_portfolio_rows()` gets the same deduplication behavior. Keep broker detail artifacts unchanged, keep trade-action duplicate rejection as a defensive guard, and add broker-sync tests proving dated/latest portfolio outputs are deduplicated.

**Tech Stack:** Python 3.11+, pytest, existing `open_trader` CSV model helpers, existing Futu/Tiger sync test fixtures.

---

## File Structure

- Modify `src/open_trader/portfolio.py`
  - Own canonical portfolio grouping by `market + symbol + currency`.
  - Normalize asset class within a canonical group.
  - Raise a clear `PortfolioBuildError` for unsafe grouping conflicts.
- Modify `tests/test_portfolio.py`
  - Add direct unit tests for canonical grouping and conflict handling.
- Modify `tests/test_futu_account.py`
  - Add a Futu sync regression test for Tiger `stock` plus Futu `unknown` zero-position deduplication.
- Modify `tests/test_tiger_account.py`
  - Add a Tiger sync regression test for preserved Futu `unknown` plus Tiger `stock` deduplication.
- No planned implementation changes in `src/open_trader/trade_actions.py`
  - Existing duplicate-position rejection stays as a final safety guard.

---

### Task 1: Add Portfolio-Level Canonical Merge Tests

**Files:**
- Modify: `tests/test_portfolio.py`
- Test: `tests/test_portfolio.py`

- [ ] **Step 1: Add failing canonical merge and conflict tests**

Append these tests after `test_build_portfolio_rows_merges_same_us_symbol_across_brokers` in `tests/test_portfolio.py`:

```python
def test_build_portfolio_rows_merges_same_symbol_when_one_asset_class_is_unknown():
    fx = StaticMonthEndFxProvider("2026-06", {"HKD": Decimal("1")})
    positions = [
        position(
            "tiger",
            "01688",
            "2640",
            "26875.2",
            "25634.4",
            market=Market.HK,
            asset_class=AssetClass.STOCK,
            currency="HKD",
            unrealized_pnl="-1240.8",
        ),
        position(
            "futu",
            "01688",
            "0",
            "0",
            "0",
            market=Market.HK,
            asset_class=AssetClass.UNKNOWN,
            currency="HKD",
            unrealized_pnl="-277.2",
        ),
    ]

    rows = build_portfolio_rows("2026-06", positions, [], fx)

    assert len([row for row in rows if row["symbol"] == "01688"]) == 1
    row = next(row for row in rows if row["symbol"] == "01688")
    assert row["market"] == "HK"
    assert row["asset_class"] == "stock"
    assert row["total_quantity"] == "2640"
    assert row["market_value"] == "25634.4"
    assert row["cost_value"] == "26875.2"
    assert row["market_value_hkd"] == "25634.40"
    assert row["brokers"] == "futu;tiger"
    assert row["accounts"] == "futu_main;tiger_main"
    assert row["ai_eligible"] == "true"
    assert row["analysis_symbol"] == "01688"


def test_build_portfolio_rows_rejects_conflicting_known_asset_classes_for_same_identity():
    fx = StaticMonthEndFxProvider("2026-06", {"HKD": Decimal("1")})

    with pytest.raises(
        ValueError,
        match=r"conflicting asset classes for HK\.01688: etf, stock",
    ):
        build_portfolio_rows(
            "2026-06",
            [
                position(
                    "tiger",
                    "01688",
                    "10",
                    "100",
                    "120",
                    market=Market.HK,
                    asset_class=AssetClass.STOCK,
                    currency="HKD",
                ),
                position(
                    "futu",
                    "01688",
                    "5",
                    "50",
                    "60",
                    market=Market.HK,
                    asset_class=AssetClass.ETF,
                    currency="HKD",
                ),
            ],
            [],
            fx,
        )


def test_build_portfolio_rows_rejects_same_symbol_with_multiple_currencies():
    fx = StaticMonthEndFxProvider(
        "2026-06",
        {"HKD": Decimal("1"), "USD": Decimal("7.8")},
    )

    with pytest.raises(
        ValueError,
        match=r"conflicting currencies for HK\.01688: HKD, USD",
    ):
        build_portfolio_rows(
            "2026-06",
            [
                position(
                    "tiger",
                    "01688",
                    "10",
                    "100",
                    "120",
                    market=Market.HK,
                    asset_class=AssetClass.STOCK,
                    currency="HKD",
                ),
                position(
                    "futu",
                    "01688",
                    "5",
                    "50",
                    "60",
                    market=Market.HK,
                    asset_class=AssetClass.STOCK,
                    currency="USD",
                ),
            ],
            [],
            fx,
        )
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio.py::test_build_portfolio_rows_merges_same_symbol_when_one_asset_class_is_unknown \
  tests/test_portfolio.py::test_build_portfolio_rows_rejects_conflicting_known_asset_classes_for_same_identity \
  tests/test_portfolio.py::test_build_portfolio_rows_rejects_same_symbol_with_multiple_currencies \
  -v
```

Expected: at least the merge test fails because the current grouping still includes `asset_class`; the conflict tests fail because no conflict is raised.

- [ ] **Step 3: Confirm only the intended test file changed**

```bash
git diff --name-only
```

Expected: output includes `tests/test_portfolio.py` and no implementation files yet.

---

### Task 2: Implement Canonical Grouping in Portfolio Builder

**Files:**
- Modify: `src/open_trader/portfolio.py`
- Test: `tests/test_portfolio.py`

- [ ] **Step 1: Add canonical grouping helpers**

In `src/open_trader/portfolio.py`, add these definitions after `_merged_confidence()`:

```python
class PortfolioBuildError(ValueError):
    pass


_ASSET_CLASS_PRIORITY = {
    AssetClass.STOCK: 50,
    AssetClass.ETF: 40,
    AssetClass.FUND: 30,
    AssetClass.OPTION: 20,
    AssetClass.MONEY_MARKET_FUND: 10,
    AssetClass.UNKNOWN: 0,
}


def _canonical_position_key(position: Position) -> tuple[Market, str, str]:
    return (
        position.market,
        position.symbol.upper(),
        position.currency.upper(),
    )


def _known_asset_classes(group: list[Position]) -> set[AssetClass]:
    return {
        position.asset_class
        for position in group
        if position.asset_class != AssetClass.UNKNOWN
    }


def _canonical_asset_class(
    market: Market,
    symbol: str,
    group: list[Position],
) -> AssetClass:
    known_classes = _known_asset_classes(group)
    if len(known_classes) > 1:
        class_names = ", ".join(sorted(asset_class.value for asset_class in known_classes))
        raise PortfolioBuildError(
            f"conflicting asset classes for {market.value}.{symbol}: {class_names}"
        )
    if known_classes:
        return next(iter(known_classes))
    return max(
        (position.asset_class for position in group),
        key=lambda asset_class: _ASSET_CLASS_PRIORITY[asset_class],
    )


def _raise_for_conflicting_position_currencies(
    grouped: dict[tuple[Market, str, str], list[Position]],
) -> None:
    currencies_by_symbol: dict[tuple[Market, str], set[str]] = defaultdict(set)
    for market, symbol, currency in grouped:
        currencies_by_symbol[(market, symbol)].add(currency)

    for (market, symbol), currencies in sorted(
        currencies_by_symbol.items(),
        key=lambda item: (item[0][0].value, item[0][1]),
    ):
        if len(currencies) <= 1:
            continue
        currency_text = ", ".join(sorted(currencies))
        raise PortfolioBuildError(
            f"conflicting currencies for {market.value}.{symbol}: {currency_text}"
        )
```

- [ ] **Step 2: Replace position grouping in `build_portfolio_rows()`**

In `build_portfolio_rows()`, replace:

```python
    grouped: dict[tuple[Market, AssetClass, str, str], list[Position]] = defaultdict(list)
    for position in positions:
        grouped[position.identity_key()].append(position)

    raw_rows: list[dict[str, object]] = []
    for (market, asset_class, symbol, currency), group in grouped.items():
```

with:

```python
    grouped: dict[tuple[Market, str, str], list[Position]] = defaultdict(list)
    for position in positions:
        grouped[_canonical_position_key(position)].append(position)
    _raise_for_conflicting_position_currencies(grouped)

    raw_rows: list[dict[str, object]] = []
    for (market, symbol, currency), group in grouped.items():
        asset_class = _canonical_asset_class(market, symbol, group)
```

- [ ] **Step 3: Run the focused portfolio tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio.py -v
```

Expected: all `tests/test_portfolio.py` tests pass.

- [ ] **Step 4: Commit the portfolio tests and implementation**

```bash
git add src/open_trader/portfolio.py tests/test_portfolio.py
git commit -m "fix: canonicalize portfolio position grouping"
```

---

### Task 3: Add Futu Sync Regression Coverage

**Files:**
- Modify: `tests/test_futu_account.py`
- Test: `tests/test_futu_account.py`

- [ ] **Step 1: Add a helper for HK Tiger preserved rows**

In `tests/test_futu_account.py`, add this helper after `tiger_row()`:

```python
def hk_tiger_stock_row() -> dict[str, str]:
    return {
        **tiger_row(),
        "sort_group": "1",
        "market": "HK",
        "asset_class": "stock",
        "symbol": "01688",
        "name": "领益智造",
        "currency": "HKD",
        "total_quantity": "2640",
        "avg_cost_price": "10.18",
        "last_price": "9.71",
        "market_value": "25634.4",
        "cost_value": "26875.2",
        "unrealized_pnl": "-1240.80",
        "unrealized_pnl_pct": "-4.62%",
        "fx_to_hkd": "1",
        "market_value_hkd": "25634.40",
        "cost_value_hkd": "26875.20",
        "portfolio_weight_hkd": "100.00%",
        "brokers": "tiger",
        "accounts": "tiger_5683",
        "analysis_symbol": "01688",
        "notes": "Tiger live account position",
    }
```

- [ ] **Step 2: Add the Futu regression test**

Append this test near the other `sync_futu_portfolio` tests:

```python
def test_sync_futu_portfolio_deduplicates_unknown_zero_position_against_tiger_stock(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [hk_tiger_stock_row()])
    snapshot = client_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "_account_alias": "futu_111",
                "code": "HK.01688",
                "stock_name": "领益智造",
                "qty": "0",
                "cost_price": "0",
                "nominal_price": "9.71",
                "market_val": "0",
                "cost_value": "0",
                "pl_val": "-277.2",
                "currency": "HKD",
            }
        ],
    )

    result = sync_futu_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-29",
        update_latest=True,
    )

    rows = read_portfolio(result.portfolio_path)
    matching = [row for row in rows if row["market"] == "HK" and row["symbol"] == "01688"]
    assert len(matching) == 1
    row = matching[0]
    assert row["asset_class"] == "stock"
    assert row["total_quantity"] == "2640"
    assert row["market_value_hkd"] == "25634.40"
    assert row["brokers"] == "futu;tiger"
    assert result.updated_latest is True
    latest_rows = read_portfolio(result.latest_path)
    assert len([row for row in latest_rows if row["market"] == "HK" and row["symbol"] == "01688"]) == 1
```

- [ ] **Step 3: Run the Futu regression test**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_futu_account.py::test_sync_futu_portfolio_deduplicates_unknown_zero_position_against_tiger_stock \
  -v
```

Expected: PASS after Task 2.

- [ ] **Step 4: Commit the Futu test**

```bash
git add tests/test_futu_account.py
git commit -m "test: cover futu portfolio deduplication"
```

---

### Task 4: Add Tiger Sync Regression Coverage

**Files:**
- Modify: `tests/test_tiger_account.py`
- Test: `tests/test_tiger_account.py`

- [ ] **Step 1: Add a helper for preserved Futu HK detail rows**

In `tests/test_tiger_account.py`, add this helper after `base_portfolio_row()`:

```python
def futu_hk_unknown_detail_row() -> dict[str, str]:
    return {
        "statement_id": "2026-06-29-futu-live",
        "broker": "futu",
        "account_alias": "futu_111",
        "market": "HK",
        "asset_class": "unknown",
        "symbol": "01688",
        "name": "领益智造",
        "currency": "HKD",
        "quantity": "0",
        "cost_price": "0",
        "last_price": "9.71",
        "market_value": "0",
        "cost_value": "0",
        "unrealized_pnl": "-277.2",
        "confidence": "high",
        "notes": "Futu live account position",
    }
```

- [ ] **Step 2: Add the Tiger regression test**

Append this test near the existing Tiger sync tests:

```python
def test_sync_tiger_portfolio_deduplicates_stock_against_preserved_futu_unknown(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio_path, [])
    run_dir = tmp_path / "data/runs/2026-06-29"
    write_csv(
        run_dir / "extracted_positions.csv",
        [
            "statement_id",
            "broker",
            "account_alias",
            "market",
            "asset_class",
            "symbol",
            "name",
            "currency",
            "quantity",
            "cost_price",
            "last_price",
            "market_value",
            "cost_value",
            "unrealized_pnl",
            "confidence",
            "notes",
        ],
        [futu_hk_unknown_detail_row()],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        [
            "statement_id",
            "broker",
            "account_alias",
            "currency",
            "cash_balance",
            "available_balance",
            "confidence",
            "notes",
        ],
        [],
    )
    snapshot = tiger_snapshot_from_records(
        cash_records=[],
        position_records=[
            {
                "account_alias": "tiger_5683",
                "symbol": "01688",
                "sec_type": "STK",
                "currency": "HKD",
                "market": "HK",
                "position_qty": "2640",
                "average_cost": "10.18",
                "market_price": "9.71",
                "market_value": "25634.4",
                "unrealized_pnl": "-1240.8",
            }
        ],
    )

    result = sync_tiger_portfolio(
        snapshot=snapshot,
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        run_date="2026-06-29",
        update_latest=True,
    )

    rows = read_portfolio(result.portfolio_path)
    matching = [row for row in rows if row["market"] == "HK" and row["symbol"] == "01688"]
    assert len(matching) == 1
    row = matching[0]
    assert row["asset_class"] == "stock"
    assert row["total_quantity"] == "2640"
    assert row["market_value_hkd"] == "25634.40"
    assert row["brokers"] == "futu;tiger"
    assert result.updated_latest is True
    latest_rows = read_portfolio(result.latest_path)
    assert len([row for row in latest_rows if row["market"] == "HK" and row["symbol"] == "01688"]) == 1
```

- [ ] **Step 3: Run the Tiger regression test**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_tiger_account.py::test_sync_tiger_portfolio_deduplicates_stock_against_preserved_futu_unknown \
  -v
```

Expected: PASS after Task 2.

- [ ] **Step 4: Commit the Tiger test**

```bash
git add tests/test_tiger_account.py
git commit -m "test: cover tiger portfolio deduplication"
```

---

### Task 5: Verify Trade-Action Guard And Account Sync Suites

**Files:**
- Test: `tests/test_trade_actions.py`
- Test: `tests/test_futu_account.py`
- Test: `tests/test_tiger_account.py`
- Test: `tests/test_portfolio.py`

- [ ] **Step 1: Run the duplicate guard test**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trade_actions.py::test_load_portfolio_action_context_rejects_duplicate_positions \
  -v
```

Expected: PASS. This confirms malformed external portfolios are still rejected by the execution-oriented layer.

- [ ] **Step 2: Run focused sync and portfolio suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio.py \
  tests/test_futu_account.py \
  tests/test_tiger_account.py \
  tests/test_trade_actions.py::test_load_portfolio_action_context_rejects_duplicate_positions \
  -v
```

Expected: all selected tests pass.

- [ ] **Step 3: Commit any test-adjustment fixes**

If Step 2 required code or test adjustments, commit only those adjusted files:

```bash
git status --short
git add src/open_trader/portfolio.py tests/test_portfolio.py tests/test_futu_account.py tests/test_tiger_account.py tests/test_trade_actions.py
git commit -m "fix: stabilize portfolio deduplication tests"
```

Expected: skip this commit if `git status --short` shows no relevant unstaged changes.

---

### Task 6: Validate Against Current Local Portfolio Artifact

**Files:**
- Read: `data/latest/portfolio.csv`
- No code changes expected.

- [ ] **Step 1: Check current duplicate state before a new sync**

Run:

```bash
ruby -rcsv -e 'rows=CSV.read("data/latest/portfolio.csv", headers:true); rows.group_by{|r| [r["market"], r["symbol"], r["currency"]]}.select{|_, v| v.size > 1}.each{|k, v| puts "#{k.join(".")} count=#{v.size} brokers=#{v.map{|r| r["brokers"]}.join("|")} classes=#{v.map{|r| r["asset_class"]}.join("|")} qty=#{v.map{|r| r["total_quantity"]}.join("|")}"}'
```

Expected before a new broker sync: this may still print the existing `HK.01688` duplicate because the implementation changes do not rewrite historical latest automatically.

- [ ] **Step 2: Run a safe dated sync command before updating latest**

Run one broker sync without `--update-latest` using the broker that is currently available. For Futu:

```bash
.venv/bin/python -m open_trader sync-futu-portfolio \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --date 2026-06-29
```

For Tiger:

```bash
.venv/bin/python -m open_trader sync-tiger-portfolio \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --date 2026-06-29
```

Expected: the command writes dated artifacts and prints `portfolio:` pointing at `data/runs/2026-06-29/portfolio.csv`. If broker credentials or OpenD are unavailable, record the exact error and proceed with test-suite verification only.

- [ ] **Step 3: Check the dated portfolio for duplicates**

Run:

```bash
ruby -rcsv -e 'path="data/runs/2026-06-29/portfolio.csv"; rows=CSV.read(path, headers:true); dups=rows.group_by{|r| [r["market"], r["symbol"], r["currency"]]}.select{|_, v| v.size > 1}; abort("duplicates: #{dups.keys.inspect}") unless dups.empty?; puts "no duplicate canonical positions in #{path}"'
```

Expected: prints `no duplicate canonical positions in data/runs/2026-06-29/portfolio.csv`.

- [ ] **Step 4: Run focused daily dry-runs if the dated or latest portfolio is clean**

If `data/latest/portfolio.csv` has been safely updated through a successful broker sync, run:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --market HK \
  --date 2026-06-29 \
  --config config/daily_premarket.env \
  --dry-run
```

Then run:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --market US \
  --date 2026-06-29 \
  --config config/daily_premarket.env \
  --dry-run
```

Expected: neither run fails with `duplicate portfolio position(s)`. Other readiness statuses such as `partial` or `review_required` can still be valid if quotes, deadlines, or model limits intervene.

---

### Task 7: Final Verification

**Files:**
- Test: full repository test suite.

- [ ] **Step 1: Run the full test suite**

Run:

```bash
.venv/bin/python -m pytest
```

Expected: all tests pass.

- [ ] **Step 2: Inspect changed files**

Run:

```bash
git status --short
git diff --stat
git diff --check
```

Expected: only intended source/test files are modified, and `git diff --check` prints no whitespace errors.

- [ ] **Step 3: Commit final implementation state**

If there are uncommitted implementation changes after verification:

```bash
git add src/open_trader/portfolio.py tests/test_portfolio.py tests/test_futu_account.py tests/test_tiger_account.py
git commit -m "fix: deduplicate canonical portfolio holdings"
```

Expected: commit succeeds. If all prior task commits already captured the final state, skip this commit.

---

## Self-Review

- Spec coverage: the plan covers canonical identity, asset-class normalization, zero-quantity duplicate behavior through merge tests, conflict handling, Futu/Tiger sync artifacts, daily report input stability, and preservation of the trade-action duplicate guard.
- Red-flag scan: no empty implementation slots or unspecified test work remain.
- Type consistency: new helpers use existing `Market`, `AssetClass`, `Position`, `StaticMonthEndFxProvider`, `build_portfolio_rows`, `sync_futu_portfolio`, and `sync_tiger_portfolio` names.
