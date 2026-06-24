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
from .market_scope import (
    MarketScope,
    market_run_dir,
    market_scoped_latest_path,
    parse_market_scope,
)
from .technical_facts import source_hash


TRADINGAGENTS_SUMMARY_SCHEMA_VERSION = "open_trader.tradingagents_summary.v1"
MISSING_VALUE = "缺失"
REASON_FIELDS = (
    "main_judgment",
    "evidence_1",
    "evidence_2",
    "risk_or_counterpoint",
    "action_logic",
)
REASON_FIELD_NAMES = REASON_FIELDS
DISPLAY_FIELDS = (
    "ta_view",
    "current_action",
    "core_reason",
    "ta_report_date",
    "latest_run_date",
)
RECORD_FIELD_NAMES = {
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
}
ADVICE_REQUIRED_COLUMNS = (
    "run_date",
    "symbol",
    "market",
    "advice_action",
    "advice_summary",
    "raw_decision",
)
PLAN_REQUIRED_COLUMNS = (
    "run_date",
    "symbol",
    "market",
    "rating",
    "agent_reason",
    "agent_excerpt",
)
ACTION_REQUIRED_COLUMNS = (
    "run_date",
    "symbol",
    "market",
    "action",
    "reason",
    "agent_reason",
)
RUN_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CHINESE_TEXT_PATTERN = re.compile(r"[\u3400-\u9fff]")
SOURCE_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
PRICE_TRIGGER_ONLY_PATTERN = re.compile(
    r"(?:(?:当前价格|价格|现价).{0,16})?(?:达到|高于|低于|跌破|突破).{0,16}"
    r"(?:目标价|第一目标价|第二目标价|target|止损|stop)",
    re.IGNORECASE,
)
ENGLISH_PRICE_TRIGGER_ONLY_PATTERN = re.compile(
    r"^\s*(?:current\s+)?price\s+is\s+at\s+or\s+(?:above|below).{0,40}"
    r"(?:target|stop\s+loss)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AdviceSummarySource:
    run_date: str
    market: str
    symbol: str
    advice_action: str
    advice_summary: str
    raw_decision: str
    fallback_from_date: str


@dataclass(frozen=True)
class PlanSummarySource:
    run_date: str
    market: str
    symbol: str
    fallback_from_date: str
    rating: str
    plan_text: str
    agent_reason: str
    agent_excerpt: str


@dataclass(frozen=True)
class ActionSummarySource:
    run_date: str
    market: str
    symbol: str
    action: str
    current_action: str
    agent_reason: str
    agent_excerpt: str
    trigger_reason: str
    reason: str


SummarySource = AdviceSummarySource


@dataclass(frozen=True)
class TradingAgentsSummaryResult:
    run_date: str
    records: int
    extracted: int
    failed: int
    reused: int
    run_path: Path
    latest_path: Path


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
            {
                "role": "system",
                "content": _summary_system_prompt(),
            },
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


def build_missing_reason_fields() -> dict[str, str]:
    return {field: MISSING_VALUE for field in REASON_FIELDS}


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
        with path.open(encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def index_tradingagents_summary_by_market_symbol(
    cache: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    records = cache.get("records")
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


def validate_tradingagents_summary_record(record: dict[str, object]) -> None:
    if not isinstance(record, dict):
        raise ValueError("TradingAgents summary record must be an object")
    unexpected_fields = set(record) - RECORD_FIELD_NAMES
    missing_fields = RECORD_FIELD_NAMES - set(record)
    if unexpected_fields:
        raise ValueError(
            "TradingAgents summary record has unexpected field(s): "
            + ", ".join(sorted(unexpected_fields))
        )
    if missing_fields:
        raise ValueError(
            "TradingAgents summary record is missing field(s): "
            + ", ".join(sorted(missing_fields))
        )
    if record.get("schema_version") != TRADINGAGENTS_SUMMARY_SCHEMA_VERSION:
        raise ValueError("TradingAgents summary schema_version is invalid")
    for field in ("market", "symbol", "error", *DISPLAY_FIELDS):
        value = record.get(field)
        if not isinstance(value, str) or (field in DISPLAY_FIELDS and not value.strip()):
            raise ValueError(f"TradingAgents summary {field} is missing")
    for field in ("latest_run_date", "ta_report_date"):
        value = str(record[field])
        if value != MISSING_VALUE and not _is_valid_run_date(value):
            raise ValueError(f"TradingAgents summary {field} is invalid")
    for field in ("ta_view", "current_action", "core_reason"):
        _validate_chinese_or_missing(str(record[field]), field)
    if _is_price_trigger_only(str(record["core_reason"])):
        raise ValueError("TradingAgents summary core_reason cannot be price trigger only")
    reason_fields = record.get("reason_fields")
    if not isinstance(reason_fields, dict) or set(reason_fields) != set(REASON_FIELDS):
        raise ValueError("TradingAgents summary reason_fields are invalid")
    for field in REASON_FIELDS:
        value = reason_fields.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("TradingAgents summary reason_fields values are invalid")
        _validate_chinese_or_missing(value, field)
    source_hash_value = record.get("source_hash")
    if not isinstance(source_hash_value, str) or not SOURCE_HASH_PATTERN.fullmatch(
        source_hash_value
    ):
        raise ValueError("TradingAgents summary source_hash is invalid")


def generate_tradingagents_summary(
    *,
    advice_path: Path,
    plan_path: Path,
    actions_path: Path,
    data_dir: Path,
    run_date: str | None,
    market: MarketScope | str | None,
    extractor: TradingAgentsSummaryExtractor | None = None,
    update_latest: bool,
) -> TradingAgentsSummaryResult:
    sources = load_advice_summary_sources(advice_path)
    _validate_source_run_dates(sources)
    market_scope = _market_scope(market)
    market_sources = (
        [source for source in sources if source.market == market_scope.value]
        if market_scope is not None
        else sources
    )
    if run_date is not None and run_date.strip():
        latest_run_date = _validate_run_date(run_date.strip())
    else:
        latest_run_date = _latest_run_date(market_sources)
    filtered_sources = [
        source
        for source in market_sources
        if not source.run_date or source.run_date == latest_run_date
    ]
    if run_date is not None and not filtered_sources:
        raise ValueError(f"no advice rows match run_date {latest_run_date}")

    plans = _index_plan_sources(
        load_plan_summary_sources(plan_path),
        market_scope,
        latest_run_date,
    )
    actions = _index_action_sources(
        load_action_summary_sources(actions_path),
        market_scope,
        latest_run_date,
    )
    summary_extractor = extractor or LLMTradingAgentsSummaryExtractor()

    records = [
        _build_record(
            source=source,
            latest_run_date=latest_run_date,
            plan=plans.get((source.market, source.symbol), {}),
            action=actions.get((source.market, source.symbol), {}),
            extractor=summary_extractor,
        )
        for source in filtered_sources
    ]
    extracted = sum(1 for record in records if not str(record.get("error") or ""))
    failed = len(records) - extracted
    payload = {
        "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "latest_run_date": latest_run_date,
        "market": market_scope.value if market_scope is not None else "",
        "records": records,
    }
    run_path = tradingagents_summary_run_path(data_dir, latest_run_date, market_scope)
    latest_path = tradingagents_summary_latest_path(data_dir, market_scope)
    _atomic_write_json(run_path, payload)
    if update_latest:
        _atomic_write_json(latest_path, payload)
    return TradingAgentsSummaryResult(
        run_date=latest_run_date,
        records=len(records),
        extracted=extracted,
        failed=failed,
        reused=0,
        run_path=run_path,
        latest_path=latest_path,
    )


def _summary_system_prompt() -> str:
    return (
        "你是 open_trader 的 TradingAgents 卡片摘要抽取器。只输出严格 JSON 对象，不输出 "
        "Markdown、解释或 JSON 外文本。schema_version 必须是 "
        f"{TRADINGAGENTS_SUMMARY_SCHEMA_VERSION}。顶层字段只能包含 schema_version、"
        "core_reason、reason_fields。core_reason 必须是一句中文，约 80 到 120 个汉字，"
        "说明 TradingAgents 为什么形成该观点，而不是说明当前价格触发了目标价或止损。"
        "reason_fields 必须且只能包含 main_judgment、evidence_1、evidence_2、"
        "risk_or_counterpoint、action_logic。所有可读字段必须使用中文；缺失写 缺失；"
        "不要输出原始英文报告段落。严禁输出可执行下单指令、券商操作、详细仓位或数量。"
    )


def load_advice_summary_sources(advice_path: Path) -> list[AdviceSummarySource]:
    rows = _load_csv_rows(
        advice_path,
        required_columns=ADVICE_REQUIRED_COLUMNS,
        source_name="advice CSV",
    )
    sources: list[AdviceSummarySource] = []
    for row in rows:
        market = (row.get("market") or "").strip().upper()
        symbol = (row.get("symbol") or "").strip().upper()
        run_date = (row.get("run_date") or "").strip()
        if not market or not symbol:
            continue
        sources.append(
            AdviceSummarySource(
                run_date=run_date,
                market=market,
                symbol=symbol,
                advice_action=(row.get("advice_action") or "").strip(),
                advice_summary=(row.get("advice_summary") or "").strip(),
                raw_decision=(row.get("raw_decision") or "").strip(),
                fallback_from_date=(row.get("fallback_from_date") or "").strip(),
            )
        )
    return sources


def load_plan_summary_sources(plan_path: Path) -> list[PlanSummarySource]:
    sources: list[PlanSummarySource] = []
    for row in _load_csv_rows(
        plan_path,
        required_columns=PLAN_REQUIRED_COLUMNS,
        source_name="plan CSV",
    ):
        market = (row.get("market") or "").strip().upper()
        symbol = (row.get("symbol") or "").strip().upper()
        if not market or not symbol:
            continue
        sources.append(
            PlanSummarySource(
                run_date=(row.get("run_date") or "").strip(),
                market=market,
                symbol=symbol,
                fallback_from_date=(row.get("fallback_from_date") or "").strip(),
                rating=(row.get("rating") or "").strip(),
                plan_text=(row.get("plan_text") or "").strip(),
                agent_reason=(row.get("agent_reason") or "").strip(),
                agent_excerpt=(row.get("agent_excerpt") or "").strip(),
            )
        )
    return sources


def load_action_summary_sources(actions_path: Path) -> list[ActionSummarySource]:
    sources: list[ActionSummarySource] = []
    for row in _load_csv_rows(
        actions_path,
        required_columns=ACTION_REQUIRED_COLUMNS,
        source_name="action CSV",
    ):
        market = (row.get("market") or "").strip().upper()
        symbol = (row.get("symbol") or "").strip().upper()
        action = (row.get("action") or "").strip()
        if not market or not symbol:
            continue
        sources.append(
            ActionSummarySource(
                run_date=(row.get("run_date") or "").strip(),
                market=market,
                symbol=symbol,
                action=action,
                current_action=normalize_current_action(action),
                agent_reason=(row.get("agent_reason") or "").strip(),
                agent_excerpt=(row.get("agent_excerpt") or "").strip(),
                trigger_reason=(row.get("trigger_reason") or "").strip(),
                reason=(row.get("reason") or "").strip(),
            )
        )
    return sources


def _load_csv_rows(
    path: Path,
    *,
    required_columns: tuple[str, ...],
    source_name: str,
) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        if fieldnames is None:
            raise ValueError(f"{source_name} missing header")
        normalized_fieldnames = [field.strip() if field is not None else "" for field in fieldnames]
        if any(not field for field in normalized_fieldnames):
            raise ValueError(f"{source_name} has blank header")
        duplicates = _duplicate_values(normalized_fieldnames)
        if duplicates:
            raise ValueError(
                f"{source_name} has duplicate header(s): {', '.join(duplicates)}"
            )
        missing = [field for field in required_columns if field not in normalized_fieldnames]
        if missing:
            raise ValueError(
                f"{source_name} missing required column(s): {', '.join(missing)}"
            )
        rows: list[dict[str, str]] = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"{source_name} row {row_number} has extra cell(s)")
            rows.append({str(key).strip(): value for key, value in row.items()})
        return rows


def _duplicate_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def _index_plan_sources(
    sources: list[PlanSummarySource],
    market_scope: MarketScope | None,
    run_date: str,
) -> dict[tuple[str, str], dict[str, str]]:
    indexed: dict[tuple[str, str], dict[str, str]] = {}
    for source in sources:
        if market_scope is not None and source.market != market_scope.value:
            continue
        if source.run_date and source.run_date != run_date:
            continue
        indexed[(source.market, source.symbol)] = {
            "run_date": source.run_date,
            "market": source.market,
            "symbol": source.symbol,
            "fallback_from_date": source.fallback_from_date,
            "rating": source.rating,
            "plan_text": source.plan_text,
            "agent_reason": source.agent_reason,
            "agent_excerpt": source.agent_excerpt,
        }
    return indexed


def _index_action_sources(
    sources: list[ActionSummarySource],
    market_scope: MarketScope | None,
    run_date: str,
) -> dict[tuple[str, str], dict[str, str]]:
    indexed: dict[tuple[str, str], dict[str, str]] = {}
    for source in sources:
        if market_scope is not None and source.market != market_scope.value:
            continue
        if source.run_date and source.run_date != run_date:
            continue
        indexed[(source.market, source.symbol)] = {
            "run_date": source.run_date,
            "market": source.market,
            "symbol": source.symbol,
            "action": source.action,
            "agent_reason": source.agent_reason,
            "agent_excerpt": source.agent_excerpt,
            "trigger_reason": source.trigger_reason,
            "reason": source.reason,
        }
    return indexed


def _build_record(
    *,
    source: SummarySource,
    latest_run_date: str,
    plan: dict[str, str],
    action: dict[str, str],
    extractor: TradingAgentsSummaryExtractor,
) -> dict[str, Any]:
    ta_report_date = _resolve_ta_report_date(source, plan)
    ta_view = normalize_ta_view(source.advice_action or plan.get("rating", ""))
    current_action = normalize_current_action(action.get("action", ""))
    final_trade_decision = extract_final_trade_decision(source.raw_decision)
    base_source = json.dumps(
        {
            "advice_summary": source.advice_summary,
            "final_trade_decision": final_trade_decision,
            "advice_action": source.advice_action,
            "current_action": current_action,
            "ta_report_date": ta_report_date,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    base: dict[str, Any] = {
        "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
        "market": source.market,
        "symbol": source.symbol,
        "latest_run_date": latest_run_date,
        "ta_report_date": ta_report_date,
        "ta_view": ta_view,
        "current_action": current_action,
        "source_hash": source_hash(base_source),
    }
    try:
        extracted = extractor.extract(
            market=source.market,
            symbol=source.symbol,
            latest_run_date=latest_run_date,
            ta_report_date=ta_report_date,
            advice_action=source.advice_action,
            current_action=current_action,
            advice_summary=source.advice_summary,
            final_trade_decision=final_trade_decision,
        )
        if not isinstance(extracted, dict):
            raise ValueError("TradingAgents summary extractor response must be an object")
        record = {
            **base,
            "core_reason": _string_or_missing(extracted.get("core_reason")),
            "reason_fields": _coerce_reason_fields(extracted.get("reason_fields")),
            "error": "",
        }
        validate_tradingagents_summary_record(record)
        return record
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        record = {
            **base,
            "core_reason": _fallback_core_reason(plan, action),
            "reason_fields": build_missing_reason_fields(),
            "error": error,
        }
        validate_tradingagents_summary_record(record)
        return record


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


def _validate_llm_payload(payload: dict[str, object]) -> dict[str, object]:
    if payload.get("schema_version") != TRADINGAGENTS_SUMMARY_SCHEMA_VERSION:
        raise ValueError("TradingAgents summary schema_version is invalid")
    core_reason = _string_or_missing(payload.get("core_reason"))
    reason_fields = _coerce_reason_fields(payload.get("reason_fields"))
    if core_reason != MISSING_VALUE:
        _validate_chinese_or_missing(core_reason, "core_reason")
        if _is_price_trigger_only(core_reason):
            raise ValueError("TradingAgents summary core_reason cannot be price trigger only")
    return {
        "schema_version": TRADINGAGENTS_SUMMARY_SCHEMA_VERSION,
        "core_reason": core_reason,
        "reason_fields": reason_fields,
    }


def _coerce_reason_fields(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(REASON_FIELDS):
        raise ValueError("TradingAgents summary reason_fields are invalid")
    fields = {field: _string_or_missing(value.get(field)) for field in REASON_FIELDS}
    for field, field_value in fields.items():
        _validate_chinese_or_missing(field_value, field)
    return fields


def _resolve_ta_report_date(source: SummarySource, plan: dict[str, str]) -> str:
    for value in (source.fallback_from_date, plan.get("fallback_from_date", ""), source.run_date):
        text = (value or "").strip()
        if text:
            return text if _is_valid_run_date(text) else MISSING_VALUE
    return MISSING_VALUE


def normalize_ta_view(value: str) -> str:
    normalized = value.strip().lower().replace("_", " ").replace("-", " ")
    compact = normalized.replace(" ", "")
    mapping = {
        "underweight": "低配",
        "under weight": "低配",
        "reduce": "低配",
        "trim": "低配",
        "overweight": "超配",
        "over weight": "超配",
        "add": "超配",
        "buy": "买入",
        "hold": "持有",
        "neutral": "持有",
        "sell": "卖出",
    }
    if value.strip() in {"低配", "超配", "买入", "持有", "卖出"}:
        return value.strip()
    return mapping.get(normalized, mapping.get(compact, MISSING_VALUE))


def normalize_current_action(value: str) -> str:
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    mapping = {
        "BUY": "买入",
        "ADD": "加仓",
        "TRIM": "减仓",
        "REDUCE": "减仓",
        "SELL": "卖出",
        "SELL_STOP": "止损卖出",
        "TAKE_PROFIT": "止盈",
        "HOLD": "持有",
        "WATCH": "观察",
        "REVIEW": "人工复核",
    }
    if value.strip() and CHINESE_TEXT_PATTERN.search(value):
        return value.strip()
    return mapping.get(normalized, MISSING_VALUE)


def _fallback_core_reason(plan: dict[str, str], action: dict[str, str]) -> str:
    for value in (
        plan.get("agent_reason", ""),
        action.get("agent_reason", ""),
        plan.get("plan_text", ""),
        action.get("agent_excerpt", ""),
        plan.get("agent_excerpt", ""),
    ):
        text = (value or "").strip()
        if _is_usable_core_reason(text):
            return text
    return MISSING_VALUE


def _is_usable_core_reason(value: str) -> bool:
    return bool(
        value
        and value != MISSING_VALUE
        and CHINESE_TEXT_PATTERN.search(value)
        and not _is_price_trigger_only(value)
    )


def _string_or_missing(value: object) -> str:
    if not isinstance(value, str):
        return MISSING_VALUE
    text = value.strip()
    return text or MISSING_VALUE


def _validate_chinese_or_missing(value: str, field: str) -> None:
    if value == MISSING_VALUE:
        return
    if not CHINESE_TEXT_PATTERN.search(value):
        raise ValueError(f"TradingAgents summary {field} must be Chinese or 缺失")


def _is_price_trigger_only(value: str) -> bool:
    text = value.strip()
    if text == MISSING_VALUE:
        return False
    return bool(
        PRICE_TRIGGER_ONLY_PATTERN.fullmatch(text.rstrip("。.;；"))
        or ENGLISH_PRICE_TRIGGER_ONLY_PATTERN.fullmatch(text.rstrip("。.;；"))
    )


def _latest_run_date(sources: list[SummarySource]) -> str:
    dates = sorted({source.run_date for source in sources if source.run_date})
    if not dates:
        raise ValueError("run_date must be YYYY-MM-DD")
    return dates[-1]


def _validate_source_run_dates(sources: list[SummarySource]) -> None:
    for source in sources:
        if source.run_date and not _is_valid_run_date(source.run_date):
            raise ValueError("run_date must be YYYY-MM-DD")


def _validate_run_date(run_date: str) -> str:
    if not _is_valid_run_date(run_date):
        raise ValueError("run_date must be YYYY-MM-DD")
    return run_date


def _is_valid_run_date(run_date: str) -> bool:
    if not RUN_DATE_PATTERN.fullmatch(run_date):
        return False
    try:
        datetime.strptime(run_date, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _market_scope(market: MarketScope | str | None) -> MarketScope | None:
    if market is None:
        return None
    if isinstance(market, MarketScope):
        return market
    return parse_market_scope(market)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
            temp_file.write("\n")
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise


def _now_text() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
