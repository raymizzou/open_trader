from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from openai import OpenAI

from .advice.change_classifier import DEEPSEEK_BASE_URL, DEFAULT_CLASSIFIER_MODEL
from open_trader.technical_facts import source_hash
from open_trader.market_scope import (
    MarketScope,
    market_run_dir,
    market_scoped_latest_path,
    parse_market_scope,
)


DECISION_FACTS_SCHEMA_VERSION = "open_trader.decision_facts.v1"
MISSING_VALUE = "缺失"
KLINE_FIELDS = ("trend", "position", "momentum", "key_levels", "risk")
NEWS_SENTIMENT_FIELDS = ("direction", "change", "catalyst", "risk", "attention")
RUN_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CHINESE_TEXT_PATTERN = re.compile(r"[\u3400-\u9fff]")
SOURCE_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
VALID_MODULE_STATUSES = {"ok", "missing_source", "extraction_failed", "error"}


@dataclass(frozen=True)
class DecisionSources:
    kline_source: str
    news_sentiment_source: str
    kline_hash: str
    news_sentiment_hash: str


@dataclass(frozen=True)
class AdviceSource:
    run_date: str
    market: str
    symbol: str
    source_status: str
    kline_source: str
    news_sentiment_source: str
    kline_hash: str
    news_sentiment_hash: str


@dataclass(frozen=True)
class DecisionFactsResult:
    run_date: str
    records: int
    extracted: int
    failed: int
    reused: int
    run_path: Path
    latest_path: Path


class DecisionFactsExtractor(Protocol):
    def extract(
        self,
        *,
        market: str,
        symbol: str,
        run_date: str,
        kline_source: str,
        news_sentiment_source: str,
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


class LLMDecisionFactsExtractor:
    def __init__(self, *, client: object | None = None) -> None:
        self.client = client or OpenAITextClient()

    def extract(
        self,
        *,
        market: str,
        symbol: str,
        run_date: str,
        kline_source: str,
        news_sentiment_source: str,
    ) -> dict[str, object]:
        messages = [
            {
                "role": "system",
                "content": _decision_facts_system_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "market": market,
                        "symbol": symbol,
                        "run_date": run_date,
                        "source_status": "",
                        "kline_source_hash": source_hash(kline_source)
                        if kline_source
                        else "",
                        "news_sentiment_source_hash": source_hash(
                            news_sentiment_source
                        )
                        if news_sentiment_source
                        else "",
                        "kline_source": kline_source,
                        "news_sentiment_source": news_sentiment_source,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        content = self.client.create(messages=messages, temperature=0)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM decision facts response must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("LLM decision facts response must be a JSON object")
        validate_decision_facts_record(payload)
        return payload


def extract_decision_sources(raw_decision: str) -> DecisionSources:
    try:
        payload = json.loads(raw_decision or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    state = payload.get("state")
    if not isinstance(state, dict):
        state = {}

    kline_source = _state_string(state, "market_report")
    sentiment_report = _state_string(state, "sentiment_report")
    news_report = _state_string(state, "news_report")
    news_sentiment_source = _combine_news_sentiment_sources(
        sentiment_report=sentiment_report,
        news_report=news_report,
    )

    return DecisionSources(
        kline_source=kline_source,
        news_sentiment_source=news_sentiment_source,
        kline_hash=source_hash(kline_source) if kline_source else "",
        news_sentiment_hash=source_hash(news_sentiment_source) if news_sentiment_source else "",
    )


def build_missing_fields(fields: tuple[str, ...]) -> dict[str, str]:
    return {field: MISSING_VALUE for field in fields}


def load_advice_sources(advice_path: Path) -> list[AdviceSource]:
    if not advice_path.exists():
        raise FileNotFoundError(f"advice CSV not found: {advice_path}")
    csv.field_size_limit(sys.maxsize)
    sources: list[AdviceSource] = []
    with advice_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            market = (row.get("market") or "").strip().upper()
            symbol = (row.get("symbol") or "").strip().upper()
            run_date = (row.get("run_date") or "").strip()
            if not market or not symbol:
                continue
            decision_sources = extract_decision_sources(row.get("raw_decision") or "")
            sources.append(
                AdviceSource(
                    run_date=run_date,
                    market=market,
                    symbol=symbol,
                    source_status=(row.get("source_status") or row.get("status") or "").strip(),
                    kline_source=decision_sources.kline_source,
                    news_sentiment_source=decision_sources.news_sentiment_source,
                    kline_hash=decision_sources.kline_hash,
                    news_sentiment_hash=decision_sources.news_sentiment_hash,
                )
            )
    return sources


def decision_facts_run_path(
    data_dir: Path,
    run_date: str,
    market: MarketScope | str | None = None,
) -> Path:
    scope = _market_scope(market)
    if scope is not None:
        return market_run_dir(data_dir, run_date, scope) / "decision_facts.json"
    return data_dir / "runs" / run_date / "decision_facts.json"


def decision_facts_latest_path(
    data_dir: Path,
    market: MarketScope | str | None = None,
) -> Path:
    scope = _market_scope(market)
    if scope is not None:
        return market_scoped_latest_path(data_dir, scope, "decision_facts.json")
    return data_dir / "latest" / "decision_facts.json"


def load_decision_facts_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def index_decision_facts_by_market_symbol(
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


def validate_decision_facts_record(record: dict[str, object]) -> None:
    if not isinstance(record, dict):
        raise ValueError("decision facts record must be an object")
    if record.get("schema_version") != DECISION_FACTS_SCHEMA_VERSION:
        raise ValueError("decision facts schema_version is invalid")
    _validate_module(record.get("kline"), "kline", KLINE_FIELDS)
    _validate_module(
        record.get("news_sentiment"),
        "news_sentiment",
        NEWS_SENTIMENT_FIELDS,
    )


def generate_decision_facts(
    *,
    advice_path: Path,
    data_dir: Path,
    run_date: str | None,
    extractor: DecisionFactsExtractor,
    update_latest: bool,
    market: MarketScope | str | None = None,
) -> DecisionFactsResult:
    sources = load_advice_sources(advice_path)
    _validate_source_run_dates(sources)
    market_scope = _market_scope(market)
    market_sources = (
        [source for source in sources if source.market == market_scope.value]
        if market_scope is not None
        else sources
    )
    if run_date is not None and run_date.strip():
        effective_run_date = _validate_run_date(run_date.strip())
    else:
        effective_run_date = _latest_run_date(market_sources)
    filtered_sources = [
        source
        for source in market_sources
        if not source.run_date or source.run_date == effective_run_date
    ]
    if run_date is not None and not filtered_sources:
        raise ValueError(f"no advice rows match run_date {effective_run_date}")

    run_path = decision_facts_run_path(data_dir, effective_run_date, market_scope)
    latest_path = decision_facts_latest_path(data_dir, market_scope)

    records = [
        _build_record(
            source=source,
            run_date=effective_run_date,
            extractor=extractor,
        )
        for source in filtered_sources
    ]
    extracted = sum(1 for record in records if not str(record.get("error") or ""))
    failed = len(records) - extracted
    payload = {
        "schema_version": DECISION_FACTS_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "run_date": effective_run_date,
        "market": market_scope.value if market_scope is not None else "",
        "records": records,
    }
    _atomic_write_json(run_path, payload)
    if update_latest:
        _atomic_write_json(latest_path, payload)
    return DecisionFactsResult(
        run_date=effective_run_date,
        records=len(records),
        extracted=extracted,
        failed=failed,
        reused=0,
        run_path=run_path,
        latest_path=latest_path,
    )


def _state_string(state: dict[object, object], key: str) -> str:
    value = state.get(key)
    return value if isinstance(value, str) else ""


def _combine_news_sentiment_sources(*, sentiment_report: str, news_report: str) -> str:
    parts = []
    if sentiment_report:
        parts.append(f"## sentiment_report\n\n{sentiment_report}")
    if news_report:
        parts.append(f"## news_report\n\n{news_report}")
    return "\n\n".join(parts)


def _decision_facts_system_prompt() -> str:
    return (
        "你是 open_trader 的决策事实抽取器。只输出严格 JSON 对象，不输出 Markdown、解释或"
        "任何 JSON 外文本。除 schema_version、status、source_hash、run_date、market、"
        "symbol、source_status、error 等固定字段外，所有可读字段必须使用中文；禁止输出原始"
        "英文段落或英文分析文字。schema_version 必须是 "
        f"{DECISION_FACTS_SCHEMA_VERSION}。顶层字段必须包含 schema_version、run_date、"
        "market、symbol、source_status、kline、news_sentiment、error。kline 和 "
        "news_sentiment 均为对象，字段为 status、source_hash、fields。kline.fields 必须"
        "且只能包含 trend、position、momentum、key_levels、risk；news_sentiment.fields "
        "必须且只能包含 direction、change、catalyst、risk、attention。status 只能使用 "
        "ok、missing_source、extraction_failed 或 error。缺失来源使用 status=missing_source；"
        "无法可靠抽取使用 status=extraction_failed；字段缺失写 缺失。严禁编造事实，严禁加入"
        "输入材料没有明确支持的信息。严禁输出交易建议、下单建议、仓位或头寸规模建议、目标价、"
        "自动执行建议，严禁使用买入、卖出、加仓、减仓、自动执行等指令性表达。"
    )


def _build_record(
    *,
    source: AdviceSource,
    run_date: str,
    extractor: DecisionFactsExtractor,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": DECISION_FACTS_SCHEMA_VERSION,
        "run_date": run_date,
        "market": source.market,
        "symbol": source.symbol,
        "source_status": source.source_status,
    }
    kline_missing = not source.kline_source.strip()
    news_sentiment_missing = not source.news_sentiment_source.strip()
    if kline_missing and news_sentiment_missing:
        record = {
            **base,
            "kline": _module_missing_source(KLINE_FIELDS, source.kline_hash),
            "news_sentiment": _module_missing_source(
                NEWS_SENTIMENT_FIELDS,
                source.news_sentiment_hash,
            ),
            "error": "",
        }
        validate_decision_facts_record(record)
        return record

    try:
        extracted = extractor.extract(
            market=source.market,
            symbol=source.symbol,
            run_date=run_date,
            kline_source=source.kline_source,
            news_sentiment_source=source.news_sentiment_source,
        )
        if not isinstance(extracted, dict):
            raise ValueError("decision facts extractor response must be an object")
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        record = {
            **base,
            "kline": (
                _module_missing_source(KLINE_FIELDS, source.kline_hash)
                if kline_missing
                else _module_error(KLINE_FIELDS, source.kline_hash)
            ),
            "news_sentiment": (
                _module_missing_source(NEWS_SENTIMENT_FIELDS, source.news_sentiment_hash)
                if news_sentiment_missing
                else _module_error(
                    NEWS_SENTIMENT_FIELDS,
                    source.news_sentiment_hash,
                )
            ),
            "error": error,
        }
        validate_decision_facts_record(record)
        return record

    kline_module, kline_error = _build_extracted_module(
        extracted.get("kline"),
        fields=KLINE_FIELDS,
        source_hash_value=source.kline_hash,
        module_name="kline",
        missing_source=kline_missing,
    )
    news_sentiment_module, news_sentiment_error = _build_extracted_module(
        extracted.get("news_sentiment"),
        fields=NEWS_SENTIMENT_FIELDS,
        source_hash_value=source.news_sentiment_hash,
        module_name="news_sentiment",
        missing_source=news_sentiment_missing,
    )
    errors = [
        error
        for error in (kline_error, news_sentiment_error)
        if error
    ]
    record = {
        **base,
        "kline": kline_module,
        "news_sentiment": news_sentiment_module,
        "error": "; ".join(errors),
    }
    validate_decision_facts_record(record)
    return record


def _build_extracted_module(
    module: object,
    *,
    fields: tuple[str, ...],
    source_hash_value: str,
    module_name: str,
    missing_source: bool,
) -> tuple[dict[str, Any], str]:
    if missing_source:
        return _module_missing_source(fields, source_hash_value), ""
    try:
        normalized = _normalize_extracted_module(
            module,
            source_hash_value=source_hash_value,
            fields=fields,
            module_name=module_name,
        )
        _validate_module(normalized, module_name, fields)
        status = str(normalized.get("status") or "").strip()
        if status != "ok":
            return normalized, f"{module_name}: status {status}"
    except Exception as exc:
        return _module_error(fields, source_hash_value), (
            f"{module_name}: {str(exc) or exc.__class__.__name__}"
        )
    return normalized, ""


def _normalize_extracted_module(
    module: object,
    *,
    source_hash_value: str,
    fields: tuple[str, ...],
    module_name: str,
) -> dict[str, Any]:
    if not isinstance(module, dict):
        raise ValueError(f"{module_name} module is missing")
    normalized = dict(module)
    normalized["source_hash"] = source_hash_value
    if "status" not in normalized:
        normalized["status"] = "ok"
    raw_fields = normalized.get("fields")
    if not isinstance(raw_fields, dict) or set(raw_fields) != set(fields):
        raise ValueError(f"{module_name} fields are invalid")
    return normalized


def _module_missing_source(fields: tuple[str, ...], source_hash_value: str) -> dict[str, Any]:
    return {
        "status": "missing_source",
        "source_hash": source_hash_value,
        "fields": build_missing_fields(fields),
    }


def _module_error(fields: tuple[str, ...], source_hash_value: str) -> dict[str, Any]:
    return {
        "status": "error",
        "source_hash": source_hash_value,
        "fields": build_missing_fields(fields),
    }


def _validate_module(
    module: object,
    module_name: str,
    expected_fields: tuple[str, ...],
) -> None:
    if not isinstance(module, dict):
        raise ValueError(f"{module_name} module is invalid")
    status = module.get("status")
    status_text = status.strip() if isinstance(status, str) else ""
    if status_text not in VALID_MODULE_STATUSES:
        raise ValueError(f"{module_name} status is invalid")
    source_hash_value = module.get("source_hash")
    if status_text == "missing_source":
        if not isinstance(source_hash_value, str) or (
            source_hash_value and not SOURCE_HASH_PATTERN.fullmatch(source_hash_value)
        ):
            raise ValueError(f"{module_name} source_hash is invalid")
    elif not isinstance(source_hash_value, str) or not SOURCE_HASH_PATTERN.fullmatch(
        source_hash_value
    ):
        raise ValueError(f"{module_name} source_hash is invalid")
    fields = module.get("fields")
    if not isinstance(fields, dict) or set(fields) != set(expected_fields):
        raise ValueError(f"{module_name} fields are invalid")
    for value in fields.values():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{module_name} field values are invalid")
        if value != MISSING_VALUE and not CHINESE_TEXT_PATTERN.search(value):
            raise ValueError("field values must be Chinese or 缺失")


def _latest_run_date(sources: list[AdviceSource]) -> str:
    dates = sorted({source.run_date for source in sources if source.run_date})
    if not dates:
        raise ValueError("run_date must be YYYY-MM-DD")
    return dates[-1]


def _validate_source_run_dates(sources: list[AdviceSource]) -> None:
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
    try:
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _now_text() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
