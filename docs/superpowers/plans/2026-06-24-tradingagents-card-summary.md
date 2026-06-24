# TradingAgents Card Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate fixed TradingAgents summary fields from full TA reports and render exactly five fields in the existing `TradingAgents` dashboard card.

**Architecture:** Add a focused `open_trader.tradingagents_summary` module that reads advice, plan, and action CSVs, resolves dates/actions deterministically, calls a strict JSON LLM extractor for the core reason, validates a fixed schema, and writes market-scoped dated/latest JSON artifacts. Wire the artifact into the CLI, daily premarket pipeline, dashboard state, and existing static dashboard card without adding page-level history or source panels.

**Tech Stack:** Python 3.12, standard-library `csv`/`json`/`dataclasses`/`pathlib`, OpenAI-compatible DeepSeek client pattern from `decision_facts.py`, pytest, Node VM tests for `dashboard_static/dashboard.js`.

---

## File Structure

- Create `src/open_trader/tradingagents_summary.py`: source loading, date/action resolution, LLM extractor, validation, artifact writing, cache loading, and path helpers.
- Modify `src/open_trader/cli.py`: add `extract-tradingagents-summary` command and invoke `generate_tradingagents_summary`.
- Modify `src/open_trader/daily_premarket.py`: generate the summary after trade actions, include artifact paths, and promote latest with the existing market-scoped promotion flow.
- Modify `src/open_trader/dashboard.py`: load `data/latest/<MARKET>/tradingagents_summary.json`, attach matching records to holdings, and provide deterministic fallback rows.
- Modify `src/open_trader/dashboard_static/dashboard.js`: render the `TradingAgents` plugin card as exactly five fixed rows.
- Modify `tests/test_tradingagents_summary.py`: module unit tests.
- Modify `tests/test_dashboard.py`: dashboard payload tests.
- Modify `tests/test_dashboard_web.py`: frontend rendering tests.
- Modify `tests/test_daily_premarket.py`: daily pipeline artifact tests.
- Modify `tests/test_premarket_cli.py`: CLI parser/command tests.

---

### Task 1: Add TradingAgents Summary Module Tests

**Files:**
- Create: `tests/test_tradingagents_summary.py`
- Create later: `src/open_trader/tradingagents_summary.py`

- [ ] **Step 1: Write failing tests for paths, deterministic fields, and LLM output**

Create `tests/test_tradingagents_summary.py`:

```python
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from open_trader.tradingagents_summary import (
    MISSING_VALUE,
    TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
    LLMTradingAgentsSummaryExtractor,
    build_missing_reason_fields,
    generate_tradingagents_summary,
    load_tradingagents_summary_cache,
    tradingagents_summary_latest_path,
    tradingagents_summary_run_path,
    validate_tradingagents_summary_record,
)


ADVICE_FIELDS = [
    "run_date",
    "symbol",
    "market",
    "asset_class",
    "portfolio_weight_hkd",
    "risk_flag",
    "source",
    "advice_action",
    "advice_summary",
    "raw_decision",
    "status",
    "error",
    "source_status",
    "fallback_reason",
    "fallback_from_date",
]

PLAN_FIELDS = [
    "run_date",
    "symbol",
    "market",
    "source_status",
    "fallback_reason",
    "fallback_from_date",
    "rating",
    "entry_zone_low",
    "entry_zone_high",
    "add_price",
    "stop_loss",
    "target_1",
    "target_2",
    "max_weight",
    "catalyst",
    "time_horizon",
    "plan_text",
    "agent_reason",
    "agent_excerpt",
    "status",
    "error",
]

ACTION_FIELDS = [
    "run_date",
    "symbol",
    "market",
    "futu_symbol",
    "action",
    "status",
    "trigger_status",
    "reason",
    "agent_reason",
    "agent_excerpt",
    "suggested_quantity",
    "suggested_notional",
    "notional_currency",
    "limit_price",
    "stop_price",
    "priority",
    "invalid_fields",
]


class FakeExtractor:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or {
            "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
            "core_reason": (
                "内存超级周期仍在，但价格极度延伸、MACD 背离且财报前情绪拥挤，"
                "所以 TA 建议降低仓位而非清仓。"
            ),
            "reason_fields": {
                "main_judgment": "结构性主题仍成立，但短期风险回报转差",
                "evidence_1": "价格远高于均线并出现 MACD 背离",
                "evidence_2": "财报前情绪拥挤，失望风险放大",
                "risk_or_counterpoint": "AI 内存超级周期仍支撑保留部分仓位",
                "action_logic": "减仓锁定收益，而不是完全清仓",
            },
        }
        self.calls: list[dict[str, str]] = []

    def extract(
        self,
        *,
        market: str,
        symbol: str,
        latest_run_date: str,
        ta_report_date: str,
        advice_action: str,
        current_action: str,
        advice_summary: str,
        final_trade_decision: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "market": market,
                "symbol": symbol,
                "latest_run_date": latest_run_date,
                "ta_report_date": ta_report_date,
                "advice_action": advice_action,
                "current_action": current_action,
                "advice_summary": advice_summary,
                "final_trade_decision": final_trade_decision,
            }
        )
        return self.payload


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def raw_decision(final_trade_decision: str = "FINAL TRANSACTION PROPOSAL: HOLD") -> str:
    return json.dumps(
        {"state": {"final_trade_decision": final_trade_decision}},
        ensure_ascii=False,
    )


def advice_row(**overrides: str) -> dict[str, str]:
    row = {
        "run_date": "2026-06-23",
        "symbol": "DRAM",
        "market": "US",
        "asset_class": "etf",
        "portfolio_weight_hkd": "7.11%",
        "risk_flag": "normal",
        "source": "tradingagents",
        "advice_action": "Underweight",
        "advice_summary": (
            "评级：Underweight\n"
            "操作计划：Trim current exposure.\n"
            "理由：The memory supercycle is intact, but price is extended and MACD divergence raises event risk."
        ),
        "raw_decision": raw_decision("Rating: Underweight because price is extended."),
        "status": "ok",
        "error": "",
        "source_status": "fallback",
        "fallback_reason": "Too Many Requests",
        "fallback_from_date": "2026-06-22",
    }
    row.update(overrides)
    return row


def plan_row(**overrides: str) -> dict[str, str]:
    row = {
        "run_date": "2026-06-23",
        "symbol": "DRAM",
        "market": "US",
        "source_status": "fallback",
        "fallback_reason": "Too Many Requests",
        "fallback_from_date": "2026-06-22",
        "rating": "Underweight",
        "entry_zone_low": "",
        "entry_zone_high": "",
        "add_price": "",
        "stop_loss": "70",
        "target_1": "76",
        "target_2": "",
        "max_weight": "",
        "catalyst": "",
        "time_horizon": "",
        "plan_text": "",
        "agent_reason": "TradingAgents建议减仓，理由是技术动能转弱、风险回报不利。",
        "agent_excerpt": "The memory supercycle is intact, but price is extended.",
        "status": "active",
        "error": "",
    }
    row.update(overrides)
    return row


def action_row(**overrides: str) -> dict[str, str]:
    row = {
        "run_date": "2026-06-23",
        "symbol": "DRAM",
        "market": "US",
        "futu_symbol": "US.DRAM",
        "action": "TRIM",
        "status": "ready",
        "trigger_status": "target_1_hit",
        "reason": "Current price is at or above target 1.",
        "agent_reason": "TradingAgents建议减仓，理由是技术动能转弱、风险回报不利。",
        "agent_excerpt": "",
        "suggested_quantity": "10",
        "suggested_notional": "800",
        "notional_currency": "USD",
        "limit_price": "80",
        "stop_price": "70",
        "priority": "normal",
        "invalid_fields": "",
    }
    row.update(overrides)
    return row


def test_paths_are_market_scoped(tmp_path: Path) -> None:
    assert tradingagents_summary_run_path(tmp_path, "2026-06-23", "US") == (
        tmp_path / "runs" / "2026-06-23" / "US" / "tradingagents_summary.json"
    )
    assert tradingagents_summary_latest_path(tmp_path, "US") == (
        tmp_path / "latest" / "US" / "tradingagents_summary.json"
    )


def test_generate_summary_uses_fallback_date_and_fixed_fields(tmp_path: Path) -> None:
    advice_path = tmp_path / "data" / "latest" / "US" / "trading_advice.csv"
    plan_path = tmp_path / "data" / "latest" / "US" / "trading_plan.csv"
    actions_path = tmp_path / "data" / "latest" / "US" / "trade_actions.csv"
    write_csv(advice_path, ADVICE_FIELDS, [advice_row()])
    write_csv(plan_path, PLAN_FIELDS, [plan_row()])
    write_csv(actions_path, ACTION_FIELDS, [action_row()])

    extractor = FakeExtractor()
    result = generate_tradingagents_summary(
        advice_path=advice_path,
        plan_path=plan_path,
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-23",
        market="US",
        extractor=extractor,
        update_latest=True,
    )

    payload = load_tradingagents_summary_cache(result.latest_path)
    record = payload["records"][0]
    assert record["schema_version"] == TRADINGAGENTS_SUMMARY_SCHEMA_VERSION
    assert record["latest_run_date"] == "2026-06-23"
    assert record["ta_report_date"] == "2026-06-22"
    assert record["ta_view"] == "低配"
    assert record["current_action"] == "减仓"
    assert "目标价" not in record["core_reason"]
    assert result.records == 1
    assert result.extracted == 1
    assert extractor.calls[0]["final_trade_decision"].startswith("Rating: Underweight")


def test_validate_rejects_price_trigger_only_reason() -> None:
    record = {
        "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
        "market": "US",
        "symbol": "DRAM",
        "latest_run_date": "2026-06-23",
        "ta_report_date": "2026-06-22",
        "ta_view": "低配",
        "current_action": "减仓",
        "core_reason": "当前价格已达到或高于第一目标价。",
        "reason_fields": build_missing_reason_fields(),
        "source_hash": "sha256:" + "a" * 64,
        "error": "",
    }

    with pytest.raises(ValueError, match="price trigger"):
        validate_tradingagents_summary_record(record)


def test_failed_llm_keeps_all_display_fields(tmp_path: Path) -> None:
    advice_path = tmp_path / "data" / "latest" / "US" / "trading_advice.csv"
    plan_path = tmp_path / "data" / "latest" / "US" / "trading_plan.csv"
    actions_path = tmp_path / "data" / "latest" / "US" / "trade_actions.csv"
    write_csv(advice_path, ADVICE_FIELDS, [advice_row()])
    write_csv(plan_path, PLAN_FIELDS, [plan_row()])
    write_csv(actions_path, ACTION_FIELDS, [action_row()])

    class BrokenExtractor:
        def extract(self, **kwargs: str) -> dict[str, object]:
            raise ValueError("bad json")

    result = generate_tradingagents_summary(
        advice_path=advice_path,
        plan_path=plan_path,
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-23",
        market="US",
        extractor=BrokenExtractor(),
        update_latest=False,
    )

    payload = load_tradingagents_summary_cache(result.run_path)
    record = payload["records"][0]
    assert record["ta_view"] == "低配"
    assert record["current_action"] == "减仓"
    assert record["core_reason"].startswith("TradingAgents建议减仓")
    assert record["ta_report_date"] == "2026-06-22"
    assert record["latest_run_date"] == "2026-06-23"
    assert record["error"] == "bad json"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_tradingagents_summary.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'open_trader.tradingagents_summary'`.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_tradingagents_summary.py
git commit -m "test: define TradingAgents summary contract"
```

---

### Task 2: Implement TradingAgents Summary Module

**Files:**
- Create: `src/open_trader/tradingagents_summary.py`
- Modify: `tests/test_tradingagents_summary.py` only if imports need formatting after implementation

- [ ] **Step 1: Add the module constants, dataclasses, and path/cache helpers**

Create `src/open_trader/tradingagents_summary.py` with the following foundation:

```python
from __future__ import annotations

import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from openai import OpenAI

from .advice.change_classifier import DEEPSEEK_BASE_URL, DEFAULT_CLASSIFIER_MODEL
from .market_scope import MarketScope, market_run_dir, market_scoped_latest_path, parse_market_scope
from .technical_facts import source_hash


TRADINGAGENTS_SUMMARY_SCHEMA_VERSION = "open_trader.tradingagents_summary.v1"
MISSING_VALUE = "缺失"
REASON_FIELD_NAMES = (
    "main_judgment",
    "evidence_1",
    "evidence_2",
    "risk_or_counterpoint",
    "action_logic",
)
RUN_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CHINESE_TEXT_PATTERN = re.compile(r"[\u3400-\u9fff]")
SOURCE_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
PRICE_TRIGGER_PATTERN = re.compile(
    r"(?:目标价|止损价|第一目标|第二目标|止损|target\s*[12]?|stop\s*loss)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TradingAgentsSummaryResult:
    run_date: str
    records: int
    extracted: int
    failed: int
    run_path: Path
    latest_path: Path


@dataclass(frozen=True)
class AdviceSummarySource:
    run_date: str
    market: str
    symbol: str
    advice_action: str
    advice_summary: str
    raw_decision: str
    source_status: str
    fallback_from_date: str


@dataclass(frozen=True)
class PlanSummarySource:
    run_date: str
    market: str
    symbol: str
    rating: str
    agent_reason: str
    agent_excerpt: str


@dataclass(frozen=True)
class ActionSummarySource:
    run_date: str
    market: str
    symbol: str
    action: str
    reason: str
    agent_reason: str


class TradingAgentsSummaryExtractor(Protocol):
    def extract(
        self,
        *,
        market: str,
        symbol: str,
        latest_run_date: str,
        ta_report_date: str,
        advice_action: str,
        current_action: str,
        advice_summary: str,
        final_trade_decision: str,
    ) -> dict[str, object]:
        ...


def tradingagents_summary_run_path(
    data_dir: Path,
    run_date: str,
    market: MarketScope | str | None = None,
) -> Path:
    scope = _market_scope(market)
    if scope is not None:
        return market_run_dir(data_dir, run_date, scope) / "tradingagents_summary.json"
    return data_dir / "runs" / run_date / "tradingagents_summary.json"


def tradingagents_summary_latest_path(
    data_dir: Path,
    market: MarketScope | str | None = None,
) -> Path:
    scope = _market_scope(market)
    if scope is not None:
        return market_scoped_latest_path(data_dir, scope, "tradingagents_summary.json")
    return data_dir / "latest" / "tradingagents_summary.json"


def load_tradingagents_summary_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def index_tradingagents_summary_by_market_symbol(
    payload: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        return {}
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        market = str(record.get("market") or "").strip().upper()
        symbol = str(record.get("symbol") or "").strip().upper()
        if market and symbol:
            indexed[(market, symbol)] = record
    return indexed


def build_missing_reason_fields() -> dict[str, str]:
    return {field: MISSING_VALUE for field in REASON_FIELD_NAMES}
```

- [ ] **Step 2: Add CSV loaders and deterministic mapping helpers**

Append:

```python
def load_advice_summary_sources(path: Path) -> list[AdviceSummarySource]:
    if not path.exists():
        raise FileNotFoundError(f"advice CSV not found: {path}")
    csv.field_size_limit(sys.maxsize)
    rows: list[AdviceSummarySource] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            market = (row.get("market") or "").strip().upper()
            symbol = (row.get("symbol") or "").strip().upper()
            if not market or not symbol:
                continue
            rows.append(
                AdviceSummarySource(
                    run_date=(row.get("run_date") or "").strip(),
                    market=market,
                    symbol=symbol,
                    advice_action=(row.get("advice_action") or "").strip(),
                    advice_summary=(row.get("advice_summary") or "").strip(),
                    raw_decision=(row.get("raw_decision") or "").strip(),
                    source_status=(row.get("source_status") or row.get("status") or "").strip(),
                    fallback_from_date=(row.get("fallback_from_date") or "").strip(),
                )
            )
    return rows


def load_plan_summary_sources(path: Path) -> dict[tuple[str, str], PlanSummarySource]:
    if not path.exists():
        return {}
    csv.field_size_limit(sys.maxsize)
    rows: dict[tuple[str, str], PlanSummarySource] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            market = (row.get("market") or "").strip().upper()
            symbol = (row.get("symbol") or "").strip().upper()
            if not market or not symbol:
                continue
            rows[(market, symbol)] = PlanSummarySource(
                run_date=(row.get("run_date") or "").strip(),
                market=market,
                symbol=symbol,
                rating=(row.get("rating") or "").strip(),
                agent_reason=(row.get("agent_reason") or "").strip(),
                agent_excerpt=(row.get("agent_excerpt") or "").strip(),
            )
    return rows


def load_action_summary_sources(path: Path) -> dict[tuple[str, str], ActionSummarySource]:
    if not path.exists():
        return {}
    csv.field_size_limit(sys.maxsize)
    rows: dict[tuple[str, str], ActionSummarySource] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            market = (row.get("market") or "").strip().upper()
            symbol = (row.get("symbol") or "").strip().upper()
            if not market or not symbol:
                continue
            rows[(market, symbol)] = ActionSummarySource(
                run_date=(row.get("run_date") or "").strip(),
                market=market,
                symbol=symbol,
                action=(row.get("action") or "").strip(),
                reason=(row.get("reason") or "").strip(),
                agent_reason=(row.get("agent_reason") or "").strip(),
            )
    return rows


def normalize_ta_view(value: str) -> str:
    text = value.strip()
    lowered = text.lower()
    if any(word in lowered for word in ("underweight", "reduce", "trim")):
        return "低配"
    if any(word in lowered for word in ("overweight", "buy", "accumulate", "add")):
        return "超配"
    if "sell" in lowered:
        return "卖出"
    if "hold" in lowered:
        return "持有"
    if "neutral" in lowered:
        return "中性"
    return text if _contains_chinese(text) else MISSING_VALUE


def normalize_current_action(value: str) -> str:
    text = value.strip()
    upper = text.upper()
    labels = {
        "BUY": "买入",
        "ADD": "加仓",
        "TRIM": "减仓",
        "REDUCE": "减仓",
        "SELL": "卖出",
        "SELL_STOP": "止损卖出",
        "TAKE_PROFIT": "止盈卖出",
        "HOLD": "持有",
        "WATCH": "观察",
        "REVIEW": "人工复核",
    }
    if upper in labels:
        return labels[upper]
    lowered = text.lower()
    if any(word in lowered for word in ("trim", "reduce", "underweight")):
        return "减仓"
    if any(word in lowered for word in ("buy", "add", "accumulate")):
        return "加仓"
    if "sell" in lowered:
        return "卖出"
    if "hold" in lowered:
        return "持有"
    return text if _contains_chinese(text) else MISSING_VALUE


def extract_final_trade_decision(raw_decision: str) -> str:
    try:
        payload = json.loads(raw_decision or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    state = payload.get("state")
    if not isinstance(state, dict):
        return ""
    value = state.get("final_trade_decision")
    return value if isinstance(value, str) else ""
```

- [ ] **Step 3: Add LLM extractor and validation**

Append:

```python
class OpenAITextClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEEPSEEK_BASE_URL,
        model: str = DEFAULT_CLASSIFIER_MODEL,
    ) -> None:
        self.model = model
        self.client = OpenAI(
            api_key=api_key or os.environ.get("DEEPSEEK_API_KEY"),
            base_url=base_url,
        )

    def create(self, *, messages: list[dict[str, str]], temperature: float) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        return content or ""


class LLMTradingAgentsSummaryExtractor:
    def __init__(self, *, client: object | None = None) -> None:
        self.client = client or OpenAITextClient()

    def extract(
        self,
        *,
        market: str,
        symbol: str,
        latest_run_date: str,
        ta_report_date: str,
        advice_action: str,
        current_action: str,
        advice_summary: str,
        final_trade_decision: str,
    ) -> dict[str, object]:
        messages = [
            {"role": "system", "content": _system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "market": market,
                        "symbol": symbol,
                        "latest_run_date": latest_run_date,
                        "ta_report_date": ta_report_date,
                        "advice_action": advice_action,
                        "current_action": current_action,
                        "advice_summary": advice_summary,
                        "final_trade_decision": final_trade_decision,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        content = self.client.create(messages=messages, temperature=0)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM TradingAgents summary response must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("LLM TradingAgents summary response must be a JSON object")
        return _validate_llm_payload(payload)


def _system_prompt() -> str:
    return (
        "你负责从 TradingAgents 完整交易报告中抽取固定中文摘要。"
        "只输出 JSON，不要输出 Markdown。schema_version 必须是 "
        f"{TRADINGAGENTS_SUMMARY_SCHEMA_VERSION}。"
        "必须输出 core_reason 和 reason_fields。"
        "core_reason 是一句中文，80 到 120 个中文字符左右，说明为什么 TradingAgents 得出该观点，"
        "不能只写达到目标价、触发止损、进入买入区间等价格触发原因。"
        "reason_fields 必须包含 main_judgment、evidence_1、evidence_2、risk_or_counterpoint、action_logic。"
        "所有字段必须是中文字符串；缺失信息写 缺失。"
        "不要复制英文原文，不要输出下单指令、券商指令或详细仓位比例。"
    )


def _validate_llm_payload(payload: dict[str, object]) -> dict[str, object]:
    schema = payload.get("schema_version", TRADINGAGENTS_SUMMARY_SCHEMA_VERSION)
    if schema != TRADINGAGENTS_SUMMARY_SCHEMA_VERSION:
        raise ValueError("TradingAgents summary schema_version is invalid")
    core_reason = _clean_display_text(payload.get("core_reason"))
    reason_fields_raw = payload.get("reason_fields")
    if not isinstance(reason_fields_raw, dict):
        raise ValueError("TradingAgents summary reason_fields must be an object")
    reason_fields = {
        field: _clean_display_text(reason_fields_raw.get(field))
        for field in REASON_FIELD_NAMES
    }
    return {
        "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
        "core_reason": core_reason,
        "reason_fields": reason_fields,
    }


def validate_tradingagents_summary_record(record: dict[str, object]) -> None:
    required = (
        "schema_version",
        "market",
        "symbol",
        "latest_run_date",
        "ta_report_date",
        "ta_view",
        "current_action",
        "core_reason",
        "reason_fields",
        "source_hash",
        "error",
    )
    for field in required:
        if field not in record:
            raise ValueError(f"TradingAgents summary {field} is missing")
    if record["schema_version"] != TRADINGAGENTS_SUMMARY_SCHEMA_VERSION:
        raise ValueError("TradingAgents summary schema_version is invalid")
    for field in ("market", "symbol", "latest_run_date", "ta_report_date", "ta_view", "current_action", "core_reason", "error"):
        if not isinstance(record[field], str):
            raise ValueError(f"TradingAgents summary {field} must be a string")
    for field in ("latest_run_date", "ta_report_date"):
        value = str(record[field])
        if value != MISSING_VALUE and not _is_valid_run_date(value):
            raise ValueError(f"TradingAgents summary {field} is invalid")
    for field in ("ta_view", "current_action", "core_reason"):
        value = str(record[field]).strip()
        if not value:
            raise ValueError(f"TradingAgents summary {field} is blank")
        if value != MISSING_VALUE and not _contains_chinese(value):
            raise ValueError(f"TradingAgents summary {field} must be Chinese")
    core_reason = str(record["core_reason"])
    if _is_price_trigger_only(core_reason):
        raise ValueError("TradingAgents summary core_reason cannot be only a price trigger")
    reason_fields = record["reason_fields"]
    if not isinstance(reason_fields, dict):
        raise ValueError("TradingAgents summary reason_fields must be an object")
    if set(reason_fields) != set(REASON_FIELD_NAMES):
        raise ValueError("TradingAgents summary reason_fields have unexpected fields")
    for field in REASON_FIELD_NAMES:
        value = reason_fields[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"TradingAgents summary reason_fields.{field} is invalid")
        if value != MISSING_VALUE and not _contains_chinese(value):
            raise ValueError(f"TradingAgents summary reason_fields.{field} must be Chinese")
    source_hash_value = str(record["source_hash"])
    if source_hash_value and not SOURCE_HASH_PATTERN.fullmatch(source_hash_value):
        raise ValueError("TradingAgents summary source_hash is invalid")
```

- [ ] **Step 4: Add generation and fallback logic**

Append:

```python
def generate_tradingagents_summary(
    *,
    advice_path: Path,
    plan_path: Path,
    actions_path: Path,
    data_dir: Path,
    run_date: str | None,
    extractor: TradingAgentsSummaryExtractor,
    update_latest: bool,
    market: MarketScope | str | None = None,
) -> TradingAgentsSummaryResult:
    advice_sources = load_advice_summary_sources(advice_path)
    plan_sources = load_plan_summary_sources(plan_path)
    action_sources = load_action_summary_sources(actions_path)
    scope = _market_scope(market)
    scoped_sources = [
        source
        for source in advice_sources
        if scope is None or source.market == scope.value
    ]
    _validate_source_run_dates(scoped_sources)
    effective_run_date = _select_run_date(scoped_sources, run_date)
    filtered_sources = [
        source
        for source in scoped_sources
        if not source.run_date or source.run_date == effective_run_date
    ]
    if run_date is not None and not filtered_sources:
        raise ValueError(f"no advice rows match run_date {effective_run_date}")

    records = [
        _build_record(
            source=source,
            plan=plan_sources.get((source.market, source.symbol)),
            action=action_sources.get((source.market, source.symbol)),
            latest_run_date=effective_run_date,
            extractor=extractor,
        )
        for source in filtered_sources
    ]
    payload = {
        "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "latest_run_date": effective_run_date,
        "market": scope.value if scope is not None else "",
        "records": records,
    }
    run_path = tradingagents_summary_run_path(data_dir, effective_run_date, scope)
    latest_path = tradingagents_summary_latest_path(data_dir, scope)
    _atomic_write_json(run_path, payload)
    if update_latest:
        _atomic_write_json(latest_path, payload)
    failed = sum(1 for record in records if str(record.get("error") or ""))
    return TradingAgentsSummaryResult(
        run_date=effective_run_date,
        records=len(records),
        extracted=len(records) - failed,
        failed=failed,
        run_path=run_path,
        latest_path=latest_path,
    )


def _build_record(
    *,
    source: AdviceSummarySource,
    plan: PlanSummarySource | None,
    action: ActionSummarySource | None,
    latest_run_date: str,
    extractor: TradingAgentsSummaryExtractor,
) -> dict[str, object]:
    ta_report_date = source.fallback_from_date or source.run_date or latest_run_date
    ta_view = normalize_ta_view(source.advice_action or (plan.rating if plan else ""))
    current_action = normalize_current_action(action.action if action else "")
    base = {
        "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
        "market": source.market,
        "symbol": source.symbol,
        "latest_run_date": latest_run_date,
        "ta_report_date": ta_report_date or MISSING_VALUE,
        "ta_view": ta_view,
        "current_action": current_action,
        "source_hash": source_hash(
            "\n\n".join(
                part
                for part in (
                    source.advice_summary,
                    extract_final_trade_decision(source.raw_decision),
                )
                if part
            )
        ),
    }
    try:
        payload = extractor.extract(
            market=source.market,
            symbol=source.symbol,
            latest_run_date=latest_run_date,
            ta_report_date=ta_report_date,
            advice_action=source.advice_action,
            current_action=current_action,
            advice_summary=source.advice_summary,
            final_trade_decision=extract_final_trade_decision(source.raw_decision),
        )
        core_reason = str(payload["core_reason"])
        reason_fields = payload["reason_fields"]
        error = ""
    except Exception as exc:
        core_reason = _fallback_core_reason(plan=plan, action=action)
        reason_fields = build_missing_reason_fields()
        error = str(exc)

    record = {
        **base,
        "core_reason": core_reason or MISSING_VALUE,
        "reason_fields": reason_fields,
        "error": error,
    }
    validate_tradingagents_summary_record(record)
    return record


def _fallback_core_reason(
    *,
    plan: PlanSummarySource | None,
    action: ActionSummarySource | None,
) -> str:
    for value in (
        plan.agent_reason if plan else "",
        action.agent_reason if action else "",
    ):
        text = " ".join(value.split()).strip()
        if text and _contains_chinese(text) and not _is_price_trigger_only(text):
            return text
    return MISSING_VALUE
```

- [ ] **Step 5: Add private utility functions**

Append:

```python
def _clean_display_text(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return MISSING_VALUE
    if text != MISSING_VALUE and not _contains_chinese(text):
        raise ValueError("TradingAgents summary display value must be Chinese")
    return text


def _is_price_trigger_only(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped == MISSING_VALUE:
        return False
    if not PRICE_TRIGGER_PATTERN.search(stripped):
        return False
    investment_terms = ("趋势", "估值", "财报", "情绪", "周期", "风险回报", "动能", "基本面", "催化", "拥挤")
    return not any(term in stripped for term in investment_terms)


def _contains_chinese(text: str) -> bool:
    return bool(CHINESE_TEXT_PATTERN.search(text))


def _validate_source_run_dates(sources: list[AdviceSummarySource]) -> None:
    for source in sources:
        if source.run_date and not _is_valid_run_date(source.run_date):
            raise ValueError("run_date must be YYYY-MM-DD")


def _select_run_date(sources: list[AdviceSummarySource], run_date: str | None) -> str:
    if run_date is not None and run_date.strip():
        if not _is_valid_run_date(run_date.strip()):
            raise ValueError("run_date must be YYYY-MM-DD")
        return run_date.strip()
    dates = sorted({source.run_date for source in sources if source.run_date})
    if not dates:
        raise ValueError("run_date must be YYYY-MM-DD")
    return dates[-1]


def _is_valid_run_date(run_date: str) -> bool:
    if not RUN_DATE_PATTERN.fullmatch(run_date):
        return False
    try:
        datetime.strptime(run_date, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _now_text() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)


def _market_scope(market: MarketScope | str | None) -> MarketScope | None:
    if market is None:
        return None
    if isinstance(market, MarketScope):
        return market
    return parse_market_scope(market)
```

- [ ] **Step 6: Run module tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_tradingagents_summary.py -q
```

Expected: all tests in `tests/test_tradingagents_summary.py` pass.

- [ ] **Step 7: Commit module implementation**

```bash
git add src/open_trader/tradingagents_summary.py tests/test_tradingagents_summary.py
git commit -m "feat: generate TradingAgents summaries"
```

---

### Task 3: Add CLI Command

**Files:**
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_premarket_cli.py`

- [ ] **Step 1: Add failing CLI test**

Append to `tests/test_premarket_cli.py`:

```python
def test_extract_tradingagents_summary_command_writes_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from open_trader import cli

    advice_path = tmp_path / "advice.csv"
    plan_path = tmp_path / "plan.csv"
    actions_path = tmp_path / "actions.csv"
    data_dir = tmp_path / "data"
    advice_path.write_text(
        "run_date,symbol,market,advice_action,advice_summary,raw_decision,status,source_status,fallback_from_date\n"
        "2026-06-23,DRAM,US,Underweight,理由：估值和技术风险上升,\"{}\",ok,,\n",
        encoding="utf-8",
    )
    plan_path.write_text(
        "run_date,symbol,market,rating,agent_reason,agent_excerpt,status\n"
        "2026-06-23,DRAM,US,Underweight,TradingAgents建议减仓，理由是技术动能转弱。,,active\n",
        encoding="utf-8",
    )
    actions_path.write_text(
        "run_date,symbol,market,action,reason,agent_reason,status\n"
        "2026-06-23,DRAM,US,TRIM,Current price is at or above target 1.,TradingAgents建议减仓，理由是技术动能转弱。,ready\n",
        encoding="utf-8",
    )

    class FakeExtractor:
        def extract(self, **kwargs: str) -> dict[str, object]:
            return {
                "schema_version": "open_trader.tradingagents_summary.v1",
                "core_reason": "估值和技术风险同时上升，但长期主题仍在，所以 TA 建议降低仓位。",
                "reason_fields": {
                    "main_judgment": "短期风险回报转差",
                    "evidence_1": "技术风险上升",
                    "evidence_2": "估值压力上升",
                    "risk_or_counterpoint": "长期主题仍在",
                    "action_logic": "降低仓位而不是清仓",
                },
            }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cli, "LLMTradingAgentsSummaryExtractor", lambda: FakeExtractor())
    try:
        code = cli.main(
            [
                "extract-tradingagents-summary",
                "--advice",
                str(advice_path),
                "--plan",
                str(plan_path),
                "--actions",
                str(actions_path),
                "--data-dir",
                str(data_dir),
                "--date",
                "2026-06-23",
                "--market",
                "US",
                "--update-latest",
            ]
        )
    finally:
        monkeypatch.undo()

    output = capsys.readouterr().out
    assert code == 0
    assert "run_date: 2026-06-23" in output
    assert "summaries: 1" in output
    assert "summary_json:" in output
    assert "latest:" in output
```

- [ ] **Step 2: Run the CLI test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py::test_extract_tradingagents_summary_command_writes_paths -q
```

Expected: fail because `extract-tradingagents-summary` is not registered.

- [ ] **Step 3: Wire imports and parser command**

Modify `src/open_trader/cli.py` imports:

```python
from .tradingagents_summary import (
    LLMTradingAgentsSummaryExtractor,
    generate_tradingagents_summary,
)
```

Add parser setup near `extract-decision-facts`:

```python
    ta_summary_parser = subparsers.add_parser(
        "extract-tradingagents-summary",
        help="Extract fixed TradingAgents card summary fields",
    )
    ta_summary_parser.add_argument("--advice", type=Path, required=True)
    ta_summary_parser.add_argument("--plan", type=Path, required=True)
    ta_summary_parser.add_argument("--actions", type=Path, required=True)
    ta_summary_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    ta_summary_parser.add_argument("--date", type=canonical_date)
    ta_summary_parser.add_argument("--market", type=canonical_market, choices=["HK", "US"])
    ta_summary_parser.add_argument(
        "--update-latest",
        action="store_true",
        help="Update data/latest tradingagents_summary.json after writing dated artifact",
    )
```

- [ ] **Step 4: Wire command handler**

Add before `dashboard` command handling:

```python
    if args.command == "extract-tradingagents-summary":
        try:
            result = generate_tradingagents_summary(
                advice_path=args.advice,
                plan_path=args.plan,
                actions_path=args.actions,
                data_dir=args.data_dir,
                run_date=args.date,
                market=args.market,
                extractor=LLMTradingAgentsSummaryExtractor(),
                update_latest=args.update_latest,
            )
        except (FileNotFoundError, ValueError) as exc:
            parser.error(str(exc))
        print(f"run_date: {result.run_date}")
        print(f"summaries: {result.records}")
        print(f"extracted: {result.extracted}")
        print(f"failed: {result.failed}")
        print(f"summary_json: {result.run_path}")
        print(f"latest: {result.latest_path}")
        return 0
```

- [ ] **Step 5: Run CLI test**

Run:

```bash
.venv/bin/python -m pytest tests/test_premarket_cli.py::test_extract_tradingagents_summary_command_writes_paths -q
```

Expected: pass.

- [ ] **Step 6: Commit CLI changes**

```bash
git add src/open_trader/cli.py tests/test_premarket_cli.py
git commit -m "feat: add TradingAgents summary CLI"
```

---

### Task 4: Attach Summary Records To Dashboard State

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Add failing dashboard payload test**

Append to `tests/test_dashboard.py`:

```python
def write_tradingagents_summary(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.tradingagents_summary.v1",
                "generated_at": "2026-06-23T18:37:04+08:00",
                "latest_run_date": "2026-06-23",
                "market": "US",
                "records": [
                    {
                        "schema_version": "open_trader.tradingagents_summary.v1",
                        "market": "US",
                        "symbol": "DRAM",
                        "latest_run_date": "2026-06-23",
                        "ta_report_date": "2026-06-22",
                        "ta_view": "低配",
                        "current_action": "减仓",
                        "core_reason": "内存超级周期仍在，但技术风险上升，所以 TA 建议降低仓位。",
                        "reason_fields": {
                            "main_judgment": "短期风险回报转差",
                            "evidence_1": "技术风险上升",
                            "evidence_2": "估值压力上升",
                            "risk_or_counterpoint": "长期主题仍在",
                            "action_logic": "降低仓位而不是清仓",
                        },
                        "source_hash": "sha256:" + "a" * 64,
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_dashboard_attaches_tradingagents_summary_without_reason_fields(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_csv(
        data_dir / "latest" / "portfolio.csv",
        PORTFOLIO_FIELDNAMES,
        [
            {
                "market": "US",
                "asset_class": "etf",
                "symbol": "DRAM",
                "name": "DRAM",
                "currency": "USD",
                "total_quantity": "10",
                "average_cost": "70",
                "last_price": "80",
                "market_value": "800",
                "cost_value": "700",
                "unrealized_pnl": "100",
                "portfolio_weight": "7.11%",
                "broker": "manual",
                "account_alias": "",
                "confidence": "actual",
                "notes": "",
            }
        ],
    )
    write_tradingagents_summary(data_dir / "latest" / "US" / "tradingagents_summary.json")

    state = load_dashboard_state(dashboard_config(tmp_path))
    summary = state.holdings[0]["tradingagents_summary"]

    assert summary == {
        "available": True,
        "ta_view": "低配",
        "current_action": "减仓",
        "core_reason": "内存超级周期仍在，但技术风险上升，所以 TA 建议降低仓位。",
        "ta_report_date": "2026-06-22",
        "latest_run_date": "2026-06-23",
    }
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_attaches_tradingagents_summary_without_reason_fields -q
```

Expected: fail with missing `tradingagents_summary`.

- [ ] **Step 3: Import summary helpers**

Modify `src/open_trader/dashboard.py` imports:

```python
from .tradingagents_summary import (
    index_tradingagents_summary_by_market_symbol,
    load_tradingagents_summary_cache,
    tradingagents_summary_latest_path,
)
```

- [ ] **Step 4: Load summary records per market**

Add helper near `_latest_decision_facts_for_markets`:

```python
def _latest_tradingagents_summary_for_markets(
    *,
    data_dir: Path,
    markets: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for market in markets:
        path = tradingagents_summary_latest_path(data_dir, market)
        if not path.exists():
            continue
        records_by_key.update(
            index_tradingagents_summary_by_market_symbol(
                load_tradingagents_summary_cache(path)
            )
        )
    return records_by_key
```

- [ ] **Step 5: Attach summary to holdings**

In `load_dashboard_state`, after decision facts loading:

```python
    tradingagents_summary_by_holding = _latest_tradingagents_summary_for_markets(
        data_dir=config.data_dir,
        markets=holding_markets,
    )
```

Pass `tradingagents_summary_by_holding` into `_merge_holding`.

Extend `_merge_holding` signature with:

```python
    tradingagents_summary_by_holding: dict[tuple[str, str], dict[str, Any]],
```

Add inside `_merge_holding` after `holding["agent_report"]`:

```python
    holding["tradingagents_summary"] = _tradingagents_summary_detail(
        tradingagents_summary_by_holding.get(key) if key is not None else None,
        agent_report,
        trade_action or premarket_action,
    )
```

Add helper:

```python
def _tradingagents_summary_detail(
    record: dict[str, Any] | None,
    agent_report: dict[str, str] | None,
    action: dict[str, str] | None,
) -> dict[str, Any]:
    if record is not None:
        return {
            "available": True,
            "ta_view": str(record.get("ta_view") or "缺失"),
            "current_action": str(record.get("current_action") or "缺失"),
            "core_reason": str(record.get("core_reason") or "缺失"),
            "ta_report_date": str(record.get("ta_report_date") or "缺失"),
            "latest_run_date": str(record.get("latest_run_date") or "缺失"),
        }
    return {
        "available": False,
        "ta_view": _normalize_dashboard_view(agent_report.get("advice_action", "") if agent_report else ""),
        "current_action": _normalize_dashboard_action(action.get("action", "") if action else ""),
        "core_reason": "缺失",
        "ta_report_date": (
            agent_report.get("fallback_from_date", "") or agent_report.get("run_date", "") or "缺失"
            if agent_report
            else "缺失"
        ),
        "latest_run_date": agent_report.get("run_date", "") or "缺失" if agent_report else "缺失",
    }
```

Add minimal local normalizers:

```python
def _normalize_dashboard_view(value: str) -> str:
    lowered = value.strip().lower()
    if any(word in lowered for word in ("underweight", "reduce", "trim")):
        return "低配"
    if any(word in lowered for word in ("overweight", "buy", "accumulate", "add")):
        return "超配"
    if "hold" in lowered:
        return "持有"
    if "sell" in lowered:
        return "卖出"
    return value.strip() or "缺失"


def _normalize_dashboard_action(value: str) -> str:
    labels = {"TRIM": "减仓", "BUY": "买入", "ADD": "加仓", "HOLD": "持有", "SELL": "卖出", "REVIEW": "人工复核"}
    return labels.get(value.strip().upper(), value.strip() or "缺失")
```

- [ ] **Step 6: Run dashboard test**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py::test_dashboard_attaches_tradingagents_summary_without_reason_fields -q
```

Expected: pass.

- [ ] **Step 7: Commit dashboard state changes**

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: attach TradingAgents summaries to dashboard"
```

---

### Task 5: Render Exactly Five Fields In The TradingAgents Card

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add failing frontend rendering test**

Append to `tests/test_dashboard_web.py`:

```python
def test_tradingagents_plugin_card_renders_exact_five_summary_fields() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard helper runtime checks")
    js_path = STATIC_DIR / "dashboard.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = { document: { addEventListener() {} } };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
vm.runInContext(`
const holding = {
  agent_report: { available: true, rating: "Underweight", run_date: "2026-06-23" },
  tradingagents_summary: {
    available: true,
    ta_view: "低配",
    current_action: "减仓",
    core_reason: "内存超级周期仍在，但技术风险上升，所以 TA 建议降低仓位。",
    ta_report_date: "2026-06-22",
    latest_run_date: "2026-06-23",
  },
};
const pluginsHtml = renderTradingDecisionPlugins(holding);
const start = pluginsHtml.indexOf("<h3>TradingAgents</h3>");
const end = pluginsHtml.indexOf("<h3>财报</h3>");
const card = pluginsHtml.slice(start, end);
for (const label of ["TA 观点", "当前动作", "核心理由", "TA 报告日期", "当前 latest"]) {
  if (!card.includes(label)) {
    throw new Error("missing TradingAgents field " + label + ": " + card);
  }
}
for (const forbidden of ["来源状态", "历史", "reason_fields", "查看英文原文", "条件：TradingAgents"]) {
  if (card.includes(forbidden)) {
    throw new Error("forbidden TradingAgents content rendered: " + forbidden + " in " + card);
  }
}
if (!card.includes("低配") || !card.includes("减仓") || !card.includes("2026-06-22") || !card.includes("2026-06-23")) {
  throw new Error("summary values missing: " + card);
}
`, sandbox);
"""
    subprocess.run([node, "-e", script, str(js_path)], check=True)
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_tradingagents_plugin_card_renders_exact_five_summary_fields -q
```

Expected: fail because the existing card renders headline/detail/condition instead of the five summary rows.

- [ ] **Step 3: Add fixed TradingAgents card renderer**

Modify `src/open_trader/dashboard_static/dashboard.js`. Add:

```javascript
function tradingAgentsSummaryPlugin(holding) {
  const summary = holding && holding.tradingagents_summary && typeof holding.tradingagents_summary === "object"
    ? holding.tradingagents_summary
    : {};
  const rows = [
    ["TA 观点", summary.ta_view],
    ["当前动作", summary.current_action],
    ["核心理由", summary.core_reason],
    ["TA 报告日期", summary.ta_report_date],
    ["当前 latest", summary.latest_run_date],
  ];
  return `
    <article class="decision-plugin-card">
      <div class="decision-plugin-card-header">
        <h3>TradingAgents</h3>
      </div>
      <div class="decision-fact-grid tradingagents-summary-grid">
        ${rows.map(([label, value]) => `
          <div class="decision-fact-row">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(formatPlain(value))}</strong>
          </div>
        `).join("")}
      </div>
    </article>
  `;
}
```

- [ ] **Step 4: Replace only the TradingAgents plugin object**

In `renderTradingDecisionPlugins`, replace the current `TradingAgents` object:

```javascript
    tradingAgentsSummaryPlugin(holding),
```

Keep all other plugin entries unchanged.

- [ ] **Step 5: Run frontend test**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_tradingagents_plugin_card_renders_exact_five_summary_fields -q
```

Expected: pass.

- [ ] **Step 6: Commit frontend card change**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: render fixed TradingAgents card fields"
```

---

### Task 6: Generate Summary In Daily Premarket Pipeline

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `tests/test_daily_premarket.py`

- [ ] **Step 1: Add failing daily report artifact test**

Append this narrow artifact-ordering test to `tests/test_daily_premarket.py`:

```python
def test_daily_premarket_includes_tradingagents_summary_artifact(tmp_path: Path) -> None:
    summary_path = tmp_path / "data" / "runs" / "2026-06-23" / "US" / "tradingagents_summary.json"
    latest_summary_path = tmp_path / "data" / "latest" / "US" / "tradingagents_summary.json"
    payload = {
        "run_date": "2026-06-23",
        "market": "US",
        "started_at": "2026-06-23T18:30:00+08:00",
        "finished_at": "2026-06-23T18:35:00+08:00",
        "deadline_at": "2026-06-23T21:10:00+08:00",
        "status": "ok",
        "readiness": "ready",
        "status_reasons": [],
        "premarket": {},
        "trading_plan": {},
        "futu_plan_check": {},
        "trade_actions": {},
        "artifacts": {
            "tradingagents_summary": str(summary_path),
            "latest_tradingagents_summary": str(latest_summary_path),
        },
    }
    report = daily_premarket._render_daily_report(payload)
    assert "tradingagents_summary" in report
    assert str(summary_path) in report
    assert "latest_tradingagents_summary" in report
    assert str(latest_summary_path) in report
```

Use the existing module import style in `tests/test_daily_premarket.py`; if it imports names directly rather than `daily_premarket`, import `_render_daily_report`.

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_daily_premarket_includes_tradingagents_summary_artifact -q
```

Expected: fail because `_render_daily_report` does not list the new artifact keys.

- [ ] **Step 3: Import summary generator**

Modify `src/open_trader/daily_premarket.py` imports:

```python
from .tradingagents_summary import LLMTradingAgentsSummaryExtractor, generate_tradingagents_summary
```

- [ ] **Step 4: Generate summary after trade actions**

Inside the market run path after `trade_actions_result` is produced, add:

```python
        tradingagents_summary_result = generate_tradingagents_summary(
            advice_path=advice_path,
            plan_path=plan_result.plan_path,
            actions_path=trade_actions_result.actions_path,
            data_dir=config.data_dir,
            run_date=run_date,
            market=market,
            extractor=LLMTradingAgentsSummaryExtractor(),
            update_latest=False,
        )
        tradingagents_summary_path = tradingagents_summary_result.run_path
```

- [ ] **Step 5: Add artifact paths**

Add latest path:

```python
        latest_tradingagents_summary_path = latest_dir / "tradingagents_summary.json"
```

Add to `artifacts`:

```python
            "tradingagents_summary": str(tradingagents_summary_path),
            "latest_tradingagents_summary": str(latest_tradingagents_summary_path),
```

- [ ] **Step 6: Promote latest summary with existing promotion set**

Extend `_promote_latest_set` signature:

```python
    tradingagents_summary_path: Path | None = None,
```

Inside it, stage a promotion:

```python
    if tradingagents_summary_path is not None:
        promotions.append(
            _LatestPromotion(
                source_path=tradingagents_summary_path,
                latest_path=latest_dir / "tradingagents_summary.json",
            )
        )
```

Pass from caller:

```python
                tradingagents_summary_path=tradingagents_summary_path,
```

- [ ] **Step 7: Include artifact in daily report ordering**

In `_render_daily_report`, add these keys near `decision_facts`:

```python
        "tradingagents_summary",
        "latest_tradingagents_summary",
```

- [ ] **Step 8: Run daily pipeline test**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py::test_daily_premarket_includes_tradingagents_summary_artifact -q
```

Expected: pass.

- [ ] **Step 9: Commit daily pipeline wiring**

```bash
git add src/open_trader/daily_premarket.py tests/test_daily_premarket.py
git commit -m "feat: include TradingAgents summaries in daily pipeline"
```

---

### Task 7: Focused Integration Verification

**Files:**
- No planned source edits unless tests reveal a defect.

- [ ] **Step 1: Run focused test set**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_tradingagents_summary.py \
  tests/test_premarket_cli.py::test_extract_tradingagents_summary_command_writes_paths \
  tests/test_dashboard.py::test_dashboard_attaches_tradingagents_summary_without_reason_fields \
  tests/test_dashboard_web.py::test_tradingagents_plugin_card_renders_exact_five_summary_fields \
  tests/test_daily_premarket.py::test_daily_premarket_includes_tradingagents_summary_artifact \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: full suite passes.

- [ ] **Step 3: Run a local dry-run extraction with fake-safe existing artifacts**

Use existing local latest files and do not update latest:

```bash
.venv/bin/python -m open_trader extract-tradingagents-summary \
  --advice data/latest/US/trading_advice.csv \
  --plan data/latest/US/trading_plan.csv \
  --actions data/latest/US/trade_actions.csv \
  --data-dir data \
  --date 2026-06-23 \
  --market US
```

Expected output includes:

```text
run_date: 2026-06-23
summaries:
summary_json: data/runs/2026-06-23/US/tradingagents_summary.json
latest: data/latest/US/tradingagents_summary.json
```

Because this command calls a real LLM unless the environment is missing credentials, it may fail with an auth or rate-limit error. If it fails for that reason, record the exact error and rely on fake-client tests for completion.

- [ ] **Step 4: Commit any verification fixes**

If Step 1 or Step 2 required fixes:

```bash
git add src/open_trader tests
git commit -m "fix: stabilize TradingAgents summary flow"
```

If no fixes were needed, do not create an empty commit.
