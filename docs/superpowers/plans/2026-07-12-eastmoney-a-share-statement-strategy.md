# 东方财富 A 股结单与策略接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从东方财富电子对账单导入当前 A 股持仓，以 AKShare 获取日线，并在现有标准策略与 Dashboard 中按既有布局使用这些数据。

**Architecture:** 新解析器把首页持仓和人民币现金归一化为现有 `Position`、`CashBalance` 与 `portfolio.csv`；AKShare provider 实现现有 `DailyKlineProvider`，不另建行情或策略链路。Dashboard 仅扩展 `CN` 市场、`eastmoney` 券商和现有表格分组，并在展示时用本地 A 股价格缓存刷新派生市值字段。

**Tech Stack:** Python 3.12、pdfplumber、AKShare、现有 CSV/Backtrader/Dashboard、pytest、Playwright

## Global Constraints

- 只解析对账单首页“汇总股票资料”和人民币现金，不读取资金流水。
- PDF 密码只在内存中使用，不写入参数日志、配置或产物。
- A 股市场代码为 `CN`，券商代码为 `eastmoney`，价格路径为 `data/prices/CN/<symbol>.csv`。
- 行情使用 AKShare `stock_zh_a_hist` 的前复权日线；失败或空响应不得覆盖旧缓存。
- 复用趋势回调、突破动量、区间均值回归，不新增 A 股专属策略。
- Dashboard 的列名、列序、详情入口和响应式布局保持现状；不新增专属卡片或页面。
- 不接 EMT、EMQ、实时账户、分钟行情或自动下单。
- 写日期归档产物；只有 `--update-latest` 才更新 `data/latest/portfolio.csv`。
- `portfolio.csv` 是稳定兼容合同：表头与 `PORTFOLIO_FIELDNAMES` 顺序不得变化；东方财富导入只替换纯 `eastmoney` 行，其他券商行必须保留。
- 每次合并后必须从全组合重新计算港元总资产、持仓数、换算市值、持仓权重和盈亏派生值；缺失汇率或金额不得用 0 或旧值代替。

---

### Task 1: 让统一数据契约识别 `CN`

**Files:**
- Modify: `src/open_trader/models.py`
- Modify: `src/open_trader/market_scope.py`
- Modify: `src/open_trader/portfolio.py`
- Test: `tests/test_market_scope.py`
- Test: `tests/test_portfolio.py`

**Interfaces:**
- Produces: `Market.CN`, `MarketScope.CN`, `parse_market_scope("CN") -> MarketScope.CN`
- Produces: CNY positions normalized into existing portfolio rows with `ai_eligible=true`

- [ ] **Step 1: Write failing CN contract tests**

```python
def test_parse_cn_market_scope() -> None:
    assert parse_market_scope("cn") is MarketScope.CN


def test_cn_stock_is_strategy_eligible() -> None:
    cn_position = position(
        "eastmoney", "600025", "6000", "53346", "57720",
        market=Market.CN, asset_class=AssetClass.STOCK, currency="CNY",
    )
    rows = build_portfolio_rows(
        "2026-07", [cn_position], [],
        StaticMonthEndFxProvider("2026-07", {"CNY": Decimal("1.08")}),
    )
    assert rows[0]["market"] == "CN"
    assert rows[0]["ai_eligible"] == "true"
    assert rows[0]["analysis_symbol"] == "600025"
```

- [ ] **Step 2: Run tests and confirm the missing enum failures**

Run: `.venv/bin/pytest tests/test_market_scope.py tests/test_portfolio.py -q`

Expected: FAIL because `Market.CN` and `MarketScope.CN` do not exist.

- [ ] **Step 3: Add the minimum CN branches**

```python
# models.py
class Market(StrEnum):
    US = "US"
    HK = "HK"
    CN = "CN"
    OTHER = "OTHER"
    CASH = "CASH"

# market_scope.py
class MarketScope(StrEnum):
    HK = "HK"
    US = "US"
    CN = "CN"

# portfolio.py
def _ai_eligible(position: Position) -> bool:
    return position.market in {Market.US, Market.HK, Market.CN} and position.asset_class in {
        AssetClass.STOCK, AssetClass.ETF,
    }
```

Add `CN` to `_sort_group` after the four fixed HK/US groups and before other/cash. Change the market-scope validation message to `market must be one of: HK, US, CN`. No default CNY FX rate is added; imports must supply the rate explicitly.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/pytest tests/test_market_scope.py tests/test_portfolio.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/models.py src/open_trader/market_scope.py src/open_trader/portfolio.py tests/test_market_scope.py tests/test_portfolio.py
git commit -m "feat: add CN market contract"
```

### Task 2: 解析东方财富加密 PDF 首页

**Files:**
- Create: `src/open_trader/parsers/eastmoney.py`
- Modify: `src/open_trader/parsers/__init__.py`
- Create: `tests/test_eastmoney_parser.py`

**Interfaces:**
- Produces: `EastmoneyStatementParser(password: str)` implementing `StatementParser.parse(path, month)`
- Produces: `parse_eastmoney_page(first_page_text: str, tables: list[list[list[str | None]]], month: str) -> ParseResult`

- [ ] **Step 1: Write parser tests using sanitized extracted structures**

```python
POSITIONS = [["交易市场", "证券代码", "证券名称", "持仓数量", "市价", "成本价", "证券市值"],
             ["沪市A股", "600025", "华能水电", "6000", "9.620", "8.891", "57720.00"]]


def test_parse_eastmoney_first_page_only() -> None:
    result = parse_eastmoney_page(
        "资金余额(RMB)： 10000.00\n资金可用(RMB)： 405219.55",
        [POSITIONS, [["发生日期", "买卖类别", "证券代码"]]],
        "2026-07",
    )
    assert [(p.market, p.symbol, p.quantity) for p in result.positions] == [
        (Market.CN, "600025", Decimal("6000")),
    ]
    assert result.positions[0].cost_value == Decimal("53346.000")
    assert result.cash_balances[0].cash_balance == Decimal("10000.00")
    assert result.cash_balances[0].available_balance == Decimal("405219.55")


def test_parser_rejects_missing_summary_table() -> None:
    with pytest.raises(ValueError, match="汇总股票资料"):
        parse_eastmoney_page("资金余额", [], "2026-07")
```

Add a fake `pdfplumber.open` test asserting the parser passes the supplied password, reads only `pages[0]`, reports the real page count, and never includes it in exceptions.

- [ ] **Step 2: Run tests and confirm import failure**

Run: `.venv/bin/pytest tests/test_eastmoney_parser.py -q`

Expected: FAIL because `open_trader.parsers.eastmoney` does not exist.

- [ ] **Step 3: Implement the parser**

```python
BROKER = "eastmoney"
ACCOUNT_ALIAS = "eastmoney_main"
POSITION_HEADER = ("交易市场", "证券代码", "证券名称", "持仓数量", "市价", "成本价", "证券市值")


class EastmoneyStatementParser(StatementParser):
    broker = BROKER

    def __init__(self, password: str):
        self.password = password

    def parse(self, path: Path, month: str) -> ParseResult:
        try:
            with pdfplumber.open(path, password=self.password) as pdf:
                if not pdf.pages:
                    raise ValueError("东方财富对账单没有页面")
                page = pdf.pages[0]
                result = parse_eastmoney_page(page.extract_text() or "", page.extract_tables(), month)
                return replace(result, page_count=len(pdf.pages))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("无法打开或解密东方财富对账单") from exc
```

`parse_eastmoney_page` selects only the table whose normalized first row equals `POSITION_HEADER`; every data row must have seven cells, a six-digit symbol, positive quantity, finite price/cost/value, and a supported `沪市A股` or `深市A股` label. Set `cost_value = quantity * cost_price`, `unrealized_pnl = market_value - cost_value`, currency `CNY`, asset class `STOCK`, confidence `high`. Extract `资金余额(RMB)` and `资金可用(RMB)` with anchored regexes and create one `CashBalance`. Never parse the transaction table.

- [ ] **Step 4: Run parser tests**

Run: `.venv/bin/pytest tests/test_eastmoney_parser.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/parsers/eastmoney.py src/open_trader/parsers/__init__.py tests/test_eastmoney_parser.py
git commit -m "feat: parse Eastmoney A-share statements"
```

### Task 3: 接入结单导入命令并保护 `latest`

**Files:**
- Modify: `src/open_trader/pipeline.py`
- Modify: `src/open_trader/cli.py`
- Modify: `src/open_trader/portfolio.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_parsers_text.py`
- Modify: `tests/test_portfolio.py`

**Interfaces:**
- Consumes: `EastmoneyStatementParser(password)` from Task 2
- Produces: `run_import(month, statement_paths, parsers, data_dir, fx_provider, update_latest=True) -> ImportResult`
- Produces CLI: `open-trader import-statements --eastmoney PATH --cny-hkd RATE [--phillips PATH --usd-hkd RATE] [--update-latest]`
- Produces: broker-safe merge that preserves every non-Eastmoney row and recomputes all portfolio weights from the combined HKD total

- [ ] **Step 1: Write failing import and CLI tests**

```python
def test_run_import_can_leave_latest_untouched(tmp_path: Path) -> None:
    class Parser(StatementParser):
        broker = "eastmoney"
        def parse(self, path: Path, month: str) -> ParseResult:
            return ParseResult(statement_id=f"{month}-eastmoney", broker=self.broker)

    latest = tmp_path / "latest" / "portfolio.csv"
    latest.parent.mkdir(parents=True)
    latest.write_text("sentinel\n", encoding="utf-8")
    statement = tmp_path / "statement.pdf"
    statement.write_bytes(b"fixture")
    result = run_import(
        month="2026-07",
        statement_paths={"eastmoney": statement},
        parsers=[Parser()],
        data_dir=tmp_path,
        fx_provider=StaticMonthEndFxProvider("2026-07", {"CNY": Decimal("1.08")}),
        update_latest=False,
    )
    assert result.portfolio_path.exists()
    assert latest.read_text(encoding="utf-8") == "sentinel\n"


def test_cli_imports_only_eastmoney_and_prompts_password(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("open_trader.cli.getpass", lambda _: "secret")
    monkeypatch.setattr("open_trader.cli.run_import", fake_run_import)
    assert main(["import-statements", "--month", "2026-07", "--eastmoney", "statement.pdf",
                 "--cny-hkd", "1.08", "--data-dir", str(tmp_path)]) == 0
```

Also assert: at least one statement is required; `--eastmoney` requires `--cny-hkd`; `--phillips` requires `--usd-hkd`; `--update-latest` is forwarded; console output contains paths/counts but not the password.

Add a regression test with existing Futu, Tiger, Phillips, and cash rows plus one stale Eastmoney row. Assert the output field names equal `PORTFOLIO_FIELDNAMES` in the same order; all non-Eastmoney values are preserved except the globally recalculated `portfolio_weight_hkd`; the stale Eastmoney row is replaced; combined HKD value equals the exact sum of all valid rows; holding count is derived from the combined non-cash rows; and recalculated weights sum to `100.00%` within the existing two-decimal rounding contract. Add failure tests for mixed broker rows containing `eastmoney`, identity collisions with a preserved broker, missing CNY FX, and non-finite or missing `market_value_hkd`.

- [ ] **Step 2: Run focused tests and verify failures**

Run: `.venv/bin/pytest tests/test_pipeline.py tests/test_parsers_text.py -q`

Expected: FAIL because the CLI requires Phillips and `run_import` always promotes latest.

- [ ] **Step 3: Add optional statement selection and explicit promotion**

```python
# cli.py parser arguments
import_parser.add_argument("--phillips", type=Path)
import_parser.add_argument("--eastmoney", type=Path)
import_parser.add_argument("--usd-hkd", type=positive_decimal)
import_parser.add_argument("--cny-hkd", type=positive_decimal)
import_parser.add_argument("--update-latest", action="store_true")
```

Build `statement_paths` and `parsers` only for supplied files. Obtain the Eastmoney password with `getpass("东方财富对账单密码: ")`; do not accept a plaintext password CLI flag. Build the FX map only from supplied rates. Add the smallest shared merge helper beside `build_portfolio_rows`: it removes only rows whose broker set is exactly `{eastmoney}`, rejects mixed Eastmoney rows and preserved/new identity collisions, appends the newly built Eastmoney rows, validates all required HKD values, and recalculates `portfolio_weight_hkd` over the entire combined portfolio. In `run_import`, always write and atomically promote the dated run directory; execute the existing latest temp/backup/replace block only when `update_latest` is true. Keep the function default `True` so existing direct Python callers remain compatible; the CLI passes `args.update_latest` explicitly.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/pytest tests/test_pipeline.py tests/test_parsers_text.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/pipeline.py src/open_trader/cli.py src/open_trader/portfolio.py tests/test_pipeline.py tests/test_parsers_text.py tests/test_portfolio.py
git commit -m "feat: import Eastmoney statements safely"
```

### Task 4: 用 AKShare 实现现有日线 provider

**Files:**
- Modify: `pyproject.toml`
- Create: `src/open_trader/akshare_quote.py`
- Modify: `src/open_trader/backtest_prices.py`
- Modify: `src/open_trader/strategy_backtest.py`
- Modify: `src/open_trader/dashboard_web.py`
- Create: `tests/test_akshare_quote.py`
- Modify: `tests/test_backtest_prices.py`
- Modify: `tests/test_strategy_backtest.py`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Produces: `AkShareDailyKlineProvider.get_daily_kline(symbol: str, *, start: str, end: str) -> list[DailyKlineBar]`
- Consumes existing `DailyKlineProvider` without a second strategy path
- Produces CN benchmark mapping `CN -> 000300`

- [ ] **Step 1: Write failing provider tests**

```python
def test_akshare_provider_maps_a_share_daily_columns() -> None:
    class FakeFrame:
        def __init__(self, rows):
            self.rows = rows
        def to_dict(self, orient):
            assert orient == "records"
            return self.rows

    frame = FakeFrame([
        {"日期": "2026-07-10", "开盘": 9.5, "最高": 9.7, "最低": 9.4,
         "收盘": 9.62, "成交量": 123456},
    ])
    provider = AkShareDailyKlineProvider(stock_history=lambda **kwargs: frame)
    bars = provider.get_daily_kline("CN.600025", start="2026-07-01", end="2026-07-10")
    assert bars[0] == DailyKlineBar(
        date="2026-07-10", close=9.62, volume=123456,
        open=9.5, high=9.7, low=9.4,
    )


def test_failed_fetch_does_not_replace_existing_cache(tmp_path: Path) -> None:
    class FailingProvider:
        def get_daily_kline(self, symbol: str, *, start: str, end: str):
            raise RuntimeError("upstream failed")

    path = tmp_path / "prices" / "CN" / "600025.csv"
    path.parent.mkdir(parents=True)
    path.write_text("date,open,high,low,close,volume\n2026-07-09,1,1,1,1,1\n")
    with pytest.raises(RuntimeError):
        fetch_backtest_prices(
            data_dir=tmp_path, market="CN", symbol="600025",
            start="2026-07-01", end="2026-07-10", provider=FailingProvider(),
        )
    assert "2026-07-09" in path.read_text()
```

Also test six-digit CN symbol validation, `adjust="qfq"`, inclusive date formatting without hyphens, finite OHLCV validation, and the index branch using `stock_zh_index_daily_em(symbol="sh000300")`.

- [ ] **Step 2: Run focused tests and verify failures**

Run: `.venv/bin/pytest tests/test_akshare_quote.py tests/test_backtest_prices.py tests/test_strategy_backtest.py tests/test_dashboard_web.py -q`

Expected: FAIL because the provider and CN market/benchmark routing are absent.

- [ ] **Step 3: Add AKShare dependency and provider**

```python
class AkShareDailyKlineProvider:
    def __init__(self, stock_history=None, index_history=None):
        if stock_history is None or index_history is None:
            import akshare as ak
            stock_history = stock_history or ak.stock_zh_a_hist
            index_history = index_history or ak.stock_zh_index_daily_em
        self.stock_history = stock_history
        self.index_history = index_history

    def get_daily_kline(self, symbol: str, *, start: str, end: str) -> list[DailyKlineBar]:
        market, code = symbol.split(".", 1)
        if market != "CN" or not re.fullmatch(r"\d{6}", code):
            raise ValueError("AKShare 行情仅支持 CN.六位代码")
        frame = (self.index_history(symbol="sh000300") if code == "000300" else
                 self.stock_history(symbol=code, period="daily",
                                    start_date=start.replace("-", ""),
                                    end_date=end.replace("-", ""), adjust="qfq"))
        return validated_bars_between(frame, start, end)
```

Implement `validated_bars_between(frame, start, end)` in the same module. It calls `frame.to_dict("records")`, accepts the Chinese stock columns and lowercase index columns, filters inclusively by ISO date, rejects missing, non-finite, or negative OHLCV data, sorts by date, rejects duplicate dates, and returns `DailyKlineBar` objects. This remains a private helper rather than a second provider layer.

Add `akshare` to runtime dependencies. Extend CN normalization and price paths in `backtest_prices.py`. Set `BENCHMARK_SYMBOLS = {"US": "SPY", "HK": "02800", "CN": "000300"}` and expose `"CN": "000300"` in Dashboard backtest options. In `build_standard_backtest_run_payload`, instantiate `AkShareDailyKlineProvider()` only when the parsed request market is `CN`; keep `FutuQuoteClient` for HK/US. Preserve the existing atomic CSV writer so provider exceptions and empty rows occur before replacement.

- [ ] **Step 4: Install and run focused tests**

Run: `.venv/bin/pip install -e '.[dev]' && .venv/bin/pytest tests/test_akshare_quote.py tests/test_backtest_prices.py tests/test_strategy_backtest.py tests/test_dashboard_web.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/open_trader/akshare_quote.py src/open_trader/backtest_prices.py src/open_trader/strategy_backtest.py src/open_trader/dashboard_web.py tests/test_akshare_quote.py tests/test_backtest_prices.py tests/test_strategy_backtest.py tests/test_dashboard_web.py
git commit -m "feat: fetch CN daily prices with AKShare"
```

### Task 5: 在现有 Dashboard 中加入 A 股行

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: CN portfolio rows and `data/prices/CN/<symbol>.csv`
- Produces: existing dashboard payload with refreshed CN `last_price`, `market_value`, `market_value_hkd`, `unrealized_pnl`, `unrealized_pnl_pct`
- Produces: existing table layout with `A 股` filter and `A 股正股` group

- [ ] **Step 1: Write failing backend and static-contract tests**

```python
def test_dashboard_refreshes_cn_derived_values_from_cached_close(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    row = {field: "" for field in PORTFOLIO_FIELDNAMES}
    row.update({"market": "CN", "asset_class": "stock", "symbol": "600025",
                "name": "华能水电", "currency": "CNY", "total_quantity": "6000",
                "last_price": "9.62", "market_value": "57720", "cost_value": "53346",
                "fx_to_hkd": "1.08", "brokers": "eastmoney"})
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [row])
    write_csv(config.data_dir / "prices/CN/600025.csv",
              ["date", "open", "high", "low", "close", "volume"],
              [{"date": "2026-07-10", "open": "9.8", "high": "10.1", "low": "9.7",
                "close": "10.00", "volume": "123456"}])
    holding = load_dashboard_state(config).holdings[0]
    assert holding["last_price"] == "10"
    assert holding["market_value"] == "60000.00"
    assert holding["unrealized_pnl"] == "6654.00"


def test_dashboard_static_keeps_existing_columns_and_adds_cn() -> None:
    html = STATIC_INDEX.read_text()
    js = STATIC_JS.read_text()
    assert html.index("<th>明细</th>") < html.index("<th>市场</th>") < html.index("<th>盈亏</th>")
    assert 'data-market="CN">A 股</button>' in html
    assert 'label: "A 股正股"' in js
```

Also assert `BROKER_LABELS["eastmoney"] == "东方财富"`, source kind `statement`, CN is included in the backtest universe, and no new A-share-only panel/card IDs are added.

- [ ] **Step 2: Run tests and confirm failures**

Run: `.venv/bin/pytest tests/test_dashboard.py tests/test_dashboard_web.py -q`

Expected: FAIL because CN rows are filtered into “其他市场” and no A 股 filter exists.

- [ ] **Step 3: Add only the existing-layout extensions**

```javascript
// index.html, between HK and CASH
<button class="filter-button" type="button" data-market="CN">A 股</button>

// dashboard.js
{ market: "CN_STOCK", marketGroup: "CN", label: "A 股正股", className: "market-section-cn-stock" },
```

Teach `marketSectionKey` to return `CN_STOCK`, and `currentViewLabel` to display `A 股` for CN. Add `eastmoney` to existing broker labels/source kinds in `dashboard.py`, allow CN stock/ETF rows in `_build_backtest_universe`, and use the existing `normalize_backtest_symbol`. Before summary calculation, overlay only CN rows that have a valid cached latest close; recompute the five derived fields from quantity, cost value and the row's existing `fx_to_hkd`. If the CSV is absent or invalid, keep the statement row unchanged and surface no invented zero.

- [ ] **Step 4: Run Dashboard tests**

Run: `.venv/bin/pytest tests/test_dashboard.py tests/test_dashboard_web.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard.py src/open_trader/dashboard_static/index.html src/open_trader/dashboard_static/dashboard.js tests/test_dashboard.py tests/test_dashboard_web.py
git commit -m "feat: show A-share holdings in dashboard"
```

### Task 6: 真实导入、真实行情和本地 Dashboard 验收

**Files:**
- Modify: `README.zh-CN.md`
- Modify: `CHANGELOG.md`
- Runtime artifacts only: `data/runs/2026-07/`, `data/prices/CN/`, optionally `data/latest/portfolio.csv`

**Interfaces:**
- Consumes: `/Users/ray/Downloads/电子对账单.pdf`
- Produces: verified dated import, five real CN price caches, strategy results, and a restarted local Dashboard

- [ ] **Step 1: Document the exact operator workflow**

```markdown
.venv/bin/python -m open_trader import-statements \
  --month 2026-07 \
  --eastmoney /Users/ray/Downloads/电子对账单.pdf \
  --cny-hkd 1.08 \
  --data-dir data \
  --update-latest
```

Document that the command prompts for the PDF password, only imports the current summary table, and that AKShare requires no secret. Add a short dated `CHANGELOG.md` entry describing A-share statement import, AKShare daily prices, and unchanged Dashboard layout.

- [ ] **Step 2: Run the full automated suite**

Run: `.venv/bin/pytest -q`

Expected: all tests PASS with zero failures.

- [ ] **Step 3: Import the real PDF without promoting latest first**

Run interactively:

```bash
.venv/bin/python -m open_trader import-statements --month 2026-07 \
  --eastmoney /Users/ray/Downloads/电子对账单.pdf \
  --cny-hkd 1.08 --data-dir /private/tmp/open-trader-eastmoney-check
```

Expected: 5 positions, 1 cash row, zero password/account leakage, dated `portfolio.csv`, and no `data/latest/portfolio.csv`. Inspect the extracted CSV to confirm only current holdings and no transaction rows. Then rerun against `data` with `--update-latest` only after that inspection.

- [ ] **Step 4: Fetch real AKShare data and run all three strategies**

Use `AkShareDailyKlineProvider` through the existing standard-backtest workflow for each of the five imported symbols. Confirm each `data/prices/CN/<symbol>.csv` has non-zero volume, ordered dates, required columns, and a recent trading-day end date. Run `trend_pullback/v1`, `breakout_momentum/v1`, and `range_mean_reversion/v1` for at least one imported symbol and confirm each writes its normal immutable backtest artifacts.

- [ ] **Step 5: Restart and verify the real Dashboard process**

```bash
screen -ls
launchctl list | rg 'open-trader|open_trader' || true
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

Stop only the identified old Open Trader Dashboard process, then start the current checkout using the established local command:

```bash
.venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv \
  --data-dir data --reports-dir reports --poll-seconds 5 \
  --host 127.0.0.1 --port 8766
```

Record the new PID and timestamp. Verify a fresh `GET /api/dashboard` contains the five real CN holdings, `eastmoney`, and CN backtest universe rows.

- [ ] **Step 6: Verify desktop and mobile UI with Playwright**

Open `http://127.0.0.1:8766`, select `A 股`, then `东方财富`. Confirm the existing ten column headers remain in the same order, the group label is `A 股正股`, five rows appear, and no extra A-share-only cards/panels exist. Repeat at a mobile viewport and capture screenshots under `/private/tmp`; confirm the responsive behavior matches the current holdings table.

- [ ] **Step 7: Commit docs and verification-facing changes**

```bash
git add README.zh-CN.md CHANGELOG.md
git commit -m "docs: add A-share statement workflow"
git status --short
```

Expected: only pre-existing unrelated user files remain untracked or modified.
