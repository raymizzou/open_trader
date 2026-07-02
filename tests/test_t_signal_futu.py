from __future__ import annotations

from decimal import Decimal

import pytest

from open_trader.futu_quote import FutuQuoteError
from open_trader.t_signal_futu import FutuTSignalMarketDataClient


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self._rows


class FakeTSignalContext:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.closed = False
        self.snapshot_calls: list[list[str]] = []
        self.kline_calls: list[tuple[str, int, object]] = []
        self.order_book_calls: list[tuple[str, int]] = []

    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
        self.snapshot_calls.append(symbols)
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "code": "US.VIXY",
                        "last_price": "48.50",
                        "change_rate": "-1.20",
                        "low_price": "48.00",
                        "high_price": "50.20",
                    }
                ]
            ),
        )

    def get_cur_kline(self, code: str, num: int, ktype: object) -> tuple[int, object]:
        self.kline_calls.append((code, num, ktype))
        if ktype == "K_1M":
            return (
                0,
                FakeDataFrame(
                    [
                        {"close": "48.35", "volume": "1000", "turnover": "48350"},
                        {"close": "48.50", "volume": "1500", "turnover": "72750"},
                    ]
                ),
            )
        return (
            0,
            FakeDataFrame(
                [
                    {"close": "50.00", "volume": "1000"},
                    {"close": "49.40", "volume": "1000"},
                    {"close": "48.90", "volume": "1000"},
                    {"close": "48.50", "volume": "1800"},
                ]
            ),
        )

    def get_order_book(self, code: str, num: int) -> tuple[int, object]:
        self.order_book_calls.append((code, num))
        return (
            0,
            {
                "Bid": [(48.49, 5000, 1)],
                "Ask": [(48.50, 4700, 1)],
            },
        )

    def close(self) -> None:
        self.closed = True


def test_futu_t_signal_client_builds_market_facts() -> None:
    client = FutuTSignalMarketDataClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeTSignalContext,
        connectivity_checker=lambda host, port: True,
        kline_type_1m="K_1M",
        kline_type_5m="K_5M",
    )

    facts = client.get_market_facts(
        run_date="2026-07-02",
        market="US",
        symbol="VIXY",
        futu_symbol="US.VIXY",
        name="Volatility ETF",
        session_phase="regular",
        updated_at="2026-07-02T22:31:00+08:00",
    )

    assert facts.last_price == Decimal("48.50")
    assert facts.day_change_pct == Decimal("-1.20")
    assert facts.vwap == Decimal("48.440")
    assert facts.ma_1m == Decimal("48.425")
    assert facts.ma_5m == Decimal("49.200")
    assert facts.day_low == Decimal("48.00")
    assert facts.day_high == Decimal("50.20")
    assert facts.bid == Decimal("48.49")
    assert facts.ask == Decimal("48.50")
    assert facts.bid_depth == Decimal("5000")
    assert facts.ask_depth == Decimal("4700")
    assert facts.rsi_5m is not None
    assert facts.volume_ratio_5m == Decimal("1.80")
    assert client.context.snapshot_calls == [["US.VIXY"]]
    assert client.context.kline_calls == [
        ("US.VIXY", 30, "K_1M"),
        ("US.VIXY", 30, "K_5M"),
    ]
    assert client.context.order_book_calls == [("US.VIXY", 1)]


def test_futu_t_signal_client_wraps_kline_failure() -> None:
    class FailingKlineContext(FakeTSignalContext):
        def get_cur_kline(self, code: str, num: int, ktype: object) -> tuple[int, object]:
            return -1, "kline failed"

    client = FutuTSignalMarketDataClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FailingKlineContext,
        connectivity_checker=lambda host, port: True,
        kline_type_1m="K_1M",
        kline_type_5m="K_5M",
    )

    with pytest.raises(FutuQuoteError) as exc_info:
        client.get_market_facts(
            run_date="2026-07-02",
            market="US",
            symbol="VIXY",
            futu_symbol="US.VIXY",
            name="Volatility ETF",
            session_phase="regular",
            updated_at="2026-07-02T22:31:00+08:00",
        )

    assert "kline failed" in str(exc_info.value)
    assert exc_info.value.error_type == "snapshot_failed"


def test_futu_t_signal_client_close_closes_context() -> None:
    client = FutuTSignalMarketDataClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeTSignalContext,
        connectivity_checker=lambda host, port: True,
        kline_type_1m="K_1M",
        kline_type_5m="K_5M",
    )

    client.close()

    assert client.context.closed is True
