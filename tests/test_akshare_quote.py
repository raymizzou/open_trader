from __future__ import annotations

import pytest

from open_trader.akshare_quote import AkShareDailyKlineProvider
from open_trader.kline_technical_facts import DailyKlineBar


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


def test_akshare_provider_maps_a_share_daily_columns_and_arguments() -> None:
    calls = []

    def history(**kwargs):
        calls.append(kwargs)
        return FakeFrame([{
            "日期": "2026-07-10", "开盘": 9.5, "最高": 9.7, "最低": 9.4,
            "收盘": 9.62, "成交量": 123456,
        }])

    provider = AkShareDailyKlineProvider(stock_history=history, index_history=lambda **_: None)
    bars = provider.get_daily_kline("CN.600025", start="2026-07-01", end="2026-07-10")

    assert calls == [{"symbol": "600025", "period": "daily", "start_date": "20260701", "end_date": "20260710", "adjust": "qfq"}]
    assert bars == [DailyKlineBar(date="2026-07-10", close=9.62, volume=123456, open=9.5, high=9.7, low=9.4)]


def test_akshare_provider_uses_index_endpoint_for_cn_benchmark() -> None:
    calls = []

    def index(**kwargs):
        calls.append(kwargs)
        return FakeFrame([{"date": "2026-07-10", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 3}])

    bars = AkShareDailyKlineProvider(stock_history=lambda **_: None, index_history=index).get_daily_kline(
        "CN.000300", start="2026-07-01", end="2026-07-10"
    )
    assert calls == [{"symbol": "sh000300"}]
    assert bars[0].close == 2


@pytest.mark.parametrize("symbol", ["US.600025", "CN.60025", "CN.6000250", "CN.ABCDEF"])
def test_akshare_provider_rejects_non_cn_six_digit_symbols(symbol: str) -> None:
    with pytest.raises(ValueError, match="CN.六位代码"):
        AkShareDailyKlineProvider(stock_history=lambda **_: None, index_history=lambda **_: None).get_daily_kline(
            symbol, start="2026-07-01", end="2026-07-10"
        )


@pytest.mark.parametrize("field,value", [("收盘", float("nan")), ("开盘", float("inf")), ("成交量", -1), ("最低", -1)])
def test_akshare_provider_rejects_invalid_ohlcv(field: str, value: float) -> None:
    row = {"日期": "2026-07-10", "开盘": 1, "最高": 2, "最低": 1, "收盘": 2, "成交量": 3}
    row[field] = value
    provider = AkShareDailyKlineProvider(stock_history=lambda **_: FakeFrame([row]), index_history=lambda **_: None)
    with pytest.raises(ValueError, match="AKShare 日线数据无效"):
        provider.get_daily_kline("CN.600025", start="2026-07-01", end="2026-07-10")


def test_akshare_provider_filters_inclusively_sorts_and_rejects_duplicates() -> None:
    rows = [
        {"日期": day, "开盘": 1, "最高": 2, "最低": 1, "收盘": 2, "成交量": 3}
        for day in ("2026-07-11", "2026-07-10", "2026-07-01", "2026-06-30")
    ]
    provider = AkShareDailyKlineProvider(stock_history=lambda **_: FakeFrame(rows), index_history=lambda **_: None)
    assert [bar.date for bar in provider.get_daily_kline("CN.600025", start="2026-07-01", end="2026-07-10")] == ["2026-07-01", "2026-07-10"]
    rows.append(dict(rows[2]))
    with pytest.raises(ValueError, match="重复日期"):
        provider.get_daily_kline("CN.600025", start="2026-07-01", end="2026-07-10")
