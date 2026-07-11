from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from open_trader.backtest_prices import (
    BacktestDateRange,
    ensure_backtest_price_range,
    fetch_backtest_prices,
    load_price_rows,
    resolve_backtest_range,
)
from open_trader.kline_technical_facts import DailyKlineBar


class FakeDailyKlineProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def get_daily_kline(
        self,
        futu_symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[DailyKlineBar]:
        self.requests.append({"futu_symbol": futu_symbol, "start": start, "end": end})
        return [
            DailyKlineBar(
                date="2026-07-09",
                open=500.0,
                high=505.0,
                low=498.0,
                close=503.25,
            ),
            DailyKlineBar(
                date="2026-07-10",
                open=504.0,
                high=506.0,
                low=501.0,
                close=502.5,
            ),
        ]


def test_fetch_backtest_prices_writes_market_symbol_price_csv(tmp_path: Path) -> None:
    provider = FakeDailyKlineProvider()

    result = fetch_backtest_prices(
        data_dir=tmp_path / "data",
        market="US",
        symbol="MSFT",
        start="2026-07-09",
        end="2026-07-10",
        provider=provider,
    )

    assert result.market == "US"
    assert result.symbol == "MSFT"
    assert result.records == 2
    assert result.prices_path == tmp_path / "data" / "prices" / "US" / "MSFT.csv"
    assert provider.requests == [
        {
            "futu_symbol": "US.MSFT",
            "start": "2026-07-09",
            "end": "2026-07-10",
        }
    ]
    assert result.prices_path.read_text(encoding="utf-8").splitlines() == [
        "date,open,high,low,close,volume",
        "2026-07-09,500.0,505.0,498.0,503.25,0",
        "2026-07-10,504.0,506.0,501.0,502.5,0",
    ]


def _write_prices(path: Path, rows: list[str]) -> Path:
    path.write_text("date,open,high,low,close,volume\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_resolve_three_year_range_ends_on_latest_available_date() -> None:
    result = resolve_backtest_range(
        preset="3Y", custom_start=None, custom_end=None, latest_available=date(2026, 7, 10)
    )
    assert result.requested_start == date(2023, 7, 10)
    assert result.requested_end == date(2026, 7, 10)
    assert result.warmup_start == date(2023, 4, 1)


def test_resolve_range_is_calendar_safe_and_clamps_custom_end() -> None:
    result = resolve_backtest_range(
        preset="1Y", custom_start=None, custom_end=date(2026, 3, 1), latest_available=date(2025, 2, 28)
    )
    assert result.requested_start == date(2024, 2, 28)
    assert result.requested_end == date(2025, 2, 28)


def test_load_price_rows_reports_actual_available_range(tmp_path: Path) -> None:
    path = _write_prices(tmp_path / "prices.csv", [
        "2024-01-02,1,2,0.5,1.5,100", "2026-07-10,2,3,1,2.5,200"
    ])
    rows = load_price_rows(path)
    assert rows[0].date == date(2024, 1, 2)
    assert rows[-1].date == date(2026, 7, 10)


@pytest.mark.parametrize(
    "content,message",
    [
        ("date,open,high,low,close\n2026-01-01,1,1,1,1\n", "缺少列"),
        ("date,open,high,low,close,volume\n2026-01-01,1,1,1,1,1\n2026-01-01,1,1,1,1,1\n", "重复"),
        ("date,open,high,low,close,volume\n2026-01-02,1,1,1,1,1\n2026-01-01,1,1,1,1,1\n", "顺序"),
        ("date,open,high,low,close,volume\n2026-01-01,x,1,1,1,1\n", "无效"),
    ],
)
def test_load_price_rows_rejects_invalid_csv(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_price_rows(path)


class CoverageProvider:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []

    def get_daily_kline(self, futu_symbol: str, *, start: str, end: str) -> list[DailyKlineBar]:
        self.requests.append((futu_symbol, start, end))
        return [DailyKlineBar(date=start, close=100), DailyKlineBar(date=end, close=101)]


def test_ensure_price_range_fetches_when_file_does_not_cover_warmup(tmp_path: Path) -> None:
    provider = CoverageProvider()
    result = ensure_backtest_price_range(
        data_dir=tmp_path, market="US", symbol="MSFT",
        date_range=BacktestDateRange(date(2025, 1, 1), date(2026, 1, 1), date(2024, 9, 23)),
        provider=provider,
    )
    assert provider.requests == [("US.MSFT", "2024-09-23", "2026-01-01")]
    assert result.actual_start <= date(2025, 1, 1)
    assert result.actual_end == date(2026, 1, 1)
    assert len(result.source_hash) == 64


def test_ensure_price_range_reuses_complete_csv(tmp_path: Path) -> None:
    path = tmp_path / "prices" / "US" / "MSFT.csv"
    path.parent.mkdir(parents=True)
    _write_prices(path, ["2024-09-23,1,1,1,1,1", "2026-01-01,2,2,2,2,2"])
    provider = CoverageProvider()
    result = ensure_backtest_price_range(
        data_dir=tmp_path, market="us", symbol="msft",
        date_range=BacktestDateRange(date(2025, 1, 1), date(2026, 1, 1), date(2024, 9, 23)),
        provider=provider,
    )
    assert provider.requests == []
    assert result.prices_path == path
