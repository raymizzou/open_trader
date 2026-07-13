from __future__ import annotations

import csv
import hashlib
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
from open_trader.market_scope import (
    MarketScope,
    market_run_dir,
    market_scoped_latest_path,
    parse_market_scope,
)


TECHNICAL_FACTS_SCHEMA_VERSION = "open_trader.technical_facts_cache.v1"
FACTS_SCHEMA_VERSION = "open_trader.technical_facts.v1"
RUN_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UNKNOWN_TIMEFRAME_VALUES = {
    "unknown",
    "unknown timeframe",
    "timeframe unknown",
    "周期缺失",
}
BOLLINGER_POSITIONS = {
    "above_upper",
    "near_upper",
    "middle_range",
    "near_lower",
    "below_lower",
    "unknown",
}
BOLLINGER_STATUSES = {
    "upper_risk",
    "lower_opportunity",
    "neutral",
    "unknown",
}
BOLLINGER_REFERENCE_BANDS = {"", "upper", "lower"}
BOLLINGER_VISIBLE_TEXT_FIELDS = ("summary_zh", "detail_zh")
BOLLINGER_FALLBACK_VERSION = "bollinger_report_parser.v2"
BOLLINGER_TRADING_INSTRUCTION_PATTERN = re.compile(
    r"(?:建议买入|建议卖出|买入|卖出|加仓|减仓|下单|建仓|平仓|止盈|止损|仓位|执行)"
)
NUMBER_TEXT_PATTERN = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")


@dataclass(frozen=True)
class AdviceSource:
    run_date: str
    market: str
    symbol: str
    source_status: str
    market_report: str
    source_advice_hash: str


@dataclass(frozen=True)
class TechnicalFactsResult:
    run_date: str
    records: int
    extracted: int
    failed: int
    reused: int
    run_path: Path
    latest_path: Path


class TechnicalFactsExtractor(Protocol):
    def extract(
        self,
        *,
        market: str,
        symbol: str,
        run_date: str,
        market_report: str,
    ) -> dict[str, object]:
        ...


class OpenAITextClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEEPSEEK_BASE_URL,
        model: str = DEFAULT_CLASSIFIER_MODEL,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.client = OpenAI(
            api_key=api_key or os.environ.get("DEEPSEEK_API_KEY"),
            base_url=base_url,
            timeout=timeout_seconds,
        )

    def create(self, *, messages: list[dict[str, str]], temperature: float) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
            timeout=self.timeout_seconds,
        )
        content = response.choices[0].message.content
        return content or ""


class LLMTechnicalFactsExtractor:
    def __init__(self, *, client: object | None = None) -> None:
        self.client = client or OpenAITextClient()

    def extract(
        self,
        *,
        market: str,
        symbol: str,
        run_date: str,
        market_report: str,
    ) -> dict[str, object]:
        messages = [
            {
                "role": "system",
                "content": _technical_facts_system_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "market": market,
                        "symbol": symbol,
                        "run_date": run_date,
                        "market_report": _strip_transaction_proposal(market_report),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        content = self.client.create(messages=messages, temperature=0)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM technical facts response must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("LLM technical facts response must be a JSON object")
        _validate_facts(payload)
        return payload


def extract_market_report(raw_decision: str) -> str:
    try:
        payload = json.loads(raw_decision or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    state = payload.get("state")
    if not isinstance(state, dict):
        return ""
    report = state.get("market_report")
    return report if isinstance(report, str) else ""


def source_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


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
            market_report = extract_market_report(row.get("raw_decision") or "")
            sources.append(
                AdviceSource(
                    run_date=run_date,
                    market=market,
                    symbol=symbol,
                    source_status=(row.get("source_status") or row.get("status") or "").strip(),
                    market_report=market_report,
                    source_advice_hash=source_hash(market_report),
                )
            )
    return sources


def technical_facts_run_path(
    data_dir: Path,
    run_date: str,
    market: MarketScope | None = None,
) -> Path:
    if market is not None:
        return market_run_dir(data_dir, run_date, market) / "technical_facts.json"
    return data_dir / "runs" / run_date / "technical_facts.json"


def technical_facts_latest_path(
    data_dir: Path,
    market: MarketScope | None = None,
) -> Path:
    if market is not None:
        return market_scoped_latest_path(data_dir, market, "technical_facts.json")
    return data_dir / "latest" / "technical_facts.json"


def load_technical_facts_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_latest_technical_facts_by_market_symbol(
    data_dir: Path,
) -> dict[tuple[str, str], dict[str, Any]]:
    return index_technical_facts_by_market_symbol(
        load_technical_facts_cache(technical_facts_latest_path(data_dir))
    )


def index_technical_facts_by_market_symbol(
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


def technical_facts_has_missing_timeframe(facts: dict[str, object]) -> bool:
    return _has_unknown_timeframe(facts)


def build_freshness(
    *,
    market_data_as_of: str,
    run_date: str,
    has_unknown_timeframe: bool,
) -> dict[str, str]:
    if not market_data_as_of:
        return {
            "status": "missing_date",
            "message": f"行情日期缺失，报告生成于 {run_date}",
        }
    if has_unknown_timeframe:
        return {
            "status": "missing_timeframe",
            "message": "指标周期缺失，需复核",
        }
    return {
        "status": "fresh",
        "message": f"日线数据截至 {market_data_as_of}",
    }


def generate_technical_facts(
    *,
    advice_path: Path,
    data_dir: Path,
    run_date: str | None,
    extractor: TechnicalFactsExtractor,
    update_latest: bool,
    market: str | None = None,
) -> TechnicalFactsResult:
    sources = load_advice_sources(advice_path)
    _validate_source_run_dates(sources)
    market_scope = parse_market_scope(market) if market is not None else None
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

    run_path = technical_facts_run_path(data_dir, effective_run_date, market_scope)
    latest_path = technical_facts_latest_path(data_dir, market_scope)
    reusable_records = _records_by_identity(load_technical_facts_cache(latest_path))

    rows: list[dict[str, Any]] = []
    extracted = 0
    failed = 0
    reused = 0
    for source in filtered_sources:
        identity = (source.market, source.symbol, source.source_advice_hash)
        reusable = reusable_records.get(identity)
        if reusable is not None and _can_reuse_technical_facts_record(reusable):
            rows.append(
                _normalize_reused_record(
                    reusable,
                    source=source,
                    run_date=effective_run_date,
                )
            )
            reused += 1
            continue
        rows.append(
            _extract_record(
                source=source,
                run_date=effective_run_date,
                extractor=extractor,
            )
        )
        extraction_status = rows[-1].get("extraction_status")
        if extraction_status == "ok":
            extracted += 1
        elif extraction_status in {"missing_source", "extraction_failed"}:
            failed += 1

    payload = {
        "schema_version": TECHNICAL_FACTS_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "run_date": effective_run_date,
        "market": market_scope.value if market_scope is not None else "",
        "records": rows,
    }
    _atomic_write_json(run_path, payload)
    if update_latest:
        _atomic_write_json(latest_path, payload)
    return TechnicalFactsResult(
        run_date=effective_run_date,
        records=len(rows),
        extracted=extracted,
        failed=failed,
        reused=reused,
        run_path=run_path,
        latest_path=latest_path,
    )


def _extract_record(
    *,
    source: AdviceSource,
    run_date: str,
    extractor: TechnicalFactsExtractor,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_date": run_date,
        "market": source.market,
        "symbol": source.symbol,
        "source_status": source.source_status,
        "source_advice_hash": source.source_advice_hash,
    }
    market_report = _strip_transaction_proposal(source.market_report)
    if not market_report:
        facts = _missing_facts(source, run_date, "market_report_missing")
        return {
            **base,
            "extraction_status": "missing_source",
            "error": "market_report_missing",
            "facts": facts,
            "freshness": build_freshness(
                market_data_as_of="",
                run_date=run_date,
                has_unknown_timeframe=True,
            ),
        }
    try:
        facts = extractor.extract(
            market=source.market,
            symbol=source.symbol,
            run_date=run_date,
            market_report=market_report,
        )
        _validate_facts(facts)
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        fallback_facts = _fallback_bollinger_facts(source, run_date, market_report)
        if fallback_facts is not None:
            return {
                **base,
                "extraction_status": "ok",
                "error": "",
                "extractor_error": error,
                "extraction_fallback": "bollinger_report_parser",
                "extraction_fallback_version": BOLLINGER_FALLBACK_VERSION,
                "facts": fallback_facts,
                "freshness": build_freshness(
                    market_data_as_of=str(
                        fallback_facts.get("market_data_as_of") or ""
                    ).strip(),
                    run_date=run_date,
                    has_unknown_timeframe=False,
                ),
            }
        facts = _missing_facts(source, run_date, error)
        return {
            **base,
            "extraction_status": "extraction_failed",
            "error": error,
            "facts": facts,
            "freshness": build_freshness(
                market_data_as_of="",
                run_date=run_date,
                has_unknown_timeframe=True,
            ),
        }

    market_data_as_of = str(facts.get("market_data_as_of") or "").strip()
    return {
        **base,
        "extraction_status": "ok",
        "error": "",
        "facts": facts,
        "freshness": build_freshness(
            market_data_as_of=market_data_as_of,
            run_date=run_date,
            has_unknown_timeframe=_has_unknown_timeframe(facts),
        ),
    }


def _records_by_identity(
    cache: dict[str, Any],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    records = cache.get("records")
    if not isinstance(records, list):
        return {}
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        market = str(record.get("market") or "").strip().upper()
        symbol = str(record.get("symbol") or "").strip().upper()
        source_advice_hash = str(record.get("source_advice_hash") or "").strip()
        if market and symbol and source_advice_hash:
            indexed[(market, symbol, source_advice_hash)] = record
    return indexed


def _can_reuse_technical_facts_record(record: dict[str, Any]) -> bool:
    if record.get("extraction_status") != "ok":
        return False
    facts = record.get("facts")
    if not isinstance(facts, dict) or technical_facts_has_missing_timeframe(facts):
        return False
    if record.get("extraction_fallback") == "bollinger_report_parser":
        return record.get("extraction_fallback_version") == BOLLINGER_FALLBACK_VERSION
    return True


def _normalize_reused_record(
    record: dict[str, Any],
    *,
    source: AdviceSource,
    run_date: str,
) -> dict[str, Any]:
    normalized = dict(record)
    normalized.update(
        {
            "run_date": source.run_date or run_date,
            "market": source.market,
            "symbol": source.symbol,
            "source_status": source.source_status,
            "source_advice_hash": source.source_advice_hash,
            "reused_from_cache": True,
        }
    )
    return normalized


def _validate_facts(facts: dict[str, object]) -> None:
    if not isinstance(facts, dict):
        raise ValueError("technical facts must be an object")
    if facts.get("schema_version") != FACTS_SCHEMA_VERSION:
        raise ValueError("technical facts schema_version is invalid")
    if not isinstance(facts.get("status"), str) or not facts.get("status"):
        raise ValueError("technical facts status is missing")
    timeframes = facts.get("timeframes")
    if not isinstance(timeframes, list):
        raise ValueError("technical facts timeframes must be a list")
    for timeframe in timeframes:
        if isinstance(timeframe, dict):
            _validate_bollinger_payload(timeframe.get("bollinger"))


def _validate_bollinger_payload(payload: object) -> None:
    if payload is None or payload == "":
        return
    if not isinstance(payload, dict):
        raise ValueError("bollinger must be an object")
    position = str(payload.get("position") or "").strip()
    if position and position not in BOLLINGER_POSITIONS:
        raise ValueError("bollinger position is invalid")
    status = str(payload.get("status") or "").strip()
    if status and status not in BOLLINGER_STATUSES:
        raise ValueError("bollinger status is invalid")
    reference_band = str(payload.get("reference_band") or "").strip()
    if reference_band not in BOLLINGER_REFERENCE_BANDS:
        raise ValueError("bollinger reference_band is invalid")
    for field_name in BOLLINGER_VISIBLE_TEXT_FIELDS:
        value = payload.get(field_name)
        if value in {None, ""}:
            continue
        if not isinstance(value, str):
            raise ValueError(f"bollinger {field_name} must be a string")
        if BOLLINGER_TRADING_INSTRUCTION_PATTERN.search(value):
            raise ValueError(f"bollinger {field_name} contains trading instruction")


def _has_unknown_timeframe(facts: dict[str, object]) -> bool:
    timeframes = facts.get("timeframes")
    if not isinstance(timeframes, list) or not timeframes:
        return True
    for timeframe in timeframes:
        if not isinstance(timeframe, dict):
            return True
        value = str(timeframe.get("timeframe") or "").strip()
        if not value or value.casefold() in UNKNOWN_TIMEFRAME_VALUES:
            return True
    return False


def _strip_transaction_proposal(report: str) -> str:
    marker = "FINAL TRANSACTION PROPOSAL"
    index = report.upper().find(marker)
    if index == -1:
        return report
    return report[:index].rstrip()


def _technical_facts_system_prompt() -> str:
    return (
        "你是 open_trader 的技术面事实抽取器。只抽取客观技术面事实，输出严格 JSON。"
        "忽略 FINAL TRANSACTION PROPOSAL、BUY、SELL、HOLD、Underweight、仓位建议、"
        "交易建议和执行建议。每个 RSI、MACD、均线、布林带、ATR、成交量信号都必须带"
        "timeframe。若报告没有明确周期，timeframe 使用 unknown，timeframe_label 使用"
        "\"周期缺失\"。缺失字段使用空字符串或空数组，不要猜测。schema_version 必须是 "
        f"{FACTS_SCHEMA_VERSION}。status 必须是 present。顶层 timeframes 必须是 JSON 数组。"
        "按日期排列的 OHLC 行或日数指标属于 daily，所有相关 timeframe 使用 daily。"
        "布林带必须放在每个 timeframe 的 bollinger 对象中，字段包含 upper、middle、"
        "lower、position、status、reference_band、reference_value、distance_pct、"
        "summary_zh、detail_zh。position 只能使用 above_upper、near_upper、"
        "middle_range、near_lower、below_lower、unknown；status 只能使用 upper_risk、"
        "lower_opportunity、neutral、unknown；reference_band 只能使用 upper、lower "
        "或空字符串。summary_zh 和 detail_zh 必须是中文事实提示，不得包含买入、卖出、"
        "加仓、减仓、下单、仓位等交易指令。"
    )


def _missing_facts(source: AdviceSource, run_date: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": FACTS_SCHEMA_VERSION,
        "status": "missing",
        "source_date": run_date,
        "market_data_as_of": "",
        "symbol": f"{source.market}.{source.symbol}",
        "timeframes": [],
        "reason": reason,
    }


def _fallback_bollinger_facts(
    source: AdviceSource,
    run_date: str,
    market_report: str,
) -> dict[str, Any] | None:
    bollinger = _parse_bollinger_from_report(market_report)
    if bollinger is None:
        return None
    market_data_as_of = bollinger.pop("market_data_as_of", "") or run_date
    facts: dict[str, Any] = {
        "schema_version": FACTS_SCHEMA_VERSION,
        "status": "present",
        "source_date": run_date,
        "market_data_as_of": market_data_as_of,
        "symbol": f"{source.market}.{source.symbol}",
        "timeframes": [
            {
                "timeframe": "daily",
                "timeframe_label": "日线",
                "current_price": bollinger.get("current_price", ""),
                "trend_summary": "",
                "moving_averages": {},
                "macd": {},
                "rsi": {},
                "bollinger": bollinger,
                "atr": {},
                "volume": {},
                "support_resistance": {
                    "support_levels": [],
                    "resistance_levels": [],
                },
                "price_action": {"timeline": []},
                "risks": [],
                "evidence_quotes": [],
            }
        ],
    }
    _validate_facts(facts)
    return facts


def _parse_bollinger_from_report(report: str) -> dict[str, str] | None:
    if not report or ("Bollinger" not in report and "布林" not in report):
        return None
    context = _bollinger_context(report)
    values = _extract_bollinger_table_values(context)
    upper = values.get("upper") or _extract_labeled_number(
        context,
        (
            "Upper Band",
            "Upper (boll_ub)",
            "Bollinger Upper",
            "boll_ub",
            "上轨",
        ),
    )
    middle = values.get("middle") or _extract_labeled_number(
        context,
        (
            "Middle (20 SMA)",
            "Middle Band",
            "Bollinger Middle",
            "Middle (boll)",
            "boll)",
            "中轨",
        ),
    )
    lower = values.get("lower") or _extract_labeled_number(
        context,
        (
            "Lower Band",
            "Lower (boll_lb)",
            "Bollinger Lower",
            "boll_lb",
            "下轨",
        ),
    )
    current_price = values.get("current_price") or _extract_current_price(context)
    if current_price is None or (upper is None and lower is None):
        return None

    current_value = _to_float(current_price)
    upper_value = _to_float(upper)
    lower_value = _to_float(lower)
    if current_value is None:
        return None

    state = _classify_bollinger_position(
        current=current_value,
        upper=upper_value,
        lower=lower_value,
    )
    reference_value = upper if state["reference_band"] == "upper" else lower
    distance_pct = _format_bollinger_distance(
        current=current_value,
        reference=_to_float(reference_value),
        reference_band=state["reference_band"],
    )
    return {
        "upper": _format_number_text(upper),
        "middle": _format_number_text(middle),
        "lower": _format_number_text(lower),
        "current_price": _format_number_text(current_price),
        "position": state["position"],
        "status": state["status"],
        "reference_band": state["reference_band"],
        "reference_value": _format_number_text(reference_value),
        "distance_pct": distance_pct,
        "summary_zh": state["summary_zh"],
        "detail_zh": state["detail_zh"],
        "market_data_as_of": _extract_bollinger_date(context),
    }


def _bollinger_context(report: str) -> str:
    lines = report.splitlines()
    selected: set[int] = set()
    for index, line in enumerate(lines):
        if "Bollinger" not in line and "布林" not in line:
            continue
        start = max(0, index - 15)
        end = min(len(lines), index + 26)
        selected.update(range(start, end))
    return "\n".join(lines[index] for index in sorted(selected))


def _extract_bollinger_table_values(report: str) -> dict[str, str]:
    values: dict[str, str] = {}
    header_cells: list[str] = []
    for raw_line in report.splitlines():
        line = raw_line.strip()
        if "|" not in line:
            header_cells = []
            continue
        cells = [_plain_cell(cell) for cell in line.strip("|").split("|")]
        if any(_is_separator_cell(cell) for cell in cells):
            continue
        if _looks_like_bollinger_table_header(cells):
            header_cells = cells
            continue
        label_value = _map_bollinger_label_value_row(cells)
        if label_value:
            values.update(label_value)
            continue
        row_values = [_first_number(cell) for cell in cells]
        if not header_cells:
            continue
        if sum(1 for value in row_values if value is not None) < 2:
            continue
        mapped = _map_bollinger_table_row(header_cells, row_values)
        if mapped:
            values.update(mapped)
    return values


def _looks_like_bollinger_table_header(cells: list[str]) -> bool:
    band_columns = 0
    for cell in cells:
        cell_text = cell.casefold()
        if (
            "upper" in cell_text
            or "middle" in cell_text
            or "lower" in cell_text
            or "boll_ub" in cell_text
            or "boll_lb" in cell_text
            or "current price" in cell_text
            or "上轨" in cell
            or "中轨" in cell
            or "下轨" in cell
        ):
            band_columns += 1
    return band_columns >= 2


def _map_bollinger_label_value_row(cells: list[str]) -> dict[str, str]:
    if len(cells) < 2:
        return {}
    label = cells[0]
    value = _first_number(cells[1])
    if value is None:
        return {}
    label_text = label.casefold()
    if "upper" in label_text or "boll_ub" in label_text or "上轨" in label:
        return {"upper": value}
    if (
        "middle" in label_text
        or "20 sma" in label_text
        or label_text == "boll"
        or "中轨" in label
    ):
        return {"middle": value}
    if "lower" in label_text or "boll_lb" in label_text or "下轨" in label:
        return {"lower": value}
    if "current price" in label_text or label_text == "price":
        return {"current_price": value}
    return {}


def _map_bollinger_table_row(
    header_cells: list[str],
    row_values: list[str | None],
) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for index, header in enumerate(header_cells[: len(row_values)]):
        value = row_values[index]
        if value is None:
            continue
        header_text = header.casefold()
        if "upper" in header_text or "boll_ub" in header_text or "上轨" in header:
            mapped["upper"] = value
        elif (
            "middle" in header_text
            or "20 sma" in header_text
            or "boll)" in header_text
            or "中轨" in header
        ):
            mapped["middle"] = value
        elif "lower" in header_text or "boll_lb" in header_text or "下轨" in header:
            mapped["lower"] = value
        elif "current price" in header_text or header_text == "price":
            mapped["current_price"] = value
    return mapped


def _extract_labeled_number(report: str, labels: tuple[str, ...]) -> str | None:
    for raw_line in report.splitlines():
        line = _plain_cell(raw_line)
        line_casefold = line.casefold()
        for label in labels:
            index = line_casefold.find(label.casefold())
            if index == -1:
                continue
            tail = line[index + len(label) :]
            if not tail.lstrip().startswith((":","：", "|", "(", "-")):
                continue
            value = _first_number(tail)
            if value is not None:
                return value
    return None


def _extract_current_price(report: str) -> str | None:
    plain_report = _plain_cell(report)
    patterns = (
        r"On\s+[A-Z][a-z]+\s+\d{1,2}:\s*Price\s+at\s*\$?([0-9][0-9,]*(?:\.\d+)?)",
        r"Current\s+Price\s*\|?\s*\**\$?([0-9][0-9,]*(?:\.\d+)?)",
        r"Current\s+price\s*\(\$?([0-9][0-9,]*(?:\.\d+)?)\)",
        r"Price\s+Position\s*\|?\s*\**\$?([0-9][0-9,]*(?:\.\d+)?)",
        r"Relationship\s+to\s+Price\s*\(\$?([0-9][0-9,]*(?:\.\d+)?)\)",
        r"Current\s+close\s*\(\$?([0-9][0-9,]*(?:\.\d+)?)\)",
        r"close\s+of\s+\$?([0-9][0-9,]*(?:\.\d+)?)",
        r"Currently,\s+at\s+\$?([0-9][0-9,]*(?:\.\d+)?)",
        r"price\s*\(\$?([0-9][0-9,]*(?:\.\d+)?)\)",
        r"The\s+price\s+\(\$?([0-9][0-9,]*(?:\.\d+)?)\)",
    )
    for pattern in patterns:
        match = re.search(pattern, plain_report, flags=re.IGNORECASE)
        if match:
            return match.group(1).replace(",", "")
    return None


def _classify_bollinger_position(
    *,
    current: float,
    upper: float | None,
    lower: float | None,
) -> dict[str, str]:
    if upper is not None:
        if current >= upper:
            return {
                "position": "above_upper",
                "status": "upper_risk",
                "reference_band": "upper",
                "summary_zh": "当前价格已超过日线布林带上轨",
                "detail_zh": "价格处在布林带上沿之外，用于提示短线偏热和波动放大。",
            }
        if (upper - current) / upper <= 0.05:
            return {
                "position": "near_upper",
                "status": "upper_risk",
                "reference_band": "upper",
                "summary_zh": "当前价格贴近日线布林带上轨",
                "detail_zh": "价格接近布林带上沿，用于提示短线偏热和可能的均值回归压力。",
            }
    if lower is not None:
        if current <= lower:
            return {
                "position": "below_lower",
                "status": "lower_opportunity",
                "reference_band": "lower",
                "summary_zh": "当前价格已跌破日线布林带下轨",
                "detail_zh": "价格处在布林带下沿之外，用于提示短线超跌状态。",
            }
        if (current - lower) / lower <= 0.08:
            return {
                "position": "near_lower",
                "status": "lower_opportunity",
                "reference_band": "lower",
                "summary_zh": "当前价格接近日线布林带下轨",
                "detail_zh": "价格靠近布林带下沿，用于提示下轨附近的低位状态。",
            }
    return {
        "position": "middle_range",
        "status": "neutral",
        "reference_band": "",
        "summary_zh": "当前价格位于日线布林带区间内",
        "detail_zh": "价格未贴近上轨或下轨，布林带事实仅作背景展示。",
    }


def _format_bollinger_distance(
    *,
    current: float,
    reference: float | None,
    reference_band: str,
) -> str:
    if reference is None or not reference_band:
        return ""
    distance = (current - reference) / reference * 100
    band_label = "上轨" if reference_band == "upper" else "下轨"
    relation = "高于" if distance >= 0 else "低于"
    return f"{relation}{band_label} {abs(distance):.1f}%"


def _extract_bollinger_date(report: str) -> str:
    match = re.search(
        r"Bollinger[^\n]*(?:as of|Value)\s*\(?([A-Z][a-z]+\s+\d{1,2})\)?",
        report,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1)
    match = re.search(r"Current Band Levels\s*\(([A-Z][a-z]+\s+\d{1,2})\)", report)
    return match.group(1) if match else ""


def _plain_cell(value: str) -> str:
    return (
        value.replace("*", "")
        .replace("`", "")
        .replace("~", "")
        .replace("$", "")
        .replace("—", " ")
        .strip()
    )


def _cell_mentions(cell: str, needles: tuple[str, ...]) -> bool:
    cell_text = cell.casefold()
    return any(needle in cell_text for needle in needles)


def _is_separator_cell(cell: str) -> bool:
    return bool(cell) and set(cell) <= {"-", ":", " "}


def _first_number(text: str) -> str | None:
    match = NUMBER_TEXT_PATTERN.search(text)
    return match.group(0).replace(",", "") if match else None


def _to_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _format_number_text(value: str | None) -> str:
    if value is None:
        return ""
    numeric = _to_float(value)
    if numeric is None:
        return value
    return f"{numeric:.2f}"


def _latest_run_date(sources: list[AdviceSource]) -> str:
    dates = sorted(
        {
            source.run_date
            for source in sources
            if source.run_date
        }
    )
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
