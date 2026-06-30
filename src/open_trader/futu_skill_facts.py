from __future__ import annotations

import csv
import html
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from open_trader.market_scope import (
    MarketScope,
    market_run_dir,
    market_scoped_latest_path,
    parse_market_scope,
)


FUTU_SKILL_FACTS_SCHEMA_VERSION = "open_trader.futu_skill_facts.v1"
VALID_MODULE_STATUSES = {"ok", "partial", "missing", "error", "stale"}
VALID_SIGNALS = {"supportive", "opposing", "neutral", "risk_up", "mixed"}
VALID_CONFIDENCES = {"high", "medium", "low"}
VALID_CONSTRAINTS = {"", "review", "reduce_only", "wait_for_event", "no_add"}
RUN_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
FUTU_AI_SEARCH_BASE_URL = "https://ai-news-search.futunn.com"
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
BULLISH_CUES = (
    "bullish",
    "boost",
    "beats",
    "surge",
    "rally",
    "upgrade",
    "buyback",
    "inflow",
    "看好",
    "上涨",
    "反弹",
    "增长",
    "强",
    "利好",
    "回购",
    "流入",
)
BEARISH_CUES = (
    "bearish",
    "drop",
    "falls",
    "miss",
    "downgrade",
    "risk",
    "outflow",
    "selloff",
    "看空",
    "下跌",
    "回调",
    "疲弱",
    "风险",
    "利空",
    "流出",
)


@dataclass(frozen=True)
class FutuSkillFactResult:
    run_date: str
    records: int
    generated: int
    failed: int
    run_path: Path
    latest_path: Path


@dataclass(frozen=True)
class FutuSkillSource:
    market: str
    symbol: str
    name: str


class FutuSkillNewsSentimentExtractor(Protocol):
    def extract_news_sentiment(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
    ) -> dict[str, object]:
        ...


class FutuNewsSentimentExtractor:
    def __init__(
        self,
        *,
        http_get_json: Callable[[str, dict[str, object]], dict[str, object]] | None = None,
    ) -> None:
        self.http_get_json = http_get_json or _default_http_get_json

    def extract_news_sentiment(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
    ) -> dict[str, object]:
        del market, symbol, run_date
        keyword = name.strip() or symbol
        news_payload = self.http_get_json(
            f"{FUTU_AI_SEARCH_BASE_URL}/news_search",
            {
                "keyword": keyword,
                "size": 10,
                "news_type": 1,
                "lang": "zh-CN",
                "sort_type": 2,
            },
        )
        feed_payload = self.http_get_json(
            f"{FUTU_AI_SEARCH_BASE_URL}/stock_feed",
            {
                "keyword": keyword,
                "size": 30,
            },
        )
        evidence = [
            *_evidence_from_news_payload(news_payload),
            *_evidence_from_feed_payload(feed_payload),
        ][:6]
        if not evidence:
            return _missing_news_sentiment_module()
        signal = _classify_signal(evidence)
        return {
            "status": "ok",
            "signal": signal,
            "confidence": "medium" if len(evidence) >= 2 else "low",
            "freshness": {"generated_at": _now_text(), "source_window": "latest"},
            "evidence": evidence,
            "blocking_reason": "",
            "suggested_constraint": "",
        }


LLMFutuNewsSentimentExtractor = FutuNewsSentimentExtractor


def futu_skill_facts_run_path(
    data_dir: Path,
    run_date: str,
    market: MarketScope | str | None = None,
) -> Path:
    scope = _market_scope(market)
    if scope is not None:
        return market_run_dir(data_dir, run_date, scope) / "futu_skill_facts.json"
    return data_dir / "runs" / run_date / "futu_skill_facts.json"


def futu_skill_facts_latest_path(
    data_dir: Path,
    market: MarketScope | str | None = None,
) -> Path:
    scope = _market_scope(market)
    if scope is not None:
        return market_scoped_latest_path(data_dir, scope, "futu_skill_facts.json")
    return data_dir / "latest" / "futu_skill_facts.json"


def load_futu_skill_facts_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def index_futu_skill_facts_by_market_symbol(
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


def validate_futu_skill_fact_record(record: dict[str, object]) -> None:
    if not isinstance(record, dict):
        raise ValueError("futu skill fact record must be an object")
    if record.get("schema_version") != FUTU_SKILL_FACTS_SCHEMA_VERSION:
        raise ValueError("futu skill fact schema_version is invalid")
    for field in ("run_date", "market", "symbol", "name", "error"):
        if field not in record or not isinstance(record[field], str):
            raise ValueError(f"futu skill fact {field} is invalid")
    if not record["run_date"].strip():
        raise ValueError("futu skill fact run_date is invalid")
    if not record["market"].strip():
        raise ValueError("futu skill fact market is invalid")
    if not record["symbol"].strip():
        raise ValueError("futu skill fact symbol is invalid")
    _validate_news_sentiment_module(record.get("news_sentiment"))


def generate_futu_skill_facts(
    *,
    portfolio_path: Path,
    data_dir: Path,
    run_date: str,
    market: MarketScope | str | None,
    extractor: FutuSkillNewsSentimentExtractor,
    update_latest: bool,
) -> FutuSkillFactResult:
    effective_run_date = _validate_run_date(run_date)
    market_scope = _market_scope(market)
    sources = _load_portfolio_sources(portfolio_path, market_scope)
    run_path = futu_skill_facts_run_path(data_dir, effective_run_date, market_scope)
    latest_path = futu_skill_facts_latest_path(data_dir, market_scope)
    records = [
        _build_record(
            source=source,
            run_date=effective_run_date,
            extractor=extractor,
        )
        for source in sources
    ]
    failed = sum(1 for record in records if str(record.get("error") or ""))
    payload = {
        "schema_version": FUTU_SKILL_FACTS_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "run_date": effective_run_date,
        "market": market_scope.value if market_scope is not None else "",
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


def _build_record(
    *,
    source: FutuSkillSource,
    run_date: str,
    extractor: FutuSkillNewsSentimentExtractor,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": FUTU_SKILL_FACTS_SCHEMA_VERSION,
        "run_date": run_date,
        "market": source.market,
        "symbol": source.symbol,
        "name": source.name,
    }
    try:
        module = extractor.extract_news_sentiment(
            market=source.market,
            symbol=source.symbol,
            name=source.name,
            run_date=run_date,
        )
        normalized = _normalize_news_sentiment_module(module)
        record = {**base, "news_sentiment": normalized, "error": ""}
    except Exception as exc:
        record = {
            **base,
            "news_sentiment": _error_news_sentiment_module(),
            "error": str(exc) or exc.__class__.__name__,
        }
    validate_futu_skill_fact_record(record)
    return record


def _normalize_news_sentiment_module(module: object) -> dict[str, Any]:
    if not isinstance(module, dict):
        raise ValueError("news_sentiment module is invalid")
    normalized = {
        "status": _required_enum(module, "status", VALID_MODULE_STATUSES, "news_sentiment"),
        "signal": _required_enum(module, "signal", VALID_SIGNALS, "news_sentiment"),
        "confidence": _required_enum(module, "confidence", VALID_CONFIDENCES, "news_sentiment"),
        "freshness": _normalize_freshness(module.get("freshness")),
        "evidence": _normalize_evidence(module.get("evidence")),
        "blocking_reason": _optional_text(module.get("blocking_reason")),
        "suggested_constraint": _required_enum(
            module,
            "suggested_constraint",
            VALID_CONSTRAINTS,
            "news_sentiment",
        ),
    }
    _validate_news_sentiment_module(normalized)
    return normalized


def _validate_news_sentiment_module(module: object) -> None:
    if not isinstance(module, dict):
        raise ValueError("news_sentiment module is invalid")
    _validate_enum(module, "status", VALID_MODULE_STATUSES, "news_sentiment")
    _validate_enum(module, "signal", VALID_SIGNALS, "news_sentiment")
    _validate_enum(module, "confidence", VALID_CONFIDENCES, "news_sentiment")
    _validate_enum(module, "suggested_constraint", VALID_CONSTRAINTS, "news_sentiment")
    freshness = module.get("freshness")
    if not isinstance(freshness, dict):
        raise ValueError("news_sentiment freshness is invalid")
    for field in ("generated_at", "source_window"):
        if not isinstance(freshness.get(field), str):
            raise ValueError(f"news_sentiment freshness {field} is invalid")
    evidence = module.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("news_sentiment evidence is invalid")
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("news_sentiment evidence item is invalid")
        for field in ("title", "summary", "url"):
            if not isinstance(item.get(field), str):
                raise ValueError(f"news_sentiment evidence {field} is invalid")
    if not isinstance(module.get("blocking_reason"), str):
        raise ValueError("news_sentiment blocking_reason is invalid")


def _load_portfolio_sources(
    portfolio_path: Path,
    market_scope: MarketScope | None,
) -> list[FutuSkillSource]:
    if not portfolio_path.exists():
        raise FileNotFoundError(f"portfolio CSV not found: {portfolio_path}")
    csv.field_size_limit(sys.maxsize)
    sources: list[FutuSkillSource] = []
    seen: set[tuple[str, str]] = set()
    with portfolio_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            asset_class = str(row.get("asset_class") or "").strip().lower()
            if asset_class == "cash":
                continue
            market = str(row.get("market") or "").strip().upper()
            symbol = str(row.get("symbol") or "").strip().upper()
            if not market or not symbol:
                continue
            if market_scope is not None and market != market_scope.value:
                continue
            key = (market, symbol)
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                FutuSkillSource(
                    market=market,
                    symbol=symbol,
                    name=str(row.get("name") or "").strip(),
                )
            )
    return sources


def _default_http_get_json(url: str, params: dict[str, object]) -> dict[str, object]:
    query = urlencode({key: str(value) for key, value in params.items()})
    request = Request(
        f"{url}?{query}",
        headers={"User-Agent": "open_trader-futu-skill-facts/0.1"},
    )
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _evidence_from_news_payload(payload: dict[str, object]) -> list[dict[str, str]]:
    if payload.get("code") not in (0, "0"):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    evidence = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"))
        url = _optional_text(item.get("url"))
        if not title:
            continue
        evidence.append({"title": title, "summary": title, "url": url})
    return evidence


def _evidence_from_feed_payload(payload: dict[str, object]) -> list[dict[str, str]]:
    if payload.get("code") not in (0, "0"):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    evidence = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"))
        desc = _clean_text(item.get("desc"))
        summary = " ".join(part for part in (title, desc) if part).strip()
        url = _optional_text(item.get("url"))
        if not title and not summary:
            continue
        evidence.append(
            {
                "title": title or summary,
                "summary": summary or title,
                "url": url,
            }
        )
    return evidence


def _classify_signal(evidence: list[dict[str, str]]) -> str:
    text = " ".join(
        f"{item.get('title', '')} {item.get('summary', '')}" for item in evidence
    ).casefold()
    bullish_count = sum(1 for cue in BULLISH_CUES if cue.casefold() in text)
    bearish_count = sum(1 for cue in BEARISH_CUES if cue.casefold() in text)
    if bullish_count and bearish_count:
        return "mixed"
    if bullish_count:
        return "supportive"
    if bearish_count:
        return "risk_up"
    return "neutral"


def _clean_text(value: object) -> str:
    text = _optional_text(value)
    if not text:
        return ""
    text = HTML_TAG_PATTERN.sub(" ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def _missing_news_sentiment_module() -> dict[str, Any]:
    return {
        "status": "missing",
        "signal": "neutral",
        "confidence": "low",
        "freshness": {"generated_at": _now_text(), "source_window": ""},
        "evidence": [],
        "blocking_reason": "",
        "suggested_constraint": "review",
    }


def _error_news_sentiment_module() -> dict[str, Any]:
    return {
        "status": "error",
        "signal": "neutral",
        "confidence": "low",
        "freshness": {"generated_at": _now_text(), "source_window": ""},
        "evidence": [],
        "blocking_reason": "",
        "suggested_constraint": "review",
    }


def _normalize_freshness(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"generated_at": "", "source_window": ""}
    return {
        "generated_at": _optional_text(value.get("generated_at")),
        "source_window": _optional_text(value.get("source_window")),
    }


def _normalize_evidence(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    evidence: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        evidence.append(
            {
                "title": _optional_text(item.get("title")),
                "summary": _optional_text(item.get("summary")),
                "url": _optional_text(item.get("url")),
            }
        )
    return evidence


def _required_enum(
    mapping: dict[str, object],
    field: str,
    valid_values: set[str],
    module_name: str,
) -> str:
    value = _optional_text(mapping.get(field))
    if value not in valid_values:
        raise ValueError(f"{module_name} {field} is invalid")
    return value


def _validate_enum(
    mapping: dict[str, object],
    field: str,
    valid_values: set[str],
    module_name: str,
) -> None:
    if not isinstance(mapping.get(field), str) or mapping[field] not in valid_values:
        raise ValueError(f"{module_name} {field} is invalid")


def _optional_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _validate_run_date(value: str) -> str:
    text = value.strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"invalid run_date: {value}") from exc
    return text


def _market_scope(market: MarketScope | str | None) -> MarketScope | None:
    if market is None or market == "":
        return None
    return parse_market_scope(market)


def _now_text() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temp:
        json.dump(payload, temp, ensure_ascii=False, indent=2, sort_keys=True)
        temp.write("\n")
        temp_path = Path(temp.name)
    temp_path.replace(path)
