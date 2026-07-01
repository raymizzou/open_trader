# Futu Anomaly Signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a template-driven `市场信号 · 富途异动信号` module that writes stable Futu anomaly facts for US and HK holdings and renders them in Chinese in the existing dashboard.

**Architecture:** Extend the existing `open_trader.futu_skill_facts.v1` artifact instead of creating a second artifact. Add normalized signal modules beside `news_sentiment`, use one composite extractor for news plus technical/capital/derivatives anomalies, and render one aggregated dashboard card that coexists with the current plugin grid.

**Tech Stack:** Python 3.12, argparse, JSON artifacts, subprocess-based local Futu skill scripts, pytest, existing static dashboard JavaScript/CSS, Playwright CLI for visual verification.

---

## File Structure

- Modify `src/open_trader/futu_skill_facts.py`
  - Own signal module constants, validation, normalization, script-client integration, and generation.
  - Keep the existing news/sentiment behavior in the same artifact.
- Modify `src/open_trader/cli.py`
  - Keep the existing `extract-futu-skill-facts` command.
  - Add `--window-days` and wire the composite extractor.
- Modify `src/open_trader/dashboard.py`
  - Attach normalized anomaly modules under `holding["futu_skill_facts"]`.
- Modify `src/open_trader/dashboard_static/dashboard.js`
  - Render one aggregated `市场信号 · 富途异动信号` plugin with Chinese labels.
- Modify `src/open_trader/dashboard_static/dashboard.css`
  - Add wide-card and signal-row styles that preserve the existing dashboard tone.
- Modify `tests/test_futu_skill_facts.py`
  - Add schema, normalization, generation, and script-client tests.
- Modify `tests/test_premarket_cli.py`
  - Add CLI wiring tests for the composite extractor and `--window-days`.
- Modify `tests/test_dashboard.py`
  - Add dashboard payload tests for US and HK anomaly modules.
- Modify `tests/test_dashboard_web.py`
  - Add static render tests for the aggregated signal card and Chinese-only UI labels.
- Modify `README.md` and `README.zh-CN.md` only when an existing documented
  `extract-futu-skill-facts` command example needs the new `--window-days`
  argument. Leave README files unchanged when no such section exists.

---

### Task 1: Core Anomaly Facts Contract

**Files:**
- Modify: `src/open_trader/futu_skill_facts.py`
- Test: `tests/test_futu_skill_facts.py`

- [ ] **Step 1: Write failing schema and generation tests**

Add these imports in `tests/test_futu_skill_facts.py`:

```python
from open_trader.futu_skill_facts import (
    CAPITAL_ANOMALY_CATEGORY_LABELS,
    DERIVATIVES_ANOMALY_CATEGORY_LABELS_HK,
    DERIVATIVES_ANOMALY_CATEGORY_LABELS_US,
    TECHNICAL_ANOMALY_CATEGORY_LABELS,
)
```

Add a fake extractor that returns all four modules:

```python
class FakeFullFutuSkillExtractor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def extract_news_sentiment(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
    ) -> dict[str, object]:
        return {
            "status": "ok",
            "signal": "supportive",
            "confidence": "medium",
            "freshness": {
                "generated_at": "2026-07-02T09:10:00+08:00",
                "source_window": "latest",
            },
            "evidence": [
                {
                    "title": f"{symbol} news",
                    "summary": "AI 需求继续支持市场关注。",
                    "url": f"https://example.com/{symbol.lower()}",
                }
            ],
            "blocking_reason": "",
            "suggested_constraint": "",
        }

    def extract_technical_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        self.calls.append({"module": "technical", "market": market, "symbol": symbol, "window_days": window_days})
        return {
            "status": "ok",
            "signal": "supportive",
            "confidence": "medium",
            "suggested_constraint": "",
            "window_days": window_days,
            "summary": "技术信号支持趋势，但不构成单独买入理由。",
            "categories": [
                {
                    "name": "MACD",
                    "state": "anomaly",
                    "direction": "bullish",
                    "detail": "金叉后继续放大，支持短线趋势延续。",
                    "evidence_date": "2026-07-01",
                },
                {
                    "name": "RSI",
                    "state": "anomaly",
                    "direction": "risk_up",
                    "detail": "接近超买区，追高风险上升。",
                    "evidence_date": "2026-07-02",
                },
                {
                    "name": "K线形态",
                    "state": "none",
                    "direction": "",
                    "detail": "窗口内无异常。",
                    "evidence_date": "",
                },
            ],
        }

    def extract_capital_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        self.calls.append({"module": "capital", "market": market, "symbol": symbol, "window_days": window_days})
        return {
            "status": "ok",
            "signal": "mixed",
            "confidence": "medium",
            "suggested_constraint": "no_add",
            "window_days": window_days,
            "summary": "资金流向与加仓动作存在分歧。",
            "categories": [
                {
                    "name": "资金流向",
                    "state": "anomaly",
                    "direction": "bearish",
                    "detail": "主力资金连续净流出，和加仓动作冲突。",
                    "evidence_date": "2026-07-02",
                },
                {
                    "name": "卖空情况",
                    "state": "none",
                    "direction": "",
                    "detail": "窗口内无异常。",
                    "evidence_date": "",
                },
            ],
        }

    def extract_derivatives_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        self.calls.append({"module": "derivatives", "market": market, "symbol": symbol, "window_days": window_days})
        return {
            "status": "partial",
            "signal": "risk_up",
            "confidence": "low",
            "suggested_constraint": "no_add",
            "window_days": window_days,
            "summary": "期权波动率偏高，不宜追高。",
            "categories": [
                {
                    "name": "期权波动率",
                    "state": "anomaly",
                    "direction": "risk_up",
                    "detail": "IV 位于高位，短线波动定价偏贵。",
                    "evidence_date": "2026-07-02",
                },
                {
                    "name": "期权大单",
                    "state": "anomaly",
                    "direction": "bullish",
                    "detail": "出现看涨大单，但不能单独覆盖资金分歧。",
                    "evidence_date": "2026-07-01",
                },
            ],
        }
```

Add the test:

```python
def test_generate_futu_skill_facts_writes_anomaly_modules(tmp_path: Path) -> None:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio,
        [
            {"market": "US", "symbol": "NVDA", "name": "NVIDIA", "asset_class": "stock"},
            {"market": "HK", "symbol": "00700", "name": "腾讯控股", "asset_class": "stock"},
        ],
    )
    extractor = FakeFullFutuSkillExtractor()

    result = generate_futu_skill_facts(
        portfolio_path=portfolio,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market=None,
        extractor=extractor,
        update_latest=True,
        window_days=7,
    )

    payload = load_futu_skill_facts_cache(result.run_path)
    assert result.records == 2
    assert result.generated == 2
    assert result.failed == 0
    assert payload["schema_version"] == FUTU_SKILL_FACTS_SCHEMA_VERSION
    nvda = next(record for record in payload["records"] if record["symbol"] == "NVDA")
    assert nvda["technical_anomaly"]["signal"] == "supportive"
    assert nvda["capital_anomaly"]["suggested_constraint"] == "no_add"
    assert nvda["derivatives_anomaly"]["categories"][0]["name"] == "期权波动率"
    assert [call["module"] for call in extractor.calls[:3]] == ["technical", "capital", "derivatives"]
    assert all(call["window_days"] == 7 for call in extractor.calls)
    assert result.latest_path.read_text(encoding="utf-8") == result.run_path.read_text(encoding="utf-8")
```

Add category constant tests:

```python
def test_anomaly_category_templates_are_fixed() -> None:
    assert TECHNICAL_ANOMALY_CATEGORY_LABELS[:3] == ("K线形态", "MACD", "RSI")
    assert CAPITAL_ANOMALY_CATEGORY_LABELS == ("资金分布与买卖经纪商", "资金流向", "卖空情况")
    assert DERIVATIVES_ANOMALY_CATEGORY_LABELS_US == ("期权大单", "期权波动率", "期权量价", "期权情绪", "期权综合信号")
    assert DERIVATIVES_ANOMALY_CATEGORY_LABELS_HK[:2] == ("牛熊证街货比例", "牛熊证街货价格区间")
```

Add invalid category validation:

```python
def test_validate_futu_skill_fact_record_rejects_invalid_anomaly_category_state() -> None:
    record = valid_record()
    record["technical_anomaly"]["categories"][0]["state"] = "maybe"

    with pytest.raises(ValueError, match="technical_anomaly category state is invalid"):
        validate_futu_skill_fact_record(record)
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_futu_skill_facts.py::test_generate_futu_skill_facts_writes_anomaly_modules \
  tests/test_futu_skill_facts.py::test_anomaly_category_templates_are_fixed \
  tests/test_futu_skill_facts.py::test_validate_futu_skill_fact_record_rejects_invalid_anomaly_category_state \
  -v
```

Expected: FAIL because anomaly constants, `window_days`, and anomaly module validation do not exist.

- [ ] **Step 3: Implement the core contract**

In `src/open_trader/futu_skill_facts.py`, add constants near the current enum constants:

```python
VALID_CATEGORY_STATES = {"anomaly", "none", "not_applicable", "error"}
VALID_CATEGORY_DIRECTIONS = {"", "bullish", "bearish", "neutral", "risk_up", "mixed"}
TECHNICAL_ANOMALY_CATEGORY_LABELS = (
    "K线形态",
    "MACD",
    "RSI",
    "CCI",
    "KDJ",
    "BIAS",
    "ARBR",
    "VR",
    "PSY",
    "OSC",
    "WMSR",
    "BOLL",
    "MA",
)
CAPITAL_ANOMALY_CATEGORY_LABELS = ("资金分布与买卖经纪商", "资金流向", "卖空情况")
DERIVATIVES_ANOMALY_CATEGORY_LABELS_HK = (
    "牛熊证街货比例",
    "牛熊证街货价格区间",
    "期权大单",
    "期权波动率",
    "期权量价",
    "期权情绪",
    "期权综合信号",
)
DERIVATIVES_ANOMALY_CATEGORY_LABELS_US = (
    "期权大单",
    "期权波动率",
    "期权量价",
    "期权情绪",
    "期权综合信号",
)
```

Keep `FutuSkillNewsSentimentExtractor` for existing news-only tests and add this wider protocol named `FutuSkillFactsExtractorProtocol`:

```python
class FutuSkillFactsExtractorProtocol(Protocol):
    def extract_news_sentiment(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
    ) -> dict[str, object]:
        ...

    def extract_technical_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        ...

    def extract_capital_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        ...

    def extract_derivatives_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        ...
```

Update `generate_futu_skill_facts()` signature and `_build_record()` signature:

```python
def generate_futu_skill_facts(
    *,
    portfolio_path: Path,
    data_dir: Path,
    run_date: str,
    market: MarketScope | str | None,
    extractor: FutuSkillFactsExtractorProtocol,
    update_latest: bool,
    window_days: int = 7,
) -> FutuSkillFactResult:
    effective_run_date = _validate_run_date(run_date)
    effective_window_days = _validate_window_days(window_days)
    market_scope = _market_scope(market)
    sources = _load_portfolio_sources(portfolio_path, market_scope)
    run_path = futu_skill_facts_run_path(data_dir, effective_run_date, market_scope)
    latest_path = futu_skill_facts_latest_path(data_dir, market_scope)
    prepare_sources = getattr(extractor, "prepare_sources", None)
    if callable(prepare_sources):
        prepare_sources(
            sources=sources,
            data_dir=data_dir,
            run_date=effective_run_date,
            market=market_scope,
        )
    records = [
        _build_record(
            source=source,
            run_date=effective_run_date,
            extractor=extractor,
            window_days=effective_window_days,
        )
        for source in sources
    ]
    failed = sum(1 for record in records if str(record.get("error") or ""))
    payload = {
        "schema_version": FUTU_SKILL_FACTS_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "run_date": effective_run_date,
        "market": market_scope.value if market_scope is not None else "",
        "window_days": effective_window_days,
        "records": records,
    }
    _atomic_write_json(run_path, payload)
    if update_latest:
        _atomic_write_json(latest_path, payload)
    return FutuSkillFactResult(
        run_date=effective_run_date,
        records=len(records),
        generated=len(records) - failed,
        failed=failed,
        run_path=run_path,
        latest_path=latest_path,
    )
```

Add validation helpers:

```python
def _validate_window_days(window_days: int) -> int:
    try:
        value = int(window_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("window_days must be an integer") from exc
    if value < 1 or value > 30:
        raise ValueError("window_days must be between 1 and 30")
    return value


def _normalize_signal_module(module: object, module_name: str) -> dict[str, Any]:
    if not isinstance(module, dict):
        raise ValueError(f"{module_name} module is invalid")
    normalized = {
        "status": _required_enum(module, "status", VALID_MODULE_STATUSES, module_name),
        "signal": _required_enum(module, "signal", VALID_SIGNALS, module_name),
        "confidence": _required_enum(module, "confidence", VALID_CONFIDENCES, module_name),
        "suggested_constraint": _required_enum(module, "suggested_constraint", VALID_CONSTRAINTS, module_name),
        "window_days": _validate_window_days(int(module.get("window_days") or 7)),
        "summary": _optional_text(module.get("summary")),
        "categories": _normalize_signal_categories(module.get("categories"), module_name),
    }
    _validate_signal_module(normalized, module_name)
    return normalized


def _normalize_signal_categories(categories: object, module_name: str) -> list[dict[str, str]]:
    if not isinstance(categories, list):
        raise ValueError(f"{module_name} categories is invalid")
    normalized = []
    for item in categories:
        if not isinstance(item, dict):
            raise ValueError(f"{module_name} category is invalid")
        normalized.append(
            {
                "name": _required_text(item, "name", f"{module_name} category"),
                "state": _required_enum(item, "state", VALID_CATEGORY_STATES, f"{module_name} category"),
                "direction": _required_enum(item, "direction", VALID_CATEGORY_DIRECTIONS, f"{module_name} category"),
                "detail": _required_text(item, "detail", f"{module_name} category"),
                "evidence_date": _optional_text(item.get("evidence_date")),
            }
        )
    return normalized


def _validate_signal_module(module: object, module_name: str) -> None:
    if not isinstance(module, dict):
        raise ValueError(f"{module_name} module is invalid")
    _validate_enum(module, "status", VALID_MODULE_STATUSES, module_name)
    _validate_enum(module, "signal", VALID_SIGNALS, module_name)
    _validate_enum(module, "confidence", VALID_CONFIDENCES, module_name)
    _validate_enum(module, "suggested_constraint", VALID_CONSTRAINTS, module_name)
    if not isinstance(module.get("window_days"), int):
        raise ValueError(f"{module_name} window_days is invalid")
    if not isinstance(module.get("summary"), str):
        raise ValueError(f"{module_name} summary is invalid")
    categories = module.get("categories")
    if not isinstance(categories, list):
        raise ValueError(f"{module_name} categories is invalid")
    for category in categories:
        if not isinstance(category, dict):
            raise ValueError(f"{module_name} category is invalid")
        for field in ("name", "state", "direction", "detail", "evidence_date"):
            if not isinstance(category.get(field), str):
                raise ValueError(f"{module_name} category {field} is invalid")
        _validate_enum(category, "state", VALID_CATEGORY_STATES, f"{module_name} category")
        _validate_enum(category, "direction", VALID_CATEGORY_DIRECTIONS, f"{module_name} category")


def _required_text(payload: dict[str, object], field: str, context: str) -> str:
    value = _optional_text(payload.get(field))
    if not value:
        raise ValueError(f"{context} {field} is invalid")
    return value
```

Update `validate_futu_skill_fact_record()`:

```python
    _validate_news_sentiment_module(record.get("news_sentiment"))
    _validate_signal_module(record.get("technical_anomaly"), "technical_anomaly")
    _validate_signal_module(record.get("capital_anomaly"), "capital_anomaly")
    _validate_signal_module(record.get("derivatives_anomaly"), "derivatives_anomaly")
```

Update `_build_record()` to collect each module independently:

```python
def _build_record(
    *,
    source: FutuSkillSource,
    run_date: str,
    extractor: FutuSkillFactsExtractorProtocol,
    window_days: int,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": FUTU_SKILL_FACTS_SCHEMA_VERSION,
        "run_date": run_date,
        "market": source.market,
        "symbol": source.symbol,
        "name": source.name,
    }
    errors: list[str] = []
    try:
        news_sentiment = _normalize_news_sentiment_module(
            extractor.extract_news_sentiment(
                market=source.market,
                symbol=source.symbol,
                name=source.name,
                run_date=run_date,
            )
        )
    except Exception as exc:
        news_sentiment = _error_news_sentiment_module()
        errors.append(f"news_sentiment: {str(exc) or exc.__class__.__name__}")
    try:
        technical_anomaly = _normalize_signal_module(
            extractor.extract_technical_anomaly(
                market=source.market,
                symbol=source.symbol,
                name=source.name,
                run_date=run_date,
                window_days=window_days,
            ),
            "technical_anomaly",
        )
    except Exception as exc:
        technical_anomaly = _error_signal_module("technical_anomaly", window_days, str(exc) or exc.__class__.__name__)
        errors.append(f"technical_anomaly: {str(exc) or exc.__class__.__name__}")
    try:
        capital_anomaly = _normalize_signal_module(
            extractor.extract_capital_anomaly(
                market=source.market,
                symbol=source.symbol,
                name=source.name,
                run_date=run_date,
                window_days=window_days,
            ),
            "capital_anomaly",
        )
    except Exception as exc:
        capital_anomaly = _error_signal_module("capital_anomaly", window_days, str(exc) or exc.__class__.__name__)
        errors.append(f"capital_anomaly: {str(exc) or exc.__class__.__name__}")
    try:
        derivatives_anomaly = _normalize_signal_module(
            extractor.extract_derivatives_anomaly(
                market=source.market,
                symbol=source.symbol,
                name=source.name,
                run_date=run_date,
                window_days=window_days,
            ),
            "derivatives_anomaly",
        )
    except Exception as exc:
        derivatives_anomaly = _error_signal_module("derivatives_anomaly", window_days, str(exc) or exc.__class__.__name__)
        errors.append(f"derivatives_anomaly: {str(exc) or exc.__class__.__name__}")
    record = {
        **base,
        "news_sentiment": news_sentiment,
        "technical_anomaly": technical_anomaly,
        "capital_anomaly": capital_anomaly,
        "derivatives_anomaly": derivatives_anomaly,
        "error": "; ".join(errors),
    }
    validate_futu_skill_fact_record(record)
    return record
```

Add error module helper:

```python
def _error_signal_module(module_name: str, window_days: int, reason: str) -> dict[str, Any]:
    return {
        "status": "error",
        "signal": "neutral",
        "confidence": "low",
        "suggested_constraint": "review",
        "window_days": window_days,
        "summary": reason,
        "categories": [
            {
                "name": _default_error_category_name(module_name),
                "state": "error",
                "direction": "",
                "detail": reason,
                "evidence_date": "",
            }
        ],
    }


def _default_error_category_name(module_name: str) -> str:
    return {
        "technical_anomaly": "技术异动",
        "capital_anomaly": "资金异动",
        "derivatives_anomaly": "衍生品异动",
    }[module_name]
```

Update `valid_record()` in `tests/test_futu_skill_facts.py` so it contains `technical_anomaly`, `capital_anomaly`, and `derivatives_anomaly` modules with valid categories.

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/futu_skill_facts.py tests/test_futu_skill_facts.py
git commit -m "feat: add futu anomaly facts schema"
```

---

### Task 2: Futu Anomaly Script Integration

**Files:**
- Modify: `src/open_trader/futu_skill_facts.py`
- Test: `tests/test_futu_skill_facts.py`

- [ ] **Step 1: Write failing tests for script client and normalization**

Add tests:

```python
def test_futu_anomaly_script_client_invokes_expected_scripts(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_runner(command: list[str]) -> object:
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "method": "get_technical_unusual",
                    "stock_symbol": "US.NVDA",
                    "time_range": 7,
                    "data": [
                        {
                            "name": "MACD",
                            "direction": "bullish",
                            "date": "2026-07-01",
                            "description": "MACD 金叉",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    client = FutuAnomalyScriptClient(
        skill_root=tmp_path / "skills",
        runner=fake_runner,
    )

    payload = client.run("technical", market="US", symbol="NVDA", window_days=7)

    assert payload["stock_symbol"] == "US.NVDA"
    assert calls[0][-4:] == ["US.NVDA", "--time-range", "7", "--json"]
    assert "handle_technical_anomaly.py" in calls[0][1]
```

Add an error test:

```python
def test_futu_anomaly_script_client_reports_script_failure(tmp_path: Path) -> None:
    def fake_runner(command: list[str]) -> object:
        return SimpleNamespace(returncode=1, stdout="", stderr="get_technical_unusual error: no permission")

    client = FutuAnomalyScriptClient(skill_root=tmp_path / "skills", runner=fake_runner)

    with pytest.raises(RuntimeError, match="no permission"):
        client.run("technical", market="US", symbol="NVDA", window_days=7)
```

Add a normalizer test:

```python
def test_futu_skill_facts_extractor_normalizes_fake_anomaly_payloads() -> None:
    class FakeAnomalyClient:
        def run(self, module: str, *, market: str, symbol: str, window_days: int) -> dict[str, object]:
            if module == "technical":
                return {
                    "data": [
                        {"name": "MACD", "direction": "bullish", "date": "2026-07-01", "description": "MACD 金叉"},
                        {"name": "RSI", "direction": "risk_up", "date": "2026-07-02", "description": "RSI 接近超买"},
                    ]
                }
            if module == "capital":
                return {
                    "data": [
                        {"name": "资金流向", "direction": "bearish", "date": "2026-07-02", "description": "主力资金连续净流出"}
                    ]
                }
            return {
                "data": [
                    {"name": "期权波动率", "direction": "risk_up", "date": "2026-07-02", "description": "IV 位于高位"}
                ]
            }

    extractor = FutuSkillFactsExtractor(
        news_extractor=FakeExtractor(),
        anomaly_client=FakeAnomalyClient(),
    )

    technical = extractor.extract_technical_anomaly(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-02",
        window_days=7,
    )
    capital = extractor.extract_capital_anomaly(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-02",
        window_days=7,
    )
    derivatives = extractor.extract_derivatives_anomaly(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-02",
        window_days=7,
    )

    assert technical["categories"][0]["name"] == "MACD"
    assert technical["categories"][0]["direction"] == "bullish"
    assert capital["suggested_constraint"] == "no_add"
    assert derivatives["signal"] == "risk_up"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_futu_skill_facts.py::test_futu_anomaly_script_client_invokes_expected_scripts \
  tests/test_futu_skill_facts.py::test_futu_anomaly_script_client_reports_script_failure \
  tests/test_futu_skill_facts.py::test_futu_skill_facts_extractor_normalizes_fake_anomaly_payloads \
  -v
```

Expected: FAIL because `FutuAnomalyScriptClient` and `FutuSkillFactsExtractor` do not exist.

- [ ] **Step 3: Implement the script client and composite extractor**

In `src/open_trader/futu_skill_facts.py`, add imports:

```python
import os
import subprocess
```

Add the client:

```python
class FutuAnomalyScriptClient:
    def __init__(
        self,
        *,
        skill_root: Path | None = None,
        runner: Callable[[list[str]], object] | None = None,
    ) -> None:
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self.skill_root = skill_root or codex_home / "skills"
        self.runner = runner or self._run_subprocess

    def run(
        self,
        module: str,
        *,
        market: str,
        symbol: str,
        window_days: int,
    ) -> dict[str, object]:
        script = self._script_path(module)
        stock_symbol = f"{market.upper()}.{symbol.upper()}"
        command = [
            sys.executable,
            str(script),
            stock_symbol,
            "--time-range",
            str(window_days),
            "--json",
        ]
        result = self.runner(command)
        returncode = int(getattr(result, "returncode", 1))
        stdout = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
        if returncode != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or f"{module} anomaly script failed")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{module} anomaly script returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{module} anomaly script returned non-object JSON")
        return payload

    def _script_path(self, module: str) -> Path:
        mapping = {
            "technical": self.skill_root / "futu-technical-anomaly/scripts/handle_technical_anomaly.py",
            "capital": self.skill_root / "futu-capital-anomaly/scripts/handle_capital_anomaly.py",
            "derivatives": self.skill_root / "futu-derivatives-anomaly/scripts/handle_derivatives_anomaly.py",
        }
        try:
            path = mapping[module]
        except KeyError as exc:
            raise ValueError(f"unknown anomaly module: {module}") from exc
        if not path.exists():
            raise FileNotFoundError(f"Futu anomaly script not found: {path}")
        return path

    @staticmethod
    def _run_subprocess(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            check=False,
        )
```

Add the composite extractor:

```python
class FutuSkillFactsExtractor:
    def __init__(
        self,
        *,
        news_extractor: FutuSkillNewsSentimentExtractor | None = None,
        anomaly_client: FutuAnomalyScriptClient | None = None,
    ) -> None:
        self.news_extractor = news_extractor or FutuNewsSentimentExtractor()
        self.anomaly_client = anomaly_client or FutuAnomalyScriptClient()

    def prepare_sources(
        self,
        *,
        sources: list[FutuSkillSource],
        data_dir: Path,
        run_date: str,
        market: MarketScope | None,
    ) -> None:
        prepare_sources = getattr(self.news_extractor, "prepare_sources", None)
        if callable(prepare_sources):
            prepare_sources(
                sources=sources,
                data_dir=data_dir,
                run_date=run_date,
                market=market,
            )

    def extract_news_sentiment(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
    ) -> dict[str, object]:
        return self.news_extractor.extract_news_sentiment(
            market=market,
            symbol=symbol,
            name=name,
            run_date=run_date,
        )

    def extract_technical_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        payload = self.anomaly_client.run("technical", market=market, symbol=symbol, window_days=window_days)
        return _normalize_anomaly_payload(
            payload,
            module_name="technical_anomaly",
            category_labels=TECHNICAL_ANOMALY_CATEGORY_LABELS,
            window_days=window_days,
        )

    def extract_capital_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        payload = self.anomaly_client.run("capital", market=market, symbol=symbol, window_days=window_days)
        return _normalize_anomaly_payload(
            payload,
            module_name="capital_anomaly",
            category_labels=CAPITAL_ANOMALY_CATEGORY_LABELS,
            window_days=window_days,
        )

    def extract_derivatives_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        payload = self.anomaly_client.run("derivatives", market=market, symbol=symbol, window_days=window_days)
        labels = (
            DERIVATIVES_ANOMALY_CATEGORY_LABELS_HK
            if market.upper() == "HK"
            else DERIVATIVES_ANOMALY_CATEGORY_LABELS_US
        )
        return _normalize_anomaly_payload(
            payload,
            module_name="derivatives_anomaly",
            category_labels=labels,
            window_days=window_days,
        )
```

Add payload normalization:

```python
def _normalize_anomaly_payload(
    payload: dict[str, object],
    *,
    module_name: str,
    category_labels: tuple[str, ...],
    window_days: int,
) -> dict[str, object]:
    rows = _anomaly_rows(payload.get("data"))
    categories = [
        _category_from_rows(label, rows)
        for label in category_labels
    ]
    anomaly_categories = [item for item in categories if item["state"] == "anomaly"]
    risk_categories = [item for item in anomaly_categories if item["direction"] in {"bearish", "risk_up"}]
    supportive_categories = [item for item in anomaly_categories if item["direction"] == "bullish"]
    if risk_categories:
        signal = "risk_up" if module_name == "derivatives_anomaly" else "mixed"
        suggested_constraint = "no_add"
    elif supportive_categories:
        signal = "supportive"
        suggested_constraint = ""
    elif anomaly_categories:
        signal = "mixed"
        suggested_constraint = "review"
    else:
        signal = "neutral"
        suggested_constraint = ""
    return {
        "status": "ok",
        "signal": signal,
        "confidence": "medium" if anomaly_categories else "low",
        "suggested_constraint": suggested_constraint,
        "window_days": window_days,
        "summary": _signal_summary(module_name, signal, suggested_constraint),
        "categories": categories,
    }


def _anomaly_rows(data: object) -> list[dict[str, object]]:
    if isinstance(data, dict):
        rows = []
        for value in data.values():
            rows.extend(_anomaly_rows(value))
        return rows
    if isinstance(data, list):
        rows = []
        for item in data:
            if isinstance(item, dict):
                rows.append(item)
            else:
                rows.extend(_anomaly_rows(item))
        return rows
    return []


def _category_from_rows(label: str, rows: list[dict[str, object]]) -> dict[str, str]:
    matches = [row for row in rows if _row_matches_category(row, label)]
    if not matches:
        return {
            "name": label,
            "state": "none",
            "direction": "",
            "detail": "窗口内无异常。",
            "evidence_date": "",
        }
    first = matches[0]
    detail = _row_detail_text(first)
    return {
        "name": label,
        "state": "anomaly",
        "direction": _row_direction(first),
        "detail": detail or "发现异动，详情见原始富途返回。",
        "evidence_date": _row_date(first),
    }
```

Add row helpers with deterministic keyword matching:

```python
def _row_matches_category(row: dict[str, object], label: str) -> bool:
    text = _row_text(row)
    aliases = {
        "K线形态": ("k线", "形态", "pattern"),
        "资金分布与买卖经纪商": ("资金分布", "经纪商", "broker", "funds_distribution", "funds_broker"),
        "资金流向": ("资金流向", "flow", "funds_flow"),
        "卖空情况": ("卖空", "short"),
        "牛熊证街货比例": ("牛熊证街货比例", "warrant_ratio"),
        "牛熊证街货价格区间": ("牛熊证街货价格区间", "warrant_price_distribution"),
        "期权大单": ("期权大单", "option_unusual"),
        "期权波动率": ("期权波动率", "iv", "volatility", "option_volatility"),
        "期权量价": ("期权量价", "volume", "option_volume_price"),
        "期权情绪": ("期权情绪", "put/call", "pcr", "option_sentiment"),
        "期权综合信号": ("期权综合", "option_comprehensive"),
    }
    terms = aliases.get(label, (label.casefold(),))
    return any(term.casefold() in text for term in terms)


def _row_text(row: dict[str, object]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True).casefold()


def _row_direction(row: dict[str, object]) -> str:
    text = _row_text(row)
    if any(term in text for term in ("bearish", "看跌", "偏空", "流出", "卖空", "risk_up", "风险")):
        return "risk_up" if "risk_up" in text or "风险" in text else "bearish"
    if any(term in text for term in ("bullish", "看涨", "偏多", "流入", "金叉")):
        return "bullish"
    if "mixed" in text or "分歧" in text:
        return "mixed"
    return "neutral"


def _row_detail_text(row: dict[str, object]) -> str:
    for field in ("description", "interpretation", "summary", "detail", "name"):
        value = _optional_text(row.get(field))
        if value:
            return value
    return _optional_text(row)


def _row_date(row: dict[str, object]) -> str:
    for field in ("date", "datetime", "time", "occur_date"):
        value = _optional_text(row.get(field))
        if value:
            return value
    return ""


def _signal_summary(module_name: str, signal: str, suggested_constraint: str) -> str:
    if signal == "supportive":
        return "异动信号支持当前交易方向。"
    if signal == "risk_up":
        return "异动信号提示风险上升。"
    if signal == "mixed":
        return "异动信号存在分歧，需要结合主结论复核。"
    if suggested_constraint:
        return "异动信号触发执行约束。"
    return "窗口内未发现明显异动。"
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/futu_skill_facts.py tests/test_futu_skill_facts.py
git commit -m "feat: wire futu anomaly signal extractors"
```

---

### Task 3: CLI Wiring For Window Days And Composite Extractor

**Files:**
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_premarket_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Update `test_extract_futu_skill_facts_help_includes_expected_options()`:

```python
    assert "--window-days" in output
```

Update `test_extract_futu_skill_facts_main_wires_generator()`:

```python
    class FakeCompositeExtractor:
        pass

    monkeypatch.setattr(cli, "FutuSkillFactsExtractor", lambda: FakeCompositeExtractor())
```

Add `--window-days` to the CLI invocation:

```python
            "--window-days",
            "14",
```

Add assertions:

```python
    assert captured["window_days"] == 14
    assert isinstance(captured["extractor"], FakeCompositeExtractor)
```

Add a validation test:

```python
def test_extract_futu_skill_facts_rejects_invalid_window_days(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text("market,symbol,asset_class\nUS,NVDA,stock\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "extract-futu-skill-facts",
                "--portfolio",
                str(portfolio),
                "--date",
                "2026-07-02",
                "--window-days",
                "0",
            ]
        )

    assert excinfo.value.code == 2
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_premarket_cli.py::test_extract_futu_skill_facts_help_includes_expected_options \
  tests/test_premarket_cli.py::test_extract_futu_skill_facts_main_wires_generator \
  tests/test_premarket_cli.py::test_extract_futu_skill_facts_rejects_invalid_window_days \
  -v
```

Expected: FAIL because the parser has no `--window-days` and the CLI still instantiates `FutuNewsSentimentExtractor`.

- [ ] **Step 3: Implement CLI changes**

Update imports in `src/open_trader/cli.py`:

```python
from .futu_skill_facts import FutuSkillFactsExtractor, generate_futu_skill_facts
```

Add parser argument:

```python
    futu_skill_facts_parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Natural-day anomaly window, 1-30 days. Defaults to 7.",
    )
```

Update command handling:

```python
    if args.command == "extract-futu-skill-facts":
        if not args.portfolio.exists():
            parser.error(f"portfolio CSV not found: {args.portfolio}")
        if args.window_days < 1 or args.window_days > 30:
            parser.error("window-days must be between 1 and 30")
        try:
            extractor = FutuSkillFactsExtractor()
        except Exception as exc:
            parser.error(str(exc))
        try:
            result = generate_futu_skill_facts(
                portfolio_path=args.portfolio,
                data_dir=args.data_dir,
                run_date=args.date,
                market=args.market,
                extractor=extractor,
                update_latest=args.update_latest,
                window_days=args.window_days,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"futu_skill_facts: {result.records}")
        print(f"generated: {result.generated}")
        print(f"failed: {result.failed}")
        print(f"window_days: {args.window_days}")
        print(f"futu_skill_facts_json: {result.run_path}")
        print(f"latest: {result.latest_path}")
        return 0
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_premarket_cli.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/cli.py tests/test_premarket_cli.py
git commit -m "feat: expose futu anomaly window in cli"
```

---

### Task 4: Dashboard Payload Attachment

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing dashboard payload tests**

Update helper `write_futu_skill_facts()` in `tests/test_dashboard.py` to include three anomaly modules in its record. Use this module shape:

```python
"technical_anomaly": {
    "status": "ok",
    "signal": "supportive",
    "confidence": "medium",
    "suggested_constraint": "",
    "window_days": 7,
    "summary": "技术信号支持趋势。",
    "categories": [
        {
            "name": "MACD",
            "state": "anomaly",
            "direction": "bullish",
            "detail": "金叉后继续放大。",
            "evidence_date": "2026-07-01",
        }
    ],
},
"capital_anomaly": {
    "status": "ok",
    "signal": "mixed",
    "confidence": "medium",
    "suggested_constraint": "no_add",
    "window_days": 7,
    "summary": "资金流向与加仓动作存在分歧。",
    "categories": [
        {
            "name": "资金流向",
            "state": "anomaly",
            "direction": "bearish",
            "detail": "主力资金连续净流出。",
            "evidence_date": "2026-07-02",
        }
    ],
},
"derivatives_anomaly": {
    "status": "partial",
    "signal": "risk_up",
    "confidence": "low",
    "suggested_constraint": "no_add",
    "window_days": 7,
    "summary": "期权波动率偏高。",
    "categories": [
        {
            "name": "期权波动率",
            "state": "anomaly",
            "direction": "risk_up",
            "detail": "IV 位于高位。",
            "evidence_date": "2026-07-02",
        }
    ],
},
```

Extend `test_load_dashboard_state_attaches_futu_skill_facts()`:

```python
    technical = vixy["futu_skill_facts"]["technical_anomaly"]
    capital = vixy["futu_skill_facts"]["capital_anomaly"]
    derivatives = vixy["futu_skill_facts"]["derivatives_anomaly"]
    assert technical["available"] is True
    assert technical["signal"] == "supportive"
    assert technical["categories"][0]["name"] == "MACD"
    assert capital["suggested_constraint"] == "no_add"
    assert derivatives["status"] == "partial"
```

Add missing-module behavior:

```python
def test_load_dashboard_state_marks_missing_anomaly_modules_unavailable(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(
        config.data_dir / "latest" / "portfolio.csv",
        [{"market": "US", "symbol": "VIXY", "name": "VIX ETF", "asset_class": "stock"}],
    )

    state = load_dashboard_state(config).to_dict()
    vixy = state["holdings"][0]

    assert vixy["futu_skill_facts"]["technical_anomaly"]["available"] is False
    assert vixy["futu_skill_facts"]["technical_anomaly"]["status"] == "missing"
    assert vixy["futu_skill_facts"]["capital_anomaly"]["categories"] == []
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard.py::test_load_dashboard_state_attaches_futu_skill_facts \
  tests/test_dashboard.py::test_load_dashboard_state_marks_missing_anomaly_modules_unavailable \
  -v
```

Expected: FAIL because `_futu_skill_facts_detail()` only attaches `news_sentiment`.

- [ ] **Step 3: Implement dashboard payload detail helpers**

Update `_futu_skill_facts_detail()`:

```python
def _futu_skill_facts_detail(record: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "news_sentiment": _futu_skill_news_sentiment_detail(
            record.get("news_sentiment") if isinstance(record, dict) else None
        ),
        "technical_anomaly": _futu_skill_signal_detail(
            record.get("technical_anomaly") if isinstance(record, dict) else None
        ),
        "capital_anomaly": _futu_skill_signal_detail(
            record.get("capital_anomaly") if isinstance(record, dict) else None
        ),
        "derivatives_anomaly": _futu_skill_signal_detail(
            record.get("derivatives_anomaly") if isinstance(record, dict) else None
        ),
    }
```

Add helper:

```python
def _futu_skill_signal_detail(module: object) -> dict[str, Any]:
    if not isinstance(module, dict):
        return _missing_futu_skill_signal()
    status = str(module.get("status") or "").strip()
    signal = str(module.get("signal") or "").strip()
    confidence = str(module.get("confidence") or "").strip()
    categories = module.get("categories")
    return {
        "available": bool(status and status not in {"missing", "error"}),
        "status": status or "missing",
        "signal": signal,
        "confidence": confidence,
        "suggested_constraint": str(module.get("suggested_constraint") or ""),
        "window_days": int(module.get("window_days") or 0),
        "summary": str(module.get("summary") or ""),
        "categories": categories if isinstance(categories, list) else [],
    }


def _missing_futu_skill_signal() -> dict[str, Any]:
    return {
        "available": False,
        "status": "missing",
        "signal": "",
        "confidence": "",
        "suggested_constraint": "",
        "window_days": 0,
        "summary": "",
        "categories": [],
    }
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: attach futu anomaly signals to dashboard state"
```

---

### Task 5: Dashboard Aggregated Signal Card

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Write failing static render tests**

Add a test in `tests/test_dashboard_web.py`:

```python
def test_dashboard_renders_futu_anomaly_signal_card_in_chinese() -> None:
    output = run_dashboard_js(
        """
const holding = {
  market: "US",
  symbol: "NVDA",
  name: "英伟达",
  portfolio_weight_hkd: "8.2%",
  decision_facts: {},
  futu_skill_facts: {
    technical_anomaly: {
      available: true,
      status: "ok",
      signal: "supportive",
      confidence: "medium",
      suggested_constraint: "",
      window_days: 7,
      summary: "技术信号支持趋势。",
      categories: [
        {name: "MACD", state: "anomaly", direction: "bullish", detail: "金叉后继续放大。", evidence_date: "2026-07-01"},
        {name: "RSI", state: "anomaly", direction: "risk_up", detail: "接近超买区。", evidence_date: "2026-07-02"},
        {name: "K线形态", state: "none", direction: "", detail: "窗口内无异常。", evidence_date: ""}
      ]
    },
    capital_anomaly: {
      available: true,
      status: "ok",
      signal: "mixed",
      confidence: "medium",
      suggested_constraint: "no_add",
      window_days: 7,
      summary: "资金流向与加仓动作存在分歧。",
      categories: [
        {name: "资金流向", state: "anomaly", direction: "bearish", detail: "主力资金连续净流出。", evidence_date: "2026-07-02"},
        {name: "卖空情况", state: "none", direction: "", detail: "窗口内无异常。", evidence_date: ""}
      ]
    },
    derivatives_anomaly: {
      available: true,
      status: "partial",
      signal: "risk_up",
      confidence: "low",
      suggested_constraint: "no_add",
      window_days: 7,
      summary: "期权波动率偏高。",
      categories: [
        {name: "期权波动率", state: "anomaly", direction: "risk_up", detail: "IV 位于高位。", evidence_date: "2026-07-02"},
        {name: "期权大单", state: "anomaly", direction: "bullish", detail: "出现看涨大单。", evidence_date: "2026-07-01"}
      ]
    }
  }
};
const html = renderTradingDecisionPlugins(holding);
console.log(html);
"""
    )

    for required in [
        "市场信号 · 富途异动信号",
        "技术异动",
        "资金异动",
        "衍生品异动",
        "支持",
        "不加仓",
        "部分可用",
        "偏多",
        "偏空",
        "风险上升",
        "无异常",
    ]:
        assert required in output

    for forbidden in ["supportive", "no_add", "partial", "risk_up", "bullish", "bearish", "schema"]:
        if forbidden in output:
            raise AssertionError(f"raw enum leaked into rendered signal card: {forbidden}")
```

Add static asset test:

```python
def test_dashboard_static_assets_include_futu_anomaly_signal_styles() -> None:
    js = DASHBOARD_JS.read_text(encoding="utf-8")
    css = DASHBOARD_CSS.read_text(encoding="utf-8")

    assert "futuAnomalySignalsPlugin" in js
    assert "translateFutuSignalValue" in js
    assert ".futu-signal-card" in css
    assert ".futu-signal-module-grid" in css
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_dashboard_web.py::test_dashboard_renders_futu_anomaly_signal_card_in_chinese \
  tests/test_dashboard_web.py::test_dashboard_static_assets_include_futu_anomaly_signal_styles \
  -v
```

Expected: FAIL because the renderer and styles do not exist.

- [ ] **Step 3: Implement JavaScript renderer**

In `renderTradingDecisionPlugins()`, insert the signal card after `newsSentimentPlugin(holding)`:

```javascript
    newsSentimentPlugin(holding),
    futuAnomalySignalsPlugin(holding),
```

Add renderer functions after `futuSkillNewsSentimentModule()`:

```javascript
function futuAnomalySignalsPlugin(holding) {
  const facts = holding && holding.futu_skill_facts && typeof holding.futu_skill_facts === "object"
    ? holding.futu_skill_facts
    : {};
  const modules = [
    ["technical_anomaly", "技术异动"],
    ["capital_anomaly", "资金异动"],
    ["derivatives_anomaly", "衍生品异动"],
  ].map(([key, title]) => futuSignalModuleView(facts[key], key, title));
  const available = modules.filter((module) => module.available).length;
  const overall = deriveFutuSignalOverall(modules);
  return `
    <article class="decision-plugin-card futu-signal-card">
      <div class="decision-plugin-card-header">
        <h4>市场信号 · 富途异动信号</h4>
        <span class="status-pill status-${escapeHtml(overall.tone)}">${escapeHtml(available)}/3 模块可用</span>
      </div>
      <div class="futu-signal-overall">
        <strong>${escapeHtml(overall.label)}</strong>
        <div>
          <b>${escapeHtml(overall.headline)}</b>
          <span>${escapeHtml(overall.detail)}</span>
        </div>
        <div class="futu-signal-pill-row">
          <span>${escapeHtml(translateFutuSignalValue(overall.signal))}</span>
          <span>${escapeHtml(translateFutuSignalValue(overall.constraint))}</span>
        </div>
      </div>
      <div class="futu-signal-module-grid">
        ${modules.map(renderFutuSignalModule).join("")}
      </div>
      <p class="condition-box">模板约束：模块标题、状态、方向、置信度、约束、类别顺序固定；缺失、无异常和权限失败必须显式展示。</p>
    </article>
  `;
}
```

Add helpers:

```javascript
function futuSignalModuleView(module, key, title) {
  const value = module && typeof module === "object" ? module : {};
  return {
    key,
    title,
    available: value.available === true,
    status: hasValue(value.status) ? String(value.status) : "missing",
    signal: hasValue(value.signal) ? String(value.signal) : "neutral",
    confidence: hasValue(value.confidence) ? String(value.confidence) : "low",
    suggestedConstraint: hasValue(value.suggested_constraint) ? String(value.suggested_constraint) : "",
    summary: hasValue(value.summary) ? String(value.summary) : "缺失",
    categories: Array.isArray(value.categories) ? value.categories.slice(0, 3) : [],
  };
}

function deriveFutuSignalOverall(modules) {
  const constraints = modules.map((module) => module.suggestedConstraint).filter(hasValue);
  const signals = modules.map((module) => module.signal).filter(hasValue);
  const constraint = constraints.includes("no_add")
    ? "no_add"
    : constraints.includes("review")
      ? "review"
      : "";
  if (signals.includes("risk_up") || signals.includes("mixed")) {
    return {
      tone: constraint ? "partial" : "ok",
      label: constraint ? "谨慎" : "分歧",
      signal: signals.includes("risk_up") ? "risk_up" : "mixed",
      constraint,
      headline: "市场信号存在分歧，需要结合主结论复核。",
      detail: "统一结论只来自三个模块的结构化字段；不会展示自由发挥的长段落。",
    };
  }
  if (signals.includes("supportive")) {
    return {
      tone: "ok",
      label: "支持",
      signal: "supportive",
      constraint,
      headline: "市场信号支持当前交易方向。",
      detail: "统一结论只来自三个模块的结构化字段；不会展示自由发挥的长段落。",
    };
  }
  return {
    tone: "muted",
    label: "中性",
    signal: "neutral",
    constraint,
    headline: "窗口内未发现明显异动。",
    detail: "缺失、无异常和权限失败会在模块内显式展示。",
  };
}

function renderFutuSignalModule(module) {
  return `
    <section class="futu-signal-module">
      <div class="futu-signal-module-header">
        <h5>${escapeHtml(module.title)}</h5>
        <span class="status-pill status-${escapeHtml(futuSignalStatusTone(module.status))}">${escapeHtml(translateFutuSignalValue(module.status))}</span>
      </div>
      <div class="futu-signal-metrics">
        <div><span>方向</span><strong>${escapeHtml(translateFutuSignalValue(module.signal))}</strong></div>
        <div><span>${module.suggestedConstraint ? "约束" : "置信度"}</span><strong>${escapeHtml(translateFutuSignalValue(module.suggestedConstraint || module.confidence))}</strong></div>
      </div>
      <div class="futu-signal-category-list">
        ${renderFutuSignalCategories(module.categories)}
      </div>
    </section>
  `;
}

function renderFutuSignalCategories(categories) {
  if (!categories.length) {
    return `
      <div class="futu-signal-category none">
        <div><strong>缺失</strong><span>缺失</span></div>
        <p>未找到可展示的结构化类别。</p>
      </div>
    `;
  }
  return categories.map((category) => {
    const state = hasValue(category.state) ? String(category.state) : "none";
    const direction = hasValue(category.direction) ? String(category.direction) : "";
    const date = hasValue(category.evidence_date) ? ` · ${category.evidence_date}` : "";
    return `
      <div class="futu-signal-category ${escapeHtml(futuSignalCategoryTone(state, direction))}">
        <div>
          <strong>${escapeHtml(category.name || "缺失")}</strong>
          <span>${escapeHtml(translateFutuSignalValue(direction || state) + date)}</span>
        </div>
        <p>${escapeHtml(category.detail || "缺失")}</p>
      </div>
    `;
  }).join("");
}
```

Add translation helpers:

```javascript
function translateFutuSignalValue(value) {
  const key = hasValue(value) ? String(value) : "";
  const labels = {
    supportive: "支持",
    opposing: "反对",
    neutral: "中性",
    risk_up: "风险上升",
    mixed: "分歧",
    no_add: "不加仓",
    review: "需复核",
    reduce_only: "只减不加",
    wait_for_event: "等待事件",
    ok: "正常",
    partial: "部分可用",
    missing: "缺失",
    error: "错误",
    stale: "已过期",
    anomaly: "异常",
    none: "无异常",
    not_applicable: "不适用",
    bullish: "偏多",
    bearish: "偏空",
    high: "高",
    medium: "中等",
    low: "低",
    "": "-",
  };
  return labels[key] || key;
}

function futuSignalStatusTone(status) {
  if (status === "ok") return "ok";
  if (status === "partial") return "partial";
  if (status === "stale") return "stale";
  if (status === "error") return "failed";
  return "muted";
}

function futuSignalCategoryTone(state, direction) {
  if (state === "error") return "failed";
  if (state === "none" || state === "not_applicable") return "none";
  if (direction === "bearish" || direction === "risk_up") return "watch";
  if (direction === "bullish") return "supportive";
  return "mixed";
}
```

- [ ] **Step 4: Implement CSS**

Add styles near existing decision plugin styles:

```css
.futu-signal-card {
  grid-column: 1 / -1;
}

.futu-signal-overall {
  align-items: center;
  background: #e7f3ee;
  border: 1px solid #c5dfd3;
  border-radius: 8px;
  display: grid;
  gap: 10px;
  grid-template-columns: 54px minmax(0, 1fr) auto;
  padding: 12px;
}

.futu-signal-overall > strong {
  align-items: center;
  background: var(--accent);
  border-radius: 999px;
  color: #fff;
  display: inline-flex;
  font-size: 13px;
  font-weight: 900;
  height: 46px;
  justify-content: center;
  width: 46px;
}

.futu-signal-overall b,
.futu-signal-overall span {
  display: block;
  overflow-wrap: anywhere;
}

.futu-signal-overall span {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

.futu-signal-pill-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
}

.futu-signal-pill-row span {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--accent-strong);
  font-weight: 800;
  min-height: 28px;
  padding: 6px 10px;
}

.futu-signal-module-grid {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.futu-signal-module {
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 9px;
  min-width: 0;
  padding: 10px;
}

.futu-signal-module-header {
  align-items: center;
  display: flex;
  gap: 8px;
  justify-content: space-between;
}

.futu-signal-module-header h5 {
  font-size: 14px;
  margin: 0;
}

.futu-signal-metrics {
  display: grid;
  gap: 7px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.futu-signal-metrics div,
.futu-signal-category {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 7px;
  min-width: 0;
}

.futu-signal-metrics div {
  display: grid;
  gap: 4px;
  min-height: 58px;
  padding: 8px;
}

.futu-signal-metrics span {
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
}

.futu-signal-category-list {
  display: grid;
  gap: 7px;
}

.futu-signal-category {
  border-left: 4px solid #aab3aa;
  display: grid;
  gap: 4px;
  padding: 8px;
}

.futu-signal-category.supportive {
  border-left-color: var(--ok);
}

.futu-signal-category.watch {
  border-left-color: #a16613;
}

.futu-signal-category.failed {
  border-left-color: var(--danger);
}

.futu-signal-category div {
  align-items: center;
  display: flex;
  gap: 8px;
  justify-content: space-between;
}

.futu-signal-category strong,
.futu-signal-category span,
.futu-signal-category p {
  overflow-wrap: anywhere;
}

.futu-signal-category p {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
  margin: 0;
}
```

Add responsive rules inside `@media (max-width: 760px)`:

```css
  .futu-signal-overall,
  .futu-signal-module-grid,
  .futu-signal-metrics {
    grid-template-columns: 1fr;
  }

  .futu-signal-pill-row {
    justify-content: flex-start;
  }
```

- [ ] **Step 5: Run tests and verify they pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -v
```

Expected: PASS.

- [ ] **Step 6: Generate visual screenshots**

Rerender the approved mock assets:

```bash
npx -y playwright@latest screenshot --viewport-size=1440,1200 \
  file:///Users/ray/projects/open_trader/docs/superpowers/mockups/futu-anomaly-signals-card-mock.html \
  docs/superpowers/mockups/futu-anomaly-signals-card-mock-desktop.png

npx -y playwright@latest screenshot --full-page --viewport-size=390,1200 \
  file:///Users/ray/projects/open_trader/docs/superpowers/mockups/futu-anomaly-signals-card-mock.html \
  docs/superpowers/mockups/futu-anomaly-signals-card-mock-mobile-full.png
```

Expected: both commands exit 0 and screenshots are non-empty PNG files.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py docs/superpowers/mockups/futu-anomaly-signals-card-mock-desktop.png docs/superpowers/mockups/futu-anomaly-signals-card-mock-mobile-full.png
git commit -m "feat: render futu anomaly signals card"
```

---

### Task 6: Focused Regression And Documentation Check

**Files:**
- Conditional modify: `README.md`
- Conditional modify: `README.zh-CN.md`
- Test: focused touched suites

- [ ] **Step 1: Run focused regression**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_futu_skill_facts.py \
  tests/test_premarket_cli.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  -v
```

Expected: PASS.

- [ ] **Step 2: Search for user-visible raw enum leaks**

Run:

```bash
rg -n ">[^<]*(supportive|opposing|risk_up|no_add|partial|not_applicable|bullish|bearish|schema)[^<]*<" src/open_trader/dashboard_static tests docs/superpowers/mockups
```

Expected: no matches in user-visible HTML strings. Matches in JavaScript object keys, CSS class names, or tests that assert leaks are forbidden are acceptable only when they are not visible rendered text.

- [ ] **Step 3: Check whether README command docs need updating**

Run:

```bash
rg -n "extract-futu-skill-facts|futu_skill_facts|富途.*事实|富途.*信号" README.md README.zh-CN.md
```

If no README section documents `extract-futu-skill-facts`, do not add docs in this task. If an existing section documents it, add `--window-days 7` to the example and mention that output now includes news/sentiment plus technical, capital, and derivatives anomaly modules.

Use this exact English sentence if `README.md` needs an update:

```markdown
The `extract-futu-skill-facts` command writes Futu-backed news/sentiment and anomaly signal modules; anomaly values are structured for dashboard rendering and do not change generated trade actions.
```

Use this exact Chinese sentence if `README.zh-CN.md` needs an update:

```markdown
`extract-futu-skill-facts` 会写入富途新闻/舆论和异动信号模块；异动信号只用于看板结构化展示，不会自动修改交易动作。
```

- [ ] **Step 4: Run docs-related tests if README changed**

If README files changed, run the focused regression again:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_futu_skill_facts.py \
  tests/test_premarket_cli.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  -v
```

Expected: PASS.

- [ ] **Step 5: Inspect final diff**

Run:

```bash
git diff --stat HEAD
git diff --name-status HEAD
```

Expected: only files touched by this plan appear.

- [ ] **Step 6: Commit final docs if needed**

If README files changed:

```bash
git add README.md README.zh-CN.md
git commit -m "docs: document futu anomaly signal facts"
```

If README files did not change, do not create an empty commit.
