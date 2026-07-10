from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol
from zoneinfo import ZoneInfo

from .market_scope import parse_market_scope
from .technical_facts import (
    FACTS_SCHEMA_VERSION,
    TECHNICAL_FACTS_SCHEMA_VERSION,
    _classify_bollinger_position,
    _format_bollinger_distance,
    build_freshness,
    technical_facts_latest_path,
    technical_facts_run_path,
)


MIN_BOLLINGER_POINTS = 20


@dataclass(frozen=True)
class DailyKlineBar:
    date: str
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None


class DailyKlineProvider(Protocol):
    def get_daily_kline(
        self,
        futu_symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[DailyKlineBar]:
        ...


@dataclass(frozen=True)
class KlineTechnicalFactsResult:
    run_date: str
    market: str
    records: int
    extracted: int
    failed: int
    run_path: Path
    latest_path: Path


def generate_kline_technical_facts(
    *,
    portfolio_path: Path,
    data_dir: Path,
    run_date: str,
    market: str,
    provider: DailyKlineProvider,
    update_latest: bool,
    lookback_days: int = 220,
) -> KlineTechnicalFactsResult:
    market_scope = parse_market_scope(market)
    start = _lookback_start(run_date, lookback_days)
    symbols = _eligible_portfolio_symbols(portfolio_path, market_scope.value)
    rows: list[dict[str, object]] = []
    extracted = 0
    failed = 0
    for symbol in symbols:
        futu_symbol = f"{market_scope.value}.{symbol}"
        try:
            bars = provider.get_daily_kline(futu_symbol, start=start, end=run_date)
            record = _record_from_daily_kline(
                market=market_scope.value,
                symbol=symbol,
                futu_symbol=futu_symbol,
                run_date=run_date,
                bars=bars,
            )
        except Exception as exc:
            record = _failed_record(
                market=market_scope.value,
                symbol=symbol,
                futu_symbol=futu_symbol,
                run_date=run_date,
                error=str(exc) or exc.__class__.__name__,
            )
        rows.append(record)
        if record.get("extraction_status") == "ok":
            extracted += 1
        else:
            failed += 1

    payload = {
        "schema_version": TECHNICAL_FACTS_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "run_date": run_date,
        "market": market_scope.value,
        "records": rows,
    }
    run_path = technical_facts_run_path(data_dir, run_date, market_scope)
    latest_path = technical_facts_latest_path(data_dir, market_scope)
    _atomic_write_json(run_path, payload)
    if update_latest:
        _atomic_write_json(latest_path, payload)
    return KlineTechnicalFactsResult(
        run_date=run_date,
        market=market_scope.value,
        records=len(rows),
        extracted=extracted,
        failed=failed,
        run_path=run_path,
        latest_path=latest_path,
    )


def _eligible_portfolio_symbols(portfolio_path: Path, market: str) -> list[str]:
    csv.field_size_limit(sys.maxsize)
    symbols: list[str] = []
    with portfolio_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("market") or "").strip().upper() != market:
                continue
            if str(row.get("ai_eligible") or "").strip().lower() != "true":
                continue
            asset_class = str(row.get("asset_class") or "").strip().lower()
            if asset_class not in {"stock", "etf"}:
                continue
            symbol = (
                str(row.get("analysis_symbol") or row.get("symbol") or "")
                .strip()
                .upper()
            )
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    return symbols


def _record_from_daily_kline(
    *,
    market: str,
    symbol: str,
    futu_symbol: str,
    run_date: str,
    bars: list[DailyKlineBar],
) -> dict[str, object]:
    valid_bars = [bar for bar in bars if math.isfinite(bar.close)]
    if len(valid_bars) < MIN_BOLLINGER_POINTS:
        return _failed_record(
            market=market,
            symbol=symbol,
            futu_symbol=futu_symbol,
            run_date=run_date,
            error="日线不足 20 根，无法计算布林带",
            bars=valid_bars,
        )
    window = valid_bars[-MIN_BOLLINGER_POINTS:]
    closes = [bar.close for bar in window]
    current = closes[-1]
    middle = sum(closes) / len(closes)
    stddev = math.sqrt(sum((close - middle) ** 2 for close in closes) / len(closes))
    upper = middle + 2 * stddev
    lower = middle - 2 * stddev
    state = _classify_bollinger_position(current=current, upper=upper, lower=lower)
    reference_band = state.get("reference_band", "")
    reference = upper if reference_band == "upper" else lower if reference_band == "lower" else None
    market_data_as_of = window[-1].date
    source_hash = _kline_source_hash(
        futu_symbol=futu_symbol,
        market_data_as_of=market_data_as_of,
        closes=closes,
    )
    facts = {
        "schema_version": FACTS_SCHEMA_VERSION,
        "status": "present",
        "source_date": run_date,
        "market_data_as_of": market_data_as_of,
        "symbol": futu_symbol,
        "timeframes": [
            {
                "timeframe": "daily",
                "timeframe_label": "日线",
                "current_price": _format_price(current),
                "bollinger": {
                    "upper": _format_price(upper),
                    "middle": _format_price(middle),
                    "lower": _format_price(lower),
                    "position": state["position"],
                    "status": state["status"],
                    "reference_band": reference_band,
                    "distance_pct": _format_bollinger_distance(
                        current=current,
                        reference=reference,
                        reference_band=reference_band,
                    ),
                    "summary_zh": state["summary_zh"],
                    "detail_zh": state["detail_zh"],
                },
            }
        ],
    }
    return {
        "run_date": run_date,
        "market": market,
        "symbol": symbol,
        "source_status": "ok",
        "source_type": "futu_kline",
        "source_hash": source_hash,
        "source_advice_hash": source_hash,
        "extraction_status": "ok",
        "error": "",
        "facts": facts,
        "freshness": build_freshness(
            market_data_as_of=market_data_as_of,
            run_date=run_date,
            has_unknown_timeframe=False,
        ),
    }


def _failed_record(
    *,
    market: str,
    symbol: str,
    futu_symbol: str,
    run_date: str,
    error: str,
    bars: list[DailyKlineBar] | None = None,
) -> dict[str, object]:
    market_data_as_of = bars[-1].date if bars else ""
    source_hash = _kline_source_hash(
        futu_symbol=futu_symbol,
        market_data_as_of=market_data_as_of,
        closes=[bar.close for bar in bars or []],
    )
    return {
        "run_date": run_date,
        "market": market,
        "symbol": symbol,
        "source_status": "error",
        "source_type": "futu_kline",
        "source_hash": source_hash,
        "source_advice_hash": source_hash,
        "extraction_status": "extraction_failed",
        "error": error,
        "facts": {
            "schema_version": FACTS_SCHEMA_VERSION,
            "status": "missing",
            "source_date": run_date,
            "market_data_as_of": market_data_as_of,
            "symbol": futu_symbol,
            "timeframes": [],
        },
        "freshness": build_freshness(
            market_data_as_of=market_data_as_of,
            run_date=run_date,
            has_unknown_timeframe=True,
        ),
    }


def _kline_source_hash(
    *,
    futu_symbol: str,
    market_data_as_of: str,
    closes: list[float],
) -> str:
    payload = json.dumps(
        {
            "source": "futu_kline",
            "symbol": futu_symbol,
            "market_data_as_of": market_data_as_of,
            "closes": [_format_price(close) for close in closes],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "futu-kline:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lookback_start(run_date: str, lookback_days: int) -> str:
    day = datetime.strptime(run_date, "%Y-%m-%d").date()
    return (day - timedelta(days=lookback_days)).isoformat()


def _format_price(value: float) -> str:
    return f"{value:.2f}"


def _now_text() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)
