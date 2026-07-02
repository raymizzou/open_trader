from __future__ import annotations

import csv
import html
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .decision_facts import OpenAITextClient
from open_trader.market_scope import (
    MarketScope,
    market_run_dir,
    market_scoped_latest_path,
    parse_market_scope,
)


FUTU_SKILL_FACTS_SCHEMA_VERSION = "open_trader.futu_skill_facts.v1"
FUTU_STOCK_FEED_CACHE_FILENAME = "futu_stock_feed_cache.json"
FUTU_STOCK_FEED_CACHE_DAYS = 7
FUTU_STOCK_FEED_SIZE = 50
VALID_MODULE_STATUSES = {"ok", "partial", "missing", "error", "stale"}
VALID_SIGNALS = {"supportive", "opposing", "neutral", "risk_up", "mixed"}
VALID_CONFIDENCES = {"high", "medium", "low"}
VALID_CONSTRAINTS = {"", "review", "reduce_only", "wait_for_event", "no_add"}
VALID_CATEGORY_STATES = {"anomaly", "none", "not_applicable", "error"}
VALID_CATEGORY_DIRECTIONS = {"", "bullish", "bearish", "neutral", "risk_up", "mixed"}
VALID_DOMESTIC_STATUSES = {"ok", "missing", "error"}
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
RUN_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
FUTU_AI_SEARCH_BASE_URL = "https://ai-news-search.futunn.com"
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
GENERIC_COMMUNITY_TERMS = {
    "adr",
    "ai",
    "etf",
    "inc",
    "ltd",
    "the",
    "trust",
}
RELATED_COMMUNITY_TERMS_BY_SYMBOL = {
    "DRAM": {
        "dram",
        "memory",
        "mu",
        "mu.us",
        "美光",
        "美光科技",
        "sndk",
        "sndk.us",
        "闪迪",
        "sk海力士",
        "海力士",
        "000660",
        "000660.kr",
        "存储",
        "内存",
        "存储链",
    },
    "RAM": {
        "dram",
        "memory",
        "mu",
        "mu.us",
        "美光",
        "美光科技",
        "sndk",
        "sndk.us",
        "闪迪",
        "sk海力士",
        "海力士",
        "000660",
        "000660.kr",
        "存储",
        "内存",
        "存储链",
    },
    "07709": {
        "07709",
        "sk海力士",
        "海力士",
        "000660",
        "000660.kr",
        "hynix",
        "存储",
        "内存",
    },
}
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


class FutuDomesticDiscussionSummarizer(Protocol):
    def summarize(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        news_items: list[dict[str, str]],
        community_items: list[dict[str, str]],
        post_count: int,
        relevant_post_count: int,
    ) -> dict[str, object]:
        ...


class FutuAnomalyModuleSummarizer(Protocol):
    def summarize(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        module_name: str,
        module: dict[str, object],
    ) -> dict[str, object]:
        ...


class LLMFutuAnomalyModuleSummarizer:
    def __init__(self, *, client: object | None = None) -> None:
        self.client = client or OpenAITextClient()

    def summarize(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        module_name: str,
        module: dict[str, object],
    ) -> dict[str, object]:
        messages = [
            {
                "role": "system",
                "content": _anomaly_module_summary_system_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "market": market,
                        "symbol": symbol,
                        "name": name,
                        "module_name": module_name,
                        "module": module,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        content = self.client.create(messages=messages, temperature=0)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM anomaly summary response must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("LLM anomaly summary response must be a JSON object")
        return _apply_anomaly_summary_payload(module, payload, module_name)


class LLMFutuDomesticDiscussionSummarizer:
    def __init__(self, *, client: object | None = None) -> None:
        self.client = client or OpenAITextClient()

    def summarize(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        news_items: list[dict[str, str]],
        community_items: list[dict[str, str]],
        post_count: int,
        relevant_post_count: int,
    ) -> dict[str, object]:
        messages = [
            {
                "role": "system",
                "content": _domestic_discussion_system_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "market": market,
                        "symbol": symbol,
                        "name": name,
                        "post_count": post_count,
                        "relevant_post_count": relevant_post_count,
                        "news_items": news_items[:8],
                        "community_items": community_items[:8],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        content = self.client.create(messages=messages, temperature=0)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM domestic discussion response must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("LLM domestic discussion response must be a JSON object")
        status = _optional_text(payload.get("status"))
        normalized = _normalize_domestic_discussion(
            {
                **payload,
                "status": status if status in VALID_DOMESTIC_STATUSES else "ok",
                "post_count": post_count,
                "relevant_post_count": relevant_post_count,
            }
        )
        return normalized


class FutuNewsSentimentExtractor:
    def __init__(
        self,
        *,
        http_get_json: Callable[[str, dict[str, object]], dict[str, object]] | None = None,
        domestic_summarizer: FutuDomesticDiscussionSummarizer | None = None,
    ) -> None:
        self.http_get_json = http_get_json or _default_http_get_json
        self.domestic_summarizer = (
            domestic_summarizer or LLMFutuDomesticDiscussionSummarizer()
        )
        self._prepared_feed_items_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._prepared_feed_counts_by_key: dict[tuple[str, str], int] = {}

    def prepare_sources(
        self,
        *,
        sources: list[FutuSkillSource],
        data_dir: Path,
        run_date: str,
        market: MarketScope | str | None,
    ) -> None:
        market_scope = _market_scope(market)
        fetched_items: list[dict[str, Any]] = []
        for source in sources:
            try:
                payload = self.http_get_json(
                    f"{FUTU_AI_SEARCH_BASE_URL}/stock_feed",
                    {
                        "keyword": source.symbol,
                        "size": FUTU_STOCK_FEED_SIZE,
                    },
                )
            except Exception:
                continue
            fetched_items.extend(
                _feed_items_from_payload(payload, query_symbol=source.symbol)
            )
        cache_path = futu_stock_feed_cache_path(data_dir, market_scope)
        cached_items = _load_stock_feed_cache_items(cache_path)
        window_items = _merge_stock_feed_cache_items(
            cached_items=cached_items,
            fetched_items=fetched_items,
            run_date=run_date,
        )
        _write_stock_feed_cache(
            cache_path,
            market=market_scope.value if market_scope is not None else "",
            items=window_items,
        )
        self._prepared_feed_counts_by_key = {
            (source.market.upper(), source.symbol.upper()): len(window_items)
            for source in sources
        }
        self._prepared_feed_items_by_key = _assign_feed_items_to_sources(
            window_items,
            sources=sources,
        )

    def extract_news_sentiment(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
    ) -> dict[str, object]:
        del run_date
        news_keyword = name.strip() or symbol
        news_payload = self.http_get_json(
            f"{FUTU_AI_SEARCH_BASE_URL}/news_search",
            {
                "keyword": news_keyword,
                "size": 10,
                "news_type": 1,
                "lang": "zh-CN",
                "sort_type": 2,
            },
        )
        news_evidence = _evidence_from_news_payload(news_payload)
        prepared_key = (market.upper(), symbol.upper())
        if prepared_key in self._prepared_feed_items_by_key:
            feed_items = self._prepared_feed_items_by_key[prepared_key]
            community_evidence = [_public_feed_item(item) for item in feed_items]
            post_count = self._prepared_feed_counts_by_key.get(prepared_key, len(feed_items))
            source_window = f"rolling_{FUTU_STOCK_FEED_CACHE_DAYS}d"
        else:
            feed_payload = self.http_get_json(
                f"{FUTU_AI_SEARCH_BASE_URL}/stock_feed",
                {
                    "keyword": symbol.strip() or news_keyword,
                    "size": 30,
                },
            )
            feed_items = _feed_items_from_payload(feed_payload, query_symbol=symbol)
            community_evidence = _relevant_community_evidence(
                feed_items,
                symbol=symbol,
                name=name,
            )
            post_count = len(feed_items)
            source_window = "latest"
        domestic_discussion = self._summarize_domestic_discussion(
            market=market,
            symbol=symbol,
            name=name,
            news_items=news_evidence,
            community_items=community_evidence,
            post_count=post_count,
            relevant_post_count=len(community_evidence),
        )
        evidence = [
            *news_evidence,
            *community_evidence,
        ][:6]
        if not evidence:
            return {
                **_missing_news_sentiment_module(),
                "domestic_discussion": domestic_discussion,
            }
        signal = _classify_signal(evidence)
        return {
            "status": "ok",
            "signal": signal,
            "confidence": "medium" if len(evidence) >= 2 else "low",
            "freshness": {"generated_at": _now_text(), "source_window": source_window},
            "evidence": evidence,
            "domestic_discussion": domestic_discussion,
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
        del market, symbol, name, run_date
        return _missing_signal_module("technical_anomaly", window_days)

    def extract_capital_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        del market, symbol, name, run_date
        return _missing_signal_module("capital_anomaly", window_days)

    def extract_derivatives_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        del market, symbol, name, run_date
        return _missing_signal_module("derivatives_anomaly", window_days)

    def _summarize_domestic_discussion(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        news_items: list[dict[str, str]],
        community_items: list[dict[str, str]],
        post_count: int,
        relevant_post_count: int,
    ) -> dict[str, object]:
        if relevant_post_count == 0:
            return _fallback_domestic_discussion(
                symbol=symbol,
                post_count=post_count,
                relevant_post_count=0,
            )
        try:
            return self.domestic_summarizer.summarize(
                market=market,
                symbol=symbol,
                name=name,
                news_items=news_items,
                community_items=community_items,
                post_count=post_count,
                relevant_post_count=relevant_post_count,
            )
        except Exception:
            return _fallback_domestic_discussion(
                symbol=symbol,
                post_count=post_count,
                relevant_post_count=relevant_post_count,
            )


LLMFutuNewsSentimentExtractor = FutuNewsSentimentExtractor


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
            raise RuntimeError(
                stderr.strip()
                or stdout.strip()
                or f"{module} anomaly script failed"
            )
        try:
            payload = _load_json_object_from_mixed_output(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{module} anomaly script returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{module} anomaly script returned non-object JSON")
        return payload

    def _script_path(self, module: str) -> Path:
        mapping = {
            "technical": self.skill_root
            / "futu-technical-anomaly/scripts/handle_technical_anomaly.py",
            "capital": self.skill_root
            / "futu-capital-anomaly/scripts/handle_capital_anomaly.py",
            "derivatives": self.skill_root
            / "futu-derivatives-anomaly/scripts/handle_derivatives_anomaly.py",
        }
        try:
            return mapping[module]
        except KeyError as exc:
            raise ValueError(f"unknown anomaly module: {module}") from exc

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


def _load_json_object_from_mixed_output(output: str) -> object:
    stripped = output.strip()
    if not stripped:
        raise json.JSONDecodeError("empty output", output, 0)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    while start >= 0:
        candidate = _balanced_json_object_text(stripped, start)
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        start = stripped.find("{", start + 1)
    raise json.JSONDecodeError("no JSON object found", output, 0)


def _balanced_json_object_text(text: str, start: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


class FutuSkillFactsExtractor:
    def __init__(
        self,
        *,
        news_extractor: FutuSkillNewsSentimentExtractor | None = None,
        anomaly_client: FutuAnomalyScriptClient | None = None,
        anomaly_summarizer: FutuAnomalyModuleSummarizer | None = None,
    ) -> None:
        self.news_extractor = news_extractor or FutuNewsSentimentExtractor()
        self.anomaly_client = anomaly_client or FutuAnomalyScriptClient()
        self.anomaly_summarizer = anomaly_summarizer or LLMFutuAnomalyModuleSummarizer()

    def prepare_sources(
        self,
        *,
        sources: list[FutuSkillSource],
        data_dir: Path,
        run_date: str,
        market: MarketScope | str | None,
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
        del run_date
        payload = self.anomaly_client.run(
            "technical",
            market=market,
            symbol=symbol,
            window_days=window_days,
        )
        module = _normalize_anomaly_payload(
            payload,
            module_name="technical_anomaly",
            category_labels=TECHNICAL_ANOMALY_CATEGORY_LABELS,
            window_days=window_days,
        )
        return self._summarize_anomaly_module(
            market=market,
            symbol=symbol,
            name=name,
            module_name="technical_anomaly",
            module=module,
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
        del run_date
        payload = self.anomaly_client.run(
            "capital",
            market=market,
            symbol=symbol,
            window_days=window_days,
        )
        module = _normalize_anomaly_payload(
            payload,
            module_name="capital_anomaly",
            category_labels=CAPITAL_ANOMALY_CATEGORY_LABELS,
            window_days=window_days,
        )
        return self._summarize_anomaly_module(
            market=market,
            symbol=symbol,
            name=name,
            module_name="capital_anomaly",
            module=module,
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
        del run_date
        payload = self.anomaly_client.run(
            "derivatives",
            market=market,
            symbol=symbol,
            window_days=window_days,
        )
        labels = (
            DERIVATIVES_ANOMALY_CATEGORY_LABELS_HK
            if market.upper() == "HK"
            else DERIVATIVES_ANOMALY_CATEGORY_LABELS_US
        )
        module = _normalize_anomaly_payload(
            payload,
            module_name="derivatives_anomaly",
            category_labels=labels,
            window_days=window_days,
        )
        return self._summarize_anomaly_module(
            market=market,
            symbol=symbol,
            name=name,
            module_name="derivatives_anomaly",
            module=module,
        )

    def _summarize_anomaly_module(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        module_name: str,
        module: dict[str, object],
    ) -> dict[str, object]:
        if not _module_needs_detail_summary(module):
            return module
        try:
            return self.anomaly_summarizer.summarize(
                market=market,
                symbol=symbol,
                name=name,
                module_name=module_name,
                module=module,
            )
        except Exception:
            return _compact_anomaly_module_details(module, module_name)


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


def futu_stock_feed_cache_path(
    data_dir: Path,
    market: MarketScope | str | None = None,
) -> Path:
    scope = _market_scope(market)
    if scope is not None:
        return market_scoped_latest_path(data_dir, scope, FUTU_STOCK_FEED_CACHE_FILENAME)
    return data_dir / "latest" / FUTU_STOCK_FEED_CACHE_FILENAME


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
    _validate_signal_module(record.get("technical_anomaly"), "technical_anomaly")
    _validate_signal_module(record.get("capital_anomaly"), "capital_anomaly")
    _validate_signal_module(record.get("derivatives_anomaly"), "derivatives_anomaly")


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
        module = extractor.extract_news_sentiment(
            market=source.market,
            symbol=source.symbol,
            name=source.name,
            run_date=run_date,
        )
        news_sentiment = _normalize_news_sentiment_module(module)
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        news_sentiment = _error_news_sentiment_module()
        errors.append(f"news_sentiment: {reason}")
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
        reason = str(exc) or exc.__class__.__name__
        technical_anomaly = _error_signal_module(
            "technical_anomaly",
            window_days,
            reason,
        )
        errors.append(f"technical_anomaly: {reason}")
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
        reason = str(exc) or exc.__class__.__name__
        capital_anomaly = _error_signal_module(
            "capital_anomaly",
            window_days,
            reason,
        )
        errors.append(f"capital_anomaly: {reason}")
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
        reason = str(exc) or exc.__class__.__name__
        derivatives_anomaly = _error_signal_module(
            "derivatives_anomaly",
            window_days,
            reason,
        )
        errors.append(f"derivatives_anomaly: {reason}")
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


def _normalize_news_sentiment_module(module: object) -> dict[str, Any]:
    if not isinstance(module, dict):
        raise ValueError("news_sentiment module is invalid")
    normalized = {
        "status": _required_enum(module, "status", VALID_MODULE_STATUSES, "news_sentiment"),
        "signal": _required_enum(module, "signal", VALID_SIGNALS, "news_sentiment"),
        "confidence": _required_enum(module, "confidence", VALID_CONFIDENCES, "news_sentiment"),
        "freshness": _normalize_freshness(module.get("freshness")),
        "evidence": _normalize_evidence(module.get("evidence")),
        "domestic_discussion": _normalize_domestic_discussion(
            module.get("domestic_discussion")
        ),
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
    if "domestic_discussion" in module:
        _validate_domestic_discussion(module.get("domestic_discussion"))


def _normalize_signal_module(module: object, module_name: str) -> dict[str, Any]:
    if not isinstance(module, dict):
        raise ValueError(f"{module_name} module is invalid")
    raw_window_days = module.get("window_days")
    window_days = 7 if raw_window_days is None or raw_window_days == "" else raw_window_days
    normalized = {
        "status": _required_enum(module, "status", VALID_MODULE_STATUSES, module_name),
        "signal": _required_enum(module, "signal", VALID_SIGNALS, module_name),
        "confidence": _required_enum(module, "confidence", VALID_CONFIDENCES, module_name),
        "suggested_constraint": _required_enum(
            module,
            "suggested_constraint",
            VALID_CONSTRAINTS,
            module_name,
        ),
        "window_days": _validate_window_days(window_days),
        "summary": _optional_text(module.get("summary")),
        "categories": _normalize_signal_categories(
            module.get("categories"),
            module_name,
        ),
    }
    _validate_signal_module(normalized, module_name)
    return normalized


def _normalize_signal_categories(
    categories: object,
    module_name: str,
) -> list[dict[str, str]]:
    if not isinstance(categories, list):
        raise ValueError(f"{module_name} categories is invalid")
    normalized: list[dict[str, str]] = []
    for item in categories:
        if not isinstance(item, dict):
            raise ValueError(f"{module_name} category is invalid")
        normalized.append(
            {
                "name": _required_text(item, "name", f"{module_name} category"),
                "state": _required_enum(
                    item,
                    "state",
                    VALID_CATEGORY_STATES,
                    f"{module_name} category",
                ),
                "direction": _required_enum(
                    item,
                    "direction",
                    VALID_CATEGORY_DIRECTIONS,
                    f"{module_name} category",
                ),
                "detail": _required_text(item, "detail", f"{module_name} category"),
                "evidence_date": _optional_text(item.get("evidence_date")),
            }
        )
    return normalized


def _module_needs_detail_summary(module: dict[str, object]) -> bool:
    categories = module.get("categories")
    if not isinstance(categories, list):
        return False
    return any(
        isinstance(category, dict)
        and category.get("state") == "anomaly"
        and len(_optional_text(category.get("detail"))) > 120
        for category in categories
    )


def _apply_anomaly_summary_payload(
    module: dict[str, object],
    payload: dict[str, object],
    module_name: str,
) -> dict[str, object]:
    summarized = dict(module)
    summary = _optional_text(payload.get("summary"))
    if summary:
        summarized["summary"] = _bounded_text(summary, 120)
    category_updates = {
        _optional_text(item.get("name")): item
        for item in payload.get("categories", [])
        if isinstance(item, dict) and _optional_text(item.get("name"))
    } if isinstance(payload.get("categories"), list) else {}
    categories = []
    for category in module.get("categories", []):
        if not isinstance(category, dict):
            continue
        updated = dict(category)
        replacement = category_updates.get(_optional_text(category.get("name")))
        if isinstance(replacement, dict):
            detail = _optional_text(replacement.get("detail"))
            direction = _optional_text(replacement.get("direction"))
            if detail:
                updated["detail"] = _bounded_text(detail, 90)
            if direction in VALID_CATEGORY_DIRECTIONS:
                updated["direction"] = direction
        categories.append(updated)
    summarized["categories"] = categories
    normalized = _normalize_signal_module(summarized, module_name)
    _validate_signal_module(normalized, module_name)
    return normalized


def _compact_anomaly_module_details(
    module: dict[str, object],
    module_name: str,
) -> dict[str, object]:
    compacted = dict(module)
    categories = []
    for category in module.get("categories", []):
        if not isinstance(category, dict):
            continue
        updated = dict(category)
        detail = _optional_text(updated.get("detail"))
        if len(detail) > 120:
            updated["detail"] = _compact_anomaly_detail(detail)
        categories.append(updated)
    compacted["categories"] = categories
    normalized = _normalize_signal_module(compacted, module_name)
    _validate_signal_module(normalized, module_name)
    return normalized


def _compact_anomaly_detail(detail: str) -> str:
    text = re.sub(r"\[timestamp:\s*\d+\]", "。", detail)
    text = " ".join(text.split())
    parts = [
        part.strip(" ，,。")
        for part in re.split(r"[。；;]\s*", text)
        if part.strip(" ，,。")
    ]
    compact = "；".join(parts[:2]) if parts else text
    return _bounded_text(compact, 90)


def _bounded_text(text: str, max_chars: int) -> str:
    value = " ".join(_optional_text(text).split())
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip(" ，,；;。") + "…"


def _validate_signal_module(module: object, module_name: str) -> None:
    if not isinstance(module, dict):
        raise ValueError(f"{module_name} module is invalid")
    _validate_enum(module, "status", VALID_MODULE_STATUSES, module_name)
    _validate_enum(module, "signal", VALID_SIGNALS, module_name)
    _validate_enum(module, "confidence", VALID_CONFIDENCES, module_name)
    _validate_enum(module, "suggested_constraint", VALID_CONSTRAINTS, module_name)
    if not isinstance(module.get("window_days"), int):
        raise ValueError(f"{module_name} window_days is invalid")
    _validate_window_days(module["window_days"])
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
        _validate_enum(
            category,
            "state",
            VALID_CATEGORY_STATES,
            f"{module_name} category",
        )
        _validate_enum(
            category,
            "direction",
            VALID_CATEGORY_DIRECTIONS,
            f"{module_name} category",
        )


def _normalize_anomaly_payload(
    payload: dict[str, object],
    *,
    module_name: str,
    category_labels: tuple[str, ...],
    window_days: int,
) -> dict[str, object]:
    data = payload.get("data")
    rows = [
        *_anomaly_rows(data),
        *_content_anomaly_rows(
            _sdk_content_text(data),
            category_labels=category_labels,
        ),
    ]
    categories = [
        _category_from_rows(label, rows)
        for label in category_labels
    ]
    anomaly_categories = [
        item for item in categories if item["state"] == "anomaly"
    ]
    risk_categories = [
        item
        for item in anomaly_categories
        if item["direction"] in {"bearish", "risk_up", "mixed"}
    ]
    supportive_categories = [
        item for item in anomaly_categories if item["direction"] == "bullish"
    ]
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
    module = {
        "status": "ok",
        "signal": signal,
        "confidence": "medium" if anomaly_categories else "low",
        "suggested_constraint": suggested_constraint,
        "window_days": _validate_window_days(window_days),
        "summary": _signal_summary(module_name, signal, suggested_constraint),
        "categories": categories,
    }
    _validate_signal_module(module, module_name)
    return module


def _sdk_content_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    return _optional_text(data.get("content"))


def _content_anomaly_rows(
    content: str,
    *,
    category_labels: tuple[str, ...],
) -> list[dict[str, object]]:
    if not content:
        return []
    return [
        {
            "name": label,
            "description": content,
        }
        for label in category_labels
        if _text_matches_category(content, label)
    ]


def _anomaly_rows(data: object) -> list[dict[str, object]]:
    if isinstance(data, dict):
        rows: list[dict[str, object]] = []
        if any(
            field in data
            for field in (
                "name",
                "category",
                "type",
                "indicator",
                "title",
                "description",
                "direction",
                "date",
                "detail",
                "summary",
            )
        ):
            rows.append(data)
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


def _category_from_rows(
    label: str,
    rows: list[dict[str, object]],
) -> dict[str, str]:
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


def _row_matches_category(row: dict[str, object], label: str) -> bool:
    normalized_label = label.casefold()
    for field in ("name", "category", "type", "indicator", "title"):
        value = _optional_text(row.get(field)).casefold()
        if value == normalized_label:
            return True
    return _text_matches_category(_row_text(row), label)


def _text_matches_category(text: str, label: str) -> bool:
    normalized_text = text.casefold()
    aliases = {
        "K线形态": ("k线", "形态", "pattern"),
        "资金分布与买卖经纪商": (
            "资金分布",
            "经纪商",
            "broker",
            "funds_distribution",
            "funds_broker",
        ),
        "资金流向": ("资金流向", "flow", "funds_flow"),
        "卖空情况": ("卖空", "short"),
        "牛熊证街货比例": ("牛熊证街货比例", "warrant_ratio"),
        "牛熊证街货价格区间": (
            "牛熊证街货价格区间",
            "warrant_price_distribution",
        ),
        "期权大单": ("期权大单", "option_unusual"),
        "期权波动率": ("期权波动率", "iv", "volatility", "option_volatility"),
        "期权量价": ("期权量价", "volume", "option_volume_price"),
        "期权情绪": ("期权情绪", "put/call", "pcr", "option_sentiment"),
        "期权综合信号": ("期权综合", "option_comprehensive"),
    }
    terms = aliases.get(label, (label.casefold(),))
    return any(
        _term_matches_text(term, normalized_text)
        for term in terms
        if term
    )


def _term_matches_text(term: str, normalized_text: str) -> bool:
    normalized_term = term.casefold()
    if re.fullmatch(r"[a-z0-9/]+", normalized_term):
        pattern = (
            rf"(?<![a-z0-9]){re.escape(normalized_term)}"
            rf"(?![a-z0-9])"
        )
        return re.search(pattern, normalized_text) is not None
    return normalized_term in normalized_text


def _row_text(row: dict[str, object]) -> str:
    return json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).casefold()


def _row_direction(row: dict[str, object]) -> str:
    explicit = _explicit_row_direction(row)
    if explicit:
        return explicit
    text = _row_text(row)
    if "risk_up" in text or "风险" in text or "超买" in text:
        return "risk_up"
    if any(
        term in text
        for term in ("bearish", "看跌", "偏空", "流出", "卖空", "short")
    ):
        return "bearish"
    if any(term in text for term in ("mixed", "分歧")):
        return "mixed"
    if any(term in text for term in ("bullish", "看涨", "偏多", "流入", "金叉")):
        return "bullish"
    return "neutral"


def _explicit_row_direction(row: dict[str, object]) -> str:
    direction = _optional_text(row.get("direction")).casefold()
    labels = {
        "bullish": "bullish",
        "偏多": "bullish",
        "看涨": "bullish",
        "bearish": "bearish",
        "偏空": "bearish",
        "看跌": "bearish",
        "risk_up": "risk_up",
        "风险上升": "risk_up",
        "mixed": "mixed",
        "分歧": "mixed",
        "neutral": "neutral",
        "中性": "neutral",
    }
    return labels.get(direction, "")


def _row_detail_text(row: dict[str, object]) -> str:
    for field in ("description", "interpretation", "summary", "detail"):
        value = _optional_text(row.get(field))
        if value:
            return value
    detail_fields = {
        key: value
        for key, value in row.items()
        if key
        not in {
            "name",
            "category",
            "type",
            "indicator",
            "title",
            "direction",
            "date",
            "datetime",
            "time",
            "occur_date",
        }
        and _optional_text(value)
    }
    if detail_fields:
        return json.dumps(
            detail_fields,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    return _row_text(row)


def _row_date(row: dict[str, object]) -> str:
    for field in ("date", "datetime", "time", "occur_date"):
        value = _optional_text(row.get(field))
        if value:
            return value
    return ""


def _signal_summary(module_name: str, signal: str, suggested_constraint: str) -> str:
    del module_name
    if signal == "supportive":
        return "异动信号支持当前交易方向。"
    if signal == "risk_up":
        return "异动信号提示风险上升。"
    if signal == "mixed":
        return "异动信号存在分歧，需要结合主结论复核。"
    if suggested_constraint:
        return "异动信号触发执行约束。"
    return "窗口内未发现明显异动。"


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


def _load_stock_feed_cache_items(path: Path) -> list[dict[str, Any]]:
    payload = load_futu_skill_facts_cache(path)
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized.append(_normalize_cached_feed_item(item))
    return normalized


def _normalize_cached_feed_item(item: dict[str, object]) -> dict[str, Any]:
    query_symbols = item.get("query_symbols")
    stock_terms = item.get("stock_terms")
    return {
        "id": _optional_text(item.get("id")) or _feed_item_fallback_id(
            item.get("title"),
            item.get("summary"),
            item.get("publish_time"),
        ),
        "title": _optional_text(item.get("title")),
        "summary": _optional_text(item.get("summary")),
        "url": _optional_text(item.get("url")),
        "source": "community",
        "publish_time": _optional_text(item.get("publish_time")),
        "query_symbols": [
            _optional_text(value).upper()
            for value in query_symbols
            if isinstance(value, str) and _optional_text(value)
        ] if isinstance(query_symbols, list) else [],
        "stock_terms": sorted(
            {
                _normalize_stock_term(value)
                for value in stock_terms
                if isinstance(value, str) and _normalize_stock_term(value)
            }
        ) if isinstance(stock_terms, list) else [],
    }


def _merge_stock_feed_cache_items(
    *,
    cached_items: list[dict[str, Any]],
    fetched_items: list[dict[str, Any]],
    run_date: str,
) -> list[dict[str, Any]]:
    cutoff = _stock_feed_cache_cutoff(run_date)
    merged: dict[str, dict[str, Any]] = {}
    for item in [*cached_items, *fetched_items]:
        normalized = _normalize_cached_feed_item(item)
        if not normalized["title"] and not normalized["summary"]:
            continue
        if not _feed_item_is_in_window(normalized, cutoff):
            continue
        key = _optional_text(normalized.get("id")) or _feed_item_fallback_id(
            normalized.get("title"),
            normalized.get("summary"),
            normalized.get("publish_time"),
        )
        if key in merged:
            merged[key]["query_symbols"] = sorted(
                set(merged[key].get("query_symbols", []))
                | set(normalized.get("query_symbols", []))
            )
            merged[key]["stock_terms"] = sorted(
                set(merged[key].get("stock_terms", []))
                | set(normalized.get("stock_terms", []))
            )
            continue
        merged[key] = normalized
    return sorted(
        merged.values(),
        key=lambda item: _feed_item_timestamp(item),
        reverse=True,
    )


def _write_stock_feed_cache(path: Path, *, market: str, items: list[dict[str, Any]]) -> None:
    payload = {
        "schema_version": FUTU_SKILL_FACTS_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "market": market,
        "window_days": FUTU_STOCK_FEED_CACHE_DAYS,
        "items": items,
    }
    _atomic_write_json(path, payload)


def _stock_feed_cache_cutoff(run_date: str) -> datetime:
    start = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return start - timedelta(days=FUTU_STOCK_FEED_CACHE_DAYS - 1)


def _feed_item_is_in_window(item: dict[str, Any], cutoff: datetime) -> bool:
    timestamp = _feed_item_datetime(item)
    return timestamp is None or timestamp >= cutoff


def _feed_item_timestamp(item: dict[str, Any]) -> int:
    timestamp = _feed_item_datetime(item)
    return int(timestamp.timestamp()) if timestamp is not None else 0


def _feed_item_datetime(item: dict[str, Any]) -> datetime | None:
    raw = _optional_text(item.get("publish_time"))
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value > 10**12:
        value //= 1000
    return datetime.fromtimestamp(value, tz=ZoneInfo("Asia/Shanghai"))


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
        evidence.append({"title": title, "summary": title, "url": url, "source": "news"})
    return evidence


def _feed_items_from_payload(
    payload: dict[str, object],
    *,
    query_symbol: str = "",
) -> list[dict[str, Any]]:
    if payload.get("code") not in (0, "0"):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    evidence = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_title = _optional_text(item.get("title"))
        raw_desc = _optional_text(item.get("desc"))
        title = _clean_text(item.get("title"))
        desc = _clean_text(item.get("desc"))
        summary = title if not desc or desc == title else " ".join(
            part for part in (title, desc) if part
        ).strip()
        if not title and not summary:
            continue
        evidence.append(
            {
                "id": _optional_text(item.get("id")) or _feed_item_fallback_id(title, summary, item.get("publish_time")),
                "title": title or summary,
                "summary": summary or title,
                "url": _optional_text(item.get("url")),
                "source": "community",
                "publish_time": _optional_text(item.get("publish_time")),
                "query_symbols": [query_symbol.strip().upper()] if query_symbol.strip() else [],
                "stock_terms": sorted(_stock_terms_from_raw_text(f"{raw_title} {raw_desc}")),
            }
        )
    return evidence


def _public_feed_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": _optional_text(item.get("title")),
        "summary": _optional_text(item.get("summary")),
        "url": _optional_text(item.get("url")),
        "source": "community",
    }


def _relevant_community_evidence(
    feed_items: list[dict[str, str]],
    *,
    symbol: str,
    name: str,
) -> list[dict[str, str]]:
    terms = _community_relevance_terms(symbol=symbol, name=name)
    if not terms:
        return []
    relevant = []
    for item in feed_items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".casefold()
        stock_terms = {
            _normalize_stock_term(term)
            for term in item.get("stock_terms", [])
            if isinstance(term, str)
        }
        if any(term in text for term in terms) or terms.intersection(stock_terms):
            relevant.append(_public_feed_item(item))
    return relevant


def _community_relevance_terms(*, symbol: str, name: str) -> set[str]:
    normalized_symbol = symbol.strip().upper()
    terms = {_normalize_stock_term(normalized_symbol)} if normalized_symbol else set()
    terms.update(RELATED_COMMUNITY_TERMS_BY_SYMBOL.get(normalized_symbol, set()))
    for token in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", name):
        text = _normalize_stock_term(token)
        if len(text) >= 3:
            terms.add(text)
    return {term for term in terms if term and term not in GENERIC_COMMUNITY_TERMS}


def _assign_feed_items_to_sources(
    feed_items: list[dict[str, Any]],
    *,
    sources: list[FutuSkillSource],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    assigned: dict[tuple[str, str], list[dict[str, Any]]] = {
        (source.market.upper(), source.symbol.upper()): [] for source in sources
    }
    seen_by_key: dict[tuple[str, str], set[str]] = {
        key: set() for key in assigned
    }
    for item in feed_items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".casefold()
        stock_terms = {
            _normalize_stock_term(term)
            for term in item.get("stock_terms", [])
            if isinstance(term, str)
        }
        item_id = _optional_text(item.get("id")) or _feed_item_fallback_id(
            item.get("title"),
            item.get("summary"),
            item.get("publish_time"),
        )
        for source in sources:
            key = (source.market.upper(), source.symbol.upper())
            terms = _community_relevance_terms(symbol=source.symbol, name=source.name)
            if any(term in text for term in terms) or terms.intersection(stock_terms):
                if item_id not in seen_by_key[key]:
                    assigned[key].append(item)
                    seen_by_key[key].add(item_id)
    return assigned


def _stock_terms_from_raw_text(raw_text: str) -> set[str]:
    text = html.unescape(raw_text or "")
    terms: set[str] = set()
    for attr in ("stocksymbol", "stockcode", "stockname"):
        for match in re.findall(rf'{attr}=["\']([^"\']+)["\']', text, flags=re.IGNORECASE):
            terms.update(_stock_term_variants(match))
    for name, code in re.findall(r"\$([^$()]+)\s*\(([A-Za-z0-9.]+)\)\$", text):
        terms.update(_stock_term_variants(name))
        terms.update(_stock_term_variants(code))
    return {term for term in terms if term}


def _stock_term_variants(value: object) -> set[str]:
    term = _normalize_stock_term(value)
    if not term:
        return set()
    terms = {term}
    if "." in term:
        terms.add(term.split(".", 1)[0])
    return terms


def _normalize_stock_term(value: object) -> str:
    text = _optional_text(value).casefold()
    text = text.replace("$", "").strip()
    return text


def _feed_item_fallback_id(title: object, summary: object, publish_time: object) -> str:
    return "|".join(
        part
        for part in (
            _optional_text(title),
            _optional_text(summary),
            _optional_text(publish_time),
        )
        if part
    )


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
        "domestic_discussion": _missing_domestic_discussion(),
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
        "domestic_discussion": _missing_domestic_discussion(),
        "blocking_reason": "",
        "suggested_constraint": "review",
    }


def _missing_signal_module(module_name: str, window_days: int) -> dict[str, Any]:
    return {
        "status": "missing",
        "signal": "neutral",
        "confidence": "low",
        "suggested_constraint": "review",
        "window_days": _validate_window_days(window_days),
        "summary": f"{_default_error_category_name(module_name)}暂未接入。",
        "categories": [],
    }


def _error_signal_module(module_name: str, window_days: int, reason: str) -> dict[str, Any]:
    return {
        "status": "error",
        "signal": "neutral",
        "confidence": "low",
        "suggested_constraint": "review",
        "window_days": _validate_window_days(window_days),
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
                "source": _optional_text(item.get("source")),
            }
        )
    return evidence


def _normalize_domestic_discussion(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return _missing_domestic_discussion()
    normalized: dict[str, object] = {
        "status": _required_enum(value, "status", VALID_DOMESTIC_STATUSES, "domestic_discussion"),
        "keyword_counts": _normalize_keyword_counts(value.get("keyword_counts")),
        "summary": _optional_text(value.get("summary")),
        "focus": _optional_text(value.get("focus")),
        "divergence_risk": _optional_text(value.get("divergence_risk")),
        "credibility": _optional_text(value.get("credibility")),
        "trading_constraint": _optional_text(value.get("trading_constraint")),
        "post_count": _optional_int(value.get("post_count")),
        "relevant_post_count": _optional_int(value.get("relevant_post_count")),
    }
    _validate_domestic_discussion(normalized)
    return normalized


def _missing_domestic_discussion() -> dict[str, object]:
    return {
        "status": "missing",
        "keyword_counts": [],
        "summary": "富途社区未找到足够相关讨论。",
        "focus": "缺失",
        "divergence_risk": "缺失",
        "credibility": "缺失",
        "trading_constraint": "富途社区未找到足够相关讨论，不作为交易依据。",
        "post_count": 0,
        "relevant_post_count": 0,
    }


def _validate_domestic_discussion(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("domestic_discussion is invalid")
    _validate_enum(value, "status", VALID_DOMESTIC_STATUSES, "domestic_discussion")
    if not isinstance(value.get("keyword_counts"), list):
        raise ValueError("domestic_discussion keyword_counts is invalid")
    for item in value["keyword_counts"]:
        if not isinstance(item, dict):
            raise ValueError("domestic_discussion keyword_counts item is invalid")
        if not isinstance(item.get("keyword"), str) or not item["keyword"].strip():
            raise ValueError("domestic_discussion keyword_counts keyword is invalid")
        if not isinstance(item.get("count"), int) or item["count"] < 1:
            raise ValueError("domestic_discussion keyword_counts count is invalid")
    for field in (
        "summary",
        "focus",
        "divergence_risk",
        "credibility",
        "trading_constraint",
    ):
        if not isinstance(value.get(field), str):
            raise ValueError(f"domestic_discussion {field} is invalid")
    for field in ("post_count", "relevant_post_count"):
        if not isinstance(value.get(field), int):
            raise ValueError(f"domestic_discussion {field} is invalid")


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


def _required_text(mapping: dict[str, object], field: str, context: str) -> str:
    value = _optional_text(mapping.get(field))
    if not value:
        raise ValueError(f"{context} {field} is invalid")
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


def _optional_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _normalize_keyword_counts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        keyword = _optional_text(item.get("keyword"))
        count = item.get("count")
        if not keyword or not isinstance(count, int) or count < 1:
            continue
        normalized.append({"keyword": keyword[:12], "count": count})
        if len(normalized) >= 3:
            break
    return normalized


def _fallback_domestic_discussion(
    *,
    symbol: str,
    post_count: int,
    relevant_post_count: int,
) -> dict[str, object]:
    if relevant_post_count == 0:
        return {
            "status": "missing",
            "keyword_counts": [],
            "summary": "富途社区未找到足够相关讨论。",
            "focus": "缺失",
            "divergence_risk": "缺失",
            "credibility": "缺失" if post_count == 0 else "噪声高",
            "trading_constraint": "富途社区未找到足够相关讨论，不作为交易依据。",
            "post_count": post_count,
            "relevant_post_count": 0,
        }
    return {
        "status": "ok",
        "keyword_counts": [],
        "summary": f"富途社区相关讨论较少，{post_count} 条 feed 中 {relevant_post_count} 条与 {symbol} 明确相关。",
        "focus": f"少量讨论关注 {symbol} 的短线走势或 ETF 结构问题。",
        "divergence_risk": "社区样本少且噪声高，不能代表稳定共识。",
        "credibility": "噪声高" if relevant_post_count / max(post_count, 1) <= 0.5 else "低",
        "trading_constraint": "仅作为国内讨论温度和 ETF 结构风险提示，不支持单独加仓或减仓。",
        "post_count": post_count,
        "relevant_post_count": relevant_post_count,
    }


def _domestic_discussion_system_prompt() -> str:
    return (
        "你是交易仪表盘的富途社区讨论摘要器。"
        "你只负责把富途 API 返回的新闻标题和社区帖子总结成固定字段，不给买卖建议。"
        "社区帖子优先，新闻标题只作为背景。"
        "必须输出 JSON object，字段为："
        "status, keyword_counts, summary, focus, divergence_risk, credibility, trading_constraint。"
        "字段中文含义：状态、讨论关键词、国内讨论结论、主要关注点、分歧 / 风险、可信度、交易约束。"
        "keyword_counts 必须是最多 3 个对象的数组，每个对象包含 keyword 和 count；"
        "keyword 是当天关于该标的国内讨论的交易可读主题词，count 是每个关键词对应多少条相关社区帖子，按 count 从高到低排序。"
        "关键词要归一成 2 到 6 个汉字的交易主题，例如 看空、震荡、损耗、跟踪偏离、不透明、离场、加仓；"
        "不要直接摘取 亏麻了、坑人、看不懂、快跑 等情绪化口头禅，除非无法归一。"
        "count 统计帖子条数，不统计词频；没有足够相关讨论时 keyword_counts 输出空数组。"
        "credibility 只能使用 高、中、低、噪声高、缺失。"
        "trading_constraint 必须明确这类信息是否能影响交易动作；默认不能单独支持加仓或减仓。"
        "所有字段必须使用简体中文，不能包含 URL，不能复制长篇原帖。"
    )


def _anomaly_module_summary_system_prompt() -> str:
    return (
        "你是交易仪表盘的富途异动信号摘要器。"
        "你只把富途技术、资金、衍生品异动原文压缩成结构化短摘要，不给买卖建议，不生成下单动作。"
        "必须输出 JSON object，字段为 summary 和 categories。"
        "summary 是 1 句中文，总结这个模块的核心异常和交易约束，最长 60 个汉字。"
        "categories 是数组，每项必须包含 name、detail，可选 direction。"
        "name 必须使用输入里的原类别名，不得新增类别，不得改变类别顺序。"
        "detail 每个类别最多 45 个汉字，只保留最重要的金额、方向、波动率、经纪商或时间信息。"
        "direction 如输出，只能是 bullish、bearish、neutral、risk_up、mixed 或空字符串。"
        "不能复制长篇原始流水，不能输出 URL，不能包含英文交易建议。"
    )


def _validate_run_date(value: str) -> str:
    text = value.strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"invalid run_date: {value}") from exc
    return text


def _validate_window_days(window_days: int) -> int:
    try:
        value = int(window_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("window_days must be an integer") from exc
    if value < 1 or value > 30:
        raise ValueError("window_days must be between 1 and 30")
    return value


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
