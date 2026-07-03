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
        self.events: list[tuple[str, object]] = []
        self.snapshot_calls: list[list[str]] = []
        self.subscribe_calls: list[tuple[list[str], list[object], bool, bool]] = []
        self.kline_calls: list[tuple[str, int, object]] = []
        self.order_book_calls: list[tuple[str, int]] = []

    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
        self.events.append(("snapshot", tuple(symbols)))
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

    def subscribe(
        self,
        code_list: list[str],
        subtype_list: list[object],
        is_first_push: bool = True,
        subscribe_push: bool = True,
    ) -> tuple[int, object]:
        self.events.append(("subscribe", (tuple(code_list), tuple(subtype_list))))
        self.subscribe_calls.append(
            (code_list, subtype_list, is_first_push, subscribe_push)
        )
        return 0, "ok"

    def get_cur_kline(self, code: str, num: int, ktype: object) -> tuple[int, object]:
        self.events.append(("kline", ktype))
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
        subtype_1m="K_1M",
        subtype_5m="K_5M",
        subtype_order_book="ORDER_BOOK",
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
    assert client.context.subscribe_calls == [
        (["US.VIXY"], ["K_1M", "K_5M", "ORDER_BOOK"], False, False)
    ]
    assert client.context.kline_calls == [
        ("US.VIXY", 30, "K_1M"),
        ("US.VIXY", 30, "K_5M"),
    ]
    assert client.context.events[:4] == [
        ("snapshot", ("US.VIXY",)),
        ("subscribe", (("US.VIXY",), ("K_1M", "K_5M", "ORDER_BOOK"))),
        ("kline", "K_1M"),
        ("kline", "K_5M"),
    ]
    assert client.context.order_book_calls == [("US.VIXY", 1)]


def test_futu_t_signal_client_wraps_subscribe_failure_before_kline() -> None:
    class FailingSubscribeContext(FakeTSignalContext):
        def subscribe(
            self,
            code_list: list[str],
            subtype_list: list[object],
            is_first_push: bool = True,
            subscribe_push: bool = True,
        ) -> tuple[int, object]:
            super().subscribe(code_list, subtype_list, is_first_push, subscribe_push)
            return -1, "subscribe failed"

    client = FutuTSignalMarketDataClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FailingSubscribeContext,
        connectivity_checker=lambda host, port: True,
        kline_type_1m="K_1M",
        kline_type_5m="K_5M",
        subtype_1m="K_1M",
        subtype_5m="K_5M",
        subtype_order_book="ORDER_BOOK",
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

    assert "subscribe failed" in str(exc_info.value)
    assert exc_info.value.error_type == "snapshot_failed"
    assert client.context.kline_calls == []


def test_futu_t_signal_client_derives_change_pct_from_prev_close() -> None:
    class FutuShapedSnapshotContext(FakeTSignalContext):
        def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
            return (
                0,
                FakeDataFrame(
                    [
                        {
                            "code": "US.VIXY",
                            "last_price": "48.50",
                            "prev_close_price": "50.00",
                            "low_price": "48.00",
                            "high_price": "50.20",
                        }
                    ]
                ),
            )

    client = FutuTSignalMarketDataClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FutuShapedSnapshotContext,
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

    assert facts.day_change_pct == Decimal("-3.00")


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


def test_futu_t_signal_client_classifies_quote_server_interruption() -> None:
    class InterruptedContext(FakeTSignalContext):
        def get_order_book(self, code: str, num: int) -> tuple[int, object]:
            return -1, "网络中断"

    client = FutuTSignalMarketDataClient(
        host="127.0.0.1",
        port=11111,
        context_factory=InterruptedContext,
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

    assert exc_info.value.error_type == "quote_server_interrupted"
    assert "qot_logined=True" in exc_info.value.next_step


def test_futu_t_signal_client_ignores_malformed_numeric_values() -> None:
    class MalformedContext(FakeTSignalContext):
        def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
            return (
                0,
                FakeDataFrame(
                    [
                        {
                            "code": "US.VIXY",
                            "last_price": "bad",
                            "change_rate": "NaN",
                            "low_price": "",
                            "high_price": None,
                        }
                    ]
                ),
            )

        def get_cur_kline(self, code: str, num: int, ktype: object) -> tuple[int, object]:
            return 0, FakeDataFrame([{"close": "bad", "volume": "NaN"}])

        def get_order_book(self, code: str, num: int) -> tuple[int, object]:
            return 0, {"Bid": [("bad", "NaN", 1)], "Ask": [(None, "", 1)]}

    client = FutuTSignalMarketDataClient(
        host="127.0.0.1",
        port=11111,
        context_factory=MalformedContext,
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

    assert facts.last_price is None
    assert facts.day_change_pct is None
    assert facts.vwap is None
    assert facts.ma_1m is None
    assert facts.ma_5m is None
    assert facts.bid is None
    assert facts.ask is None
    assert facts.volume_ratio_5m is None


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
