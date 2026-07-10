from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from .backtest import PRICE_FIELDNAMES
from .kline_technical_facts import DailyKlineBar
from .market_scope import parse_market_scope


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
        writer = csv.DictWriter(handle, fieldnames=PRICE_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)
