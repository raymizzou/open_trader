from __future__ import annotations

import calendar
import csv
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import Protocol, Sequence

from .kline_technical_facts import DailyKlineBar
from .market_scope import MarketScope, parse_market_scope
from .standard_strategies import StrategyBar


BACKTEST_PRICE_FIELDNAMES = ("date", "open", "high", "low", "close", "volume")
PRESET_MONTHS = {"6M": 6, "1Y": 12, "3Y": 36, "5Y": 60}
US_EQUITY_SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*")
HK_EQUITY_SYMBOL_PATTERN = re.compile(r"\d{1,5}")
CN_EQUITY_SYMBOL_PATTERN = re.compile(r"\d{6}")


def normalize_backtest_symbol(market: str, symbol: str) -> str:
    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("标的代码不能为空")
    valid = (
        normalized_market == "HK"
        and HK_EQUITY_SYMBOL_PATTERN.fullmatch(normalized_symbol) is not None
    ) or (
        normalized_market == "US"
        and US_EQUITY_SYMBOL_PATTERN.fullmatch(normalized_symbol) is not None
    ) or (
        normalized_market == "CN"
        and CN_EQUITY_SYMBOL_PATTERN.fullmatch(normalized_symbol) is not None
    )
    if not valid:
        raise ValueError("标的代码格式无效，仅支持港股、美股或 A 股正股及 ETF")
    return normalized_symbol.zfill(5) if normalized_market == "HK" else normalized_symbol


def _backtest_prices_path(data_dir: Path, market: str, symbol: str) -> Path:
    base = (data_dir / "prices" / market).resolve()
    target = (base / f"{symbol}.csv").resolve()
    if not target.is_relative_to(base):
        raise ValueError("价格文件路径无效")
    return target


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


@dataclass(frozen=True)
class ResolvedBacktestPriceRangeResult:
    date_range: BacktestDateRange
    price_range: BacktestPriceRangeResult


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


def _price_cache_missing_required_columns(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            fieldnames = next(reader, ())
    except OSError as exc:
        raise ValueError(f"无法读取价格文件：{path}") from exc
    return bool(set(BACKTEST_PRICE_FIELDNAMES) - set(fieldnames))


def ensure_backtest_price_range(
    *, data_dir: Path, market: str, symbol: str,
    date_range: BacktestDateRange, provider: DailyKlineProvider,
) -> BacktestPriceRangeResult:
    market_scope = _parse_market_scope_zh(market)
    normalized_symbol = normalize_backtest_symbol(market_scope.value, symbol)
    prices_path = _backtest_prices_path(data_dir, market_scope.value, normalized_symbol)
    bars: list[StrategyBar] | None = None
    if (
        market_scope is not MarketScope.CN
        and prices_path.exists()
        and not _price_cache_missing_required_columns(prices_path)
    ):
        bars = load_price_rows(prices_path)
        if (
            all(bar.volume == 0 for bar in bars)
            or bars[0].date > date_range.warmup_start
            or bars[-1].date < date_range.requested_end
        ):
            bars = None
    if bars is None:
        fetched = fetch_backtest_prices(
            data_dir=data_dir, market=market_scope.value, symbol=normalized_symbol,
            start=date_range.warmup_start.isoformat(), end=date_range.requested_end.isoformat(),
            provider=provider,
        )
        prices_path = fetched.prices_path
        bars = load_price_rows(prices_path)
    bars = [
        bar for bar in bars
        if date_range.warmup_start <= bar.date <= date_range.requested_end
    ]
    if not bars:
        raise ValueError("请求区间内没有可用价格数据")
    requested_bars = [bar for bar in bars if bar.date >= date_range.requested_start]
    if not requested_bars:
        raise ValueError("请求区间内没有可用价格数据")
    return BacktestPriceRangeResult(
        market=market_scope.value, symbol=normalized_symbol,
        requested_start=date_range.requested_start, requested_end=date_range.requested_end,
        actual_start=requested_bars[0].date, actual_end=requested_bars[-1].date,
        warmup_start=date_range.warmup_start, prices_path=prices_path,
        source_hash=hashlib.sha256(prices_path.read_bytes()).hexdigest(), bars=tuple(bars),
    )


def ensure_resolved_backtest_price_range(
    *,
    data_dir: Path,
    market: str,
    symbol: str,
    preset: str | None,
    custom_start: date | None,
    custom_end: date | None,
    provider: DailyKlineProvider,
) -> ResolvedBacktestPriceRangeResult:
    provisional_end = custom_end or date.today()
    provisional_range = resolve_backtest_range(
        preset=preset, custom_start=custom_start, custom_end=custom_end,
        latest_available=provisional_end,
    )
    initial = ensure_backtest_price_range(
        data_dir=data_dir, market=market, symbol=symbol,
        date_range=provisional_range, provider=provider,
    )
    effective_range = resolve_backtest_range(
        preset=preset, custom_start=custom_start, custom_end=custom_end,
        latest_available=initial.actual_end,
    )
    if custom_end is not None:
        preserved = BacktestPriceRangeResult(
            market=initial.market, symbol=initial.symbol,
            requested_start=initial.requested_start, requested_end=custom_end,
            actual_start=initial.actual_start, actual_end=initial.actual_end,
            warmup_start=initial.warmup_start, prices_path=initial.prices_path,
            source_hash=initial.source_hash, bars=initial.bars,
        )
        return ResolvedBacktestPriceRangeResult(effective_range, preserved)
    if effective_range.warmup_start < initial.bars[0].date:
        initial = ensure_backtest_price_range(
            data_dir=data_dir, market=market, symbol=symbol,
            date_range=effective_range, provider=provider,
        )
    return ResolvedBacktestPriceRangeResult(effective_range, initial)


def fetch_backtest_prices(
    *,
    data_dir: Path,
    market: str,
    symbol: str,
    start: str,
    end: str,
    provider: DailyKlineProvider,
) -> BacktestPriceFetchResult:
    market_scope = _parse_market_scope_zh(market)
    normalized_symbol = normalize_backtest_symbol(market_scope.value, symbol)
    if not start.strip() or not end.strip():
        raise ValueError("开始日期和结束日期不能为空")

    futu_symbol = f"{market_scope.value}.{normalized_symbol}"
    bars = provider.get_daily_kline(futu_symbol, start=start, end=end)
    rows = [_price_row(bar) for bar in bars]
    if not rows:
        raise ValueError(f"{futu_symbol} 在请求日期区间 {start} 至 {end} 没有返回日线数据")

    prices_path = _backtest_prices_path(data_dir, market_scope.value, normalized_symbol)
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
    try:
        date.fromisoformat(bar.date)
        close = Decimal(str(bar.close))
        open_ = Decimal(str(bar.open if bar.open is not None else bar.close))
        high = Decimal(str(bar.high if bar.high is not None else bar.close))
        low = Decimal(str(bar.low if bar.low is not None else bar.close))
        volume = Decimal(str(bar.volume))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("价格必须是有限的非负数") from exc
    if not volume.is_finite() or volume < 0:
        raise ValueError("成交量必须是有限的非负数")
    if not all(value.is_finite() and value >= 0 for value in (open_, high, low, close)):
        raise ValueError("价格必须是有限的非负数")
    if low > min(open_, close) or high < max(open_, close) or low > high:
        raise ValueError("价格高低关系无效")
    return {
        "date": bar.date,
        "open": str(bar.open if bar.open is not None else bar.close),
        "high": str(bar.high if bar.high is not None else bar.close),
        "low": str(bar.low if bar.low is not None else bar.close),
        "close": str(bar.close),
        "volume": str(bar.volume),
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


def _parse_market_scope_zh(market: str) -> MarketScope:
    try:
        return parse_market_scope(market)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"不支持的市场：{market}") from exc
