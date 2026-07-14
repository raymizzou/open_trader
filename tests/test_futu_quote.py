from __future__ import annotations

from decimal import Decimal

import pytest

from open_trader.futu_quote import FutuQuoteClient, FutuQuoteError
from open_trader.futu_watch import QuoteSnapshot
from open_trader.kline_technical_facts import DailyKlineBar


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self._rows


class FakeOpenQuoteContext:
    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.closed = False
        self.requested_symbols: list[str] = []

    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
        self.requested_symbols = symbols
        return (
            0,
            FakeDataFrame(
                [
                    {"code": "US.VIXY", "last_price": 94.5},
                    {"code": "US.QQQ", "last_price": "510.25"},
                ]
            ),
        )

    def request_trading_days(
        self, *, market: object, start: str, end: str
    ) -> tuple[int, object]:
        self.requested_trading_days = {"market": market, "start": start, "end": end}
        return 0, [
            {"time": "2026-07-14", "trade_date_type": "WHOLE"},
            {"time": "", "trade_date_type": "WHOLE"},
        ]

    def request_history_kline(
        self,
        symbol: str,
        *,
        start: str,
        end: str,
        ktype: object,
        autype: object,
        max_count: int,
        page_req_key: object,
    ) -> tuple[int, object, object]:
        self.requested_history = {
            "symbol": symbol,
            "start": start,
            "end": end,
            "ktype": ktype,
            "autype": autype,
            "max_count": max_count,
            "page_req_key": page_req_key,
        }
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "time_key": "2026-06-18 00:00:00",
                        "open": "18.4",
                        "high": "19.0",
                        "low": "18.2",
                        "close": "18.82",
                        "volume": "123456",
                    },
                    {"time_key": "2026-06-19", "close": 19.1, "volume": 654321},
                    {
                        "time_key": "2026-06-20", "open": 20, "high": 21,
                        "low": 20.5, "close": 19, "volume": 100,
                    },
                    {"time_key": "2026-06-19", "close": 19.1, "volume": "NaN"},
                    {"time_key": "2026-06-20", "close": None},
                ]
            ),
            None,
        )

    def get_rehab(self, symbol: str) -> tuple[int, object]:
        self.requested_rehab_symbol = symbol
        return 0, FakeDataFrame([
            {
                "time": "2026-06-20",
                "company_act": "DIVIDEND",
                "ex_dividend": 0.42,
                "forward_adj_factorA": None,
            },
        ])

    def close(self) -> None:
        self.closed = True


class FakeFailingContext(FakeOpenQuoteContext):
    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
        return -1, "OpenD connection failed"


class FakeInterruptedContext(FakeOpenQuoteContext):
    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
        return -1, "网络中断"


class FakePaginatedContext(FakeOpenQuoteContext):
    def request_history_kline(
        self, symbol: str, *, start: str, end: str, ktype: object,
        autype: object, max_count: int, page_req_key: object,
    ) -> tuple[int, object, object]:
        self.page_keys = getattr(self, "page_keys", []) + [page_req_key]
        day = "2026-06-18" if page_req_key is None else "2026-06-19"
        next_key = b"page-2" if page_req_key is None else None
        return 0, FakeDataFrame([{
            "time_key": day, "open": "18", "high": "20", "low": "17",
            "close": "19", "volume": "100",
        }]), next_key


def test_futu_quote_error_preserves_diagnostic_metadata() -> None:
    error = FutuQuoteError(
        "网络中断",
        error_type="quote_server_interrupted",
        next_step="请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。",
        opend_reachable=True,
        context_ok=True,
        snapshot_ok=False,
    )

    assert str(error) == "网络中断"
    assert error.error_type == "quote_server_interrupted"
    assert error.next_step == "请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。"
    assert error.opend_reachable is True
    assert error.context_ok is True
    assert error.snapshot_ok is False


def test_futu_quote_client_classifies_unreachable_opend() -> None:
    with pytest.raises(FutuQuoteError) as exc_info:
        FutuQuoteClient(
            host="127.0.0.1",
            port=11111,
            context_factory=FakeOpenQuoteContext,
            connectivity_checker=lambda host, port: False,
        )

    error = exc_info.value
    assert error.error_type == "opend_unreachable"
    assert error.opend_reachable is False
    assert error.context_ok is False
    assert error.snapshot_ok is False
    assert "请启动或重启 Futu OpenD" in error.next_step


def test_futu_quote_client_classifies_quote_server_interruption() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeInterruptedContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuQuoteError) as exc_info:
        client.get_snapshots(["US.VIXY"])

    error = exc_info.value
    assert str(error) == "网络中断"
    assert error.error_type == "quote_server_interrupted"
    assert error.opend_reachable is True
    assert error.context_ok is True
    assert error.snapshot_ok is False
    assert "qot_logined=True" in error.next_step


def test_futu_quote_client_omits_nonpositive_and_nonfinite_prices() -> None:
    class InvalidPriceContext(FakeOpenQuoteContext):
        def get_market_snapshot(self, symbols: list[str]) -> tuple[int, object]:
            return 0, FakeDataFrame(
                [
                    {"code": "SH.ZERO", "last_price": "0"},
                    {"code": "SH.NEG", "last_price": "-1"},
                    {"code": "SH.NAN", "last_price": "NaN"},
                    {"code": "SH.OK", "last_price": "1.01"},
                ]
            )

    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=InvalidPriceContext,
        connectivity_checker=lambda host, port: True,
    )

    assert client.get_snapshots(["SH.ZERO", "SH.NEG", "SH.NAN", "SH.OK"]) == {
        "SH.OK": QuoteSnapshot(futu_symbol="SH.OK", last_price=Decimal("1.01"))
    }


def test_futu_quote_client_returns_normalized_snapshots() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeOpenQuoteContext,
        connectivity_checker=lambda host, port: True,
    )

    snapshots = client.get_snapshots(["US.VIXY", "US.QQQ"])

    assert snapshots == {
        "US.VIXY": QuoteSnapshot("US.VIXY", Decimal("94.5")),
        "US.QQQ": QuoteSnapshot("US.QQQ", Decimal("510.25")),
    }
    assert client.context.requested_symbols == ["US.VIXY", "US.QQQ"]


def test_futu_quote_client_returns_cn_trading_days() -> None:
    from futu import TradeDateMarket

    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=FakeOpenQuoteContext,
        connectivity_checker=lambda host, port: True,
    )

    assert client.get_cn_trading_days(
        start="2026-07-14", end="2026-07-20"
    ) == ["2026-07-14"]
    assert client.context.requested_trading_days == {
        "market": TradeDateMarket.CN,
        "start": "2026-07-14",
        "end": "2026-07-20",
    }


def test_futu_quote_client_classifies_trading_calendar_failure() -> None:
    class FailingCalendarContext(FakeOpenQuoteContext):
        def request_trading_days(self, **kwargs: object) -> tuple[int, object]:
            return -1, "网络中断"

    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=FailingCalendarContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuQuoteError) as exc_info:
        client.get_cn_trading_days(start="2026-07-14", end="2026-07-14")

    assert exc_info.value.error_type == "quote_server_interrupted"


@pytest.mark.parametrize("data", [None, ["not-a-row"]])
def test_futu_quote_client_rejects_malformed_trading_calendar_data(
    data: object,
) -> None:
    class MalformedCalendarContext(FakeOpenQuoteContext):
        def request_trading_days(self, **kwargs: object) -> tuple[int, object]:
            return 0, data

    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=MalformedCalendarContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuQuoteError) as exc_info:
        client.get_cn_trading_days(start="2026-07-14", end="2026-07-14")

    assert exc_info.value.error_type == "snapshot_failed"
    assert exc_info.value.opend_reachable is True
    assert exc_info.value.context_ok is True
    assert exc_info.value.snapshot_ok is False
    assert "行情服务状态" in exc_info.value.next_step


@pytest.mark.parametrize("time_value", [None, 20260714])
def test_futu_quote_client_rejects_non_string_trading_day(
    time_value: object,
) -> None:
    class MalformedCalendarContext(FakeOpenQuoteContext):
        def request_trading_days(self, **kwargs: object) -> tuple[int, object]:
            return 0, [{"time": time_value}]

    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=MalformedCalendarContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuQuoteError) as exc_info:
        client.get_cn_trading_days(start="2026-07-14", end="2026-07-14")

    assert exc_info.value.error_type == "snapshot_failed"
    assert exc_info.value.opend_reachable is True
    assert exc_info.value.context_ok is True
    assert exc_info.value.snapshot_ok is False
    assert "行情服务状态" in exc_info.value.next_step


@pytest.mark.parametrize("time_value", ["garbage", "2026-02-30"])
def test_futu_quote_client_rejects_invalid_trading_day(time_value: str) -> None:
    class MalformedCalendarContext(FakeOpenQuoteContext):
        def request_trading_days(self, **kwargs: object) -> tuple[int, object]:
            return 0, [{"time": time_value}]

    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=MalformedCalendarContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuQuoteError) as exc_info:
        client.get_cn_trading_days(start="2026-07-14", end="2026-07-14")

    assert exc_info.value.error_type == "snapshot_failed"
    assert exc_info.value.opend_reachable is True
    assert exc_info.value.context_ok is True
    assert exc_info.value.snapshot_ok is False
    assert "行情服务状态" in exc_info.value.next_step


def test_futu_quote_client_returns_normalized_daily_kline() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeOpenQuoteContext,
        connectivity_checker=lambda host, port: True,
    )

    bars = client.get_daily_kline("US.VIXY", start="2026-01-01", end="2026-07-04")

    assert bars == [
        DailyKlineBar(
            date="2026-06-18",
            close=18.82,
            open=18.4,
            high=19.0,
            low=18.2,
            volume=123456.0,
        ),
        DailyKlineBar(date="2026-06-19", close=19.1, volume=654321.0),
    ]
    assert client.context.requested_history["symbol"] == "US.VIXY"
    assert client.context.requested_history["start"] == "2026-01-01"
    assert client.context.requested_history["end"] == "2026-07-04"
    assert client.context.requested_history["max_count"] == 1000


def test_futu_quote_client_requests_qfq_and_exposes_rehab_rows() -> None:
    from futu import AuType

    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeOpenQuoteContext,
        connectivity_checker=lambda host, port: True,
    )

    client.get_daily_kline("US.QQQ", start="2026-01-01", end="2026-07-04")
    rehab = client.get_rehab_rows("US.QQQ")

    assert client.context.requested_history["autype"] == AuType.QFQ
    assert client.context.requested_rehab_symbol == "US.QQQ"
    assert rehab == [{
        "company_act": "DIVIDEND",
        "ex_dividend": "0.42",
        "forward_adj_factorA": "",
        "time": "2026-06-20",
    }]


def test_futu_quote_client_reads_all_history_pages() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=FakePaginatedContext,
        connectivity_checker=lambda host, port: True,
    )

    bars = client.get_daily_kline("US.SPY", start="2021-01-01", end="2026-07-13")

    assert [bar.date for bar in bars] == ["2026-06-18", "2026-06-19"]
    assert client.context.page_keys == [None, b"page-2"]


def test_futu_quote_client_maps_cn_symbol_for_daily_kline() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeOpenQuoteContext,
        connectivity_checker=lambda host, port: True,
    )

    client.get_daily_kline("CN.600025", start="2026-07-01", end="2026-07-14")

    assert client.context.requested_history["symbol"] == "SH.600025"


@pytest.mark.parametrize("symbol", ["SH.000001", "SZ.600025", "SH.BAD"])
def test_futu_quote_client_rejects_invalid_cn_wire_symbol(symbol: str) -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeOpenQuoteContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(ValueError):
        client.get_daily_kline(symbol, start="2026-07-01", end="2026-07-14")

    assert not hasattr(client.context, "requested_history")


def test_futu_quote_client_raises_clear_error_on_sdk_failure() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeFailingContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuQuoteError, match="OpenD connection failed"):
        client.get_snapshots(["US.VIXY"])


def test_futu_quote_client_close_closes_context() -> None:
    client = FutuQuoteClient(
        host="127.0.0.1",
        port=11111,
        context_factory=FakeOpenQuoteContext,
        connectivity_checker=lambda host, port: True,
    )

    client.close()

    assert client.context.closed is True


def test_futu_quote_client_fails_fast_when_opend_port_is_not_reachable() -> None:
    called = False

    def context_factory(*, host: str, port: int) -> FakeOpenQuoteContext:
        nonlocal called
        called = True
        return FakeOpenQuoteContext(host=host, port=port)

    with pytest.raises(FutuQuoteError) as exc_info:
        FutuQuoteClient(
            host="127.0.0.1",
            port=11111,
            context_factory=context_factory,
            connectivity_checker=lambda host, port: False,
        )

    assert called is False
    assert "Futu OpenD is not reachable at 127.0.0.1:11111" in str(exc_info.value)
    assert exc_info.value.error_type == "opend_unreachable"
    assert exc_info.value.opend_reachable is False
