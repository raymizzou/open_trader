from __future__ import annotations

from pathlib import Path

from open_trader.backtest_prices import fetch_backtest_prices
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
        "date,open,high,low,close",
        "2026-07-09,500.0,505.0,498.0,503.25",
        "2026-07-10,504.0,506.0,501.0,502.5",
    ]
