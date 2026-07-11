from __future__ import annotations

import calendar
import csv
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol, Sequence

from .kline_technical_facts import DailyKlineBar
from .market_scope import parse_market_scope
from .standard_strategies import StrategyBar


BACKTEST_PRICE_FIELDNAMES = ("date", "open", "high", "low", "close", "volume")
PRESET_MONTHS = {"6M": 6, "1Y": 12, "3Y": 36, "5Y": 60}


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
class BacktestPriceFetchResult:
    market: str
    symbol: str
    start: str
    end: str
    records: int
    prices_path: Path


@dataclass(frozen=True)
class BacktestDateRange:
    requested_start: date
    requested_end: date
    warmup_start: date


@dataclass(frozen=True)
class BacktestPriceRangeResult:
    market: str
    symbol: str
    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    warmup_start: date
    prices_path: Path
    source_hash: str
    bars: Sequence[StrategyBar]


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def resolve_backtest_range(
    *,
    preset: str | None,
    custom_start: date | None,
    custom_end: date | None,
    latest_available: date,
) -> BacktestDateRange:
    if preset is not None and preset not in PRESET_MONTHS:
        raise ValueError(f"未知回测区间：{preset}")
    end = min(custom_end or latest_available, latest_available)
    start = custom_start or _subtract_months(end, PRESET_MONTHS[preset or "1Y"])
    if start >= end:
        raise ValueError("回测开始日期必须早于结束日期")
    return BacktestDateRange(start, end, start - timedelta(days=100))


def load_price_rows(path: Path) -> list[StrategyBar]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = sorted(set(BACKTEST_PRICE_FIELDNAMES) - set(reader.fieldnames or ()))
            if missing:
                raise ValueError(f"价格文件缺少列：{', '.join(missing)}")
            bars: list[StrategyBar] = []
            seen: set[date] = set()
            previous: date | None = None
            for line_number, row in enumerate(reader, start=2):
                try:
                    bar = StrategyBar(
                        date=date.fromisoformat(row["date"].strip()),
                        open=Decimal(row["open"]), high=Decimal(row["high"]),
                        low=Decimal(row["low"]), close=Decimal(row["close"]),
                        volume=Decimal(row["volume"]),
                    )
                except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
                    raise ValueError(f"价格文件第 {line_number} 行无效") from exc
                values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
                if not all(value.is_finite() for value in values) or bar.volume < 0:
                    raise ValueError(f"价格文件第 {line_number} 行无效")
                if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.low > bar.high:
                    raise ValueError(f"价格文件第 {line_number} 行无效")
                if bar.date in seen:
                    raise ValueError(f"价格文件包含重复日期：{bar.date.isoformat()}")
                if previous is not None and bar.date < previous:
                    raise ValueError("价格文件日期顺序无效")
                seen.add(bar.date)
                previous = bar.date
                bars.append(bar)
    except OSError as exc:
        raise ValueError(f"无法读取价格文件：{path}") from exc
    if not bars:
        raise ValueError("价格文件没有数据行")
    return bars


def ensure_backtest_price_range(
    *, data_dir: Path, market: str, symbol: str,
    date_range: BacktestDateRange, provider: DailyKlineProvider,
) -> BacktestPriceRangeResult:
    market_scope = parse_market_scope(market)
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    prices_path = data_dir / "prices" / market_scope.value / f"{normalized_symbol}.csv"
    bars: list[StrategyBar] | None = None
    if prices_path.exists():
        bars = load_price_rows(prices_path)
        if bars[0].date > date_range.warmup_start or bars[-1].date < date_range.requested_end:
            bars = None
    if bars is None:
        fetched = fetch_backtest_prices(
            data_dir=data_dir, market=market_scope.value, symbol=normalized_symbol,
            start=date_range.warmup_start.isoformat(), end=date_range.requested_end.isoformat(),
            provider=provider,
        )
        prices_path = fetched.prices_path
        bars = load_price_rows(prices_path)
    return BacktestPriceRangeResult(
        market=market_scope.value, symbol=normalized_symbol,
        requested_start=date_range.requested_start, requested_end=date_range.requested_end,
        actual_start=bars[0].date, actual_end=bars[-1].date,
        warmup_start=date_range.warmup_start, prices_path=prices_path,
        source_hash=hashlib.sha256(prices_path.read_bytes()).hexdigest(), bars=tuple(bars),
    )


def fetch_backtest_prices(
    *,
    data_dir: Path,
    market: str,
    symbol: str,
    start: str,
    end: str,
    provider: DailyKlineProvider,
) -> BacktestPriceFetchResult:
    market_scope = parse_market_scope(market)
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    if not start.strip() or not end.strip():
        raise ValueError("start and end are required")

    futu_symbol = f"{market_scope.value}.{normalized_symbol}"
    bars = provider.get_daily_kline(futu_symbol, start=start, end=end)
    rows = [_price_row(bar) for bar in bars]
    if not rows:
        raise ValueError(f"no daily kline rows returned for {futu_symbol}")

    prices_path = data_dir / "prices" / market_scope.value / f"{normalized_symbol}.csv"
    _atomic_write_csv(prices_path, rows)
    return BacktestPriceFetchResult(
        market=market_scope.value,
        symbol=normalized_symbol,
        start=start,
        end=end,
        records=len(rows),
        prices_path=prices_path,
    )


def _price_row(bar: DailyKlineBar) -> dict[str, str]:
    close = bar.close
    return {
        "date": bar.date,
        "open": str(bar.open if bar.open is not None else close),
        "high": str(bar.high if bar.high is not None else close),
        "low": str(bar.low if bar.low is not None else close),
        "close": str(close),
        "volume": str(getattr(bar, "volume", 0)),
    }


def _atomic_write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=BACKTEST_PRICE_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)
