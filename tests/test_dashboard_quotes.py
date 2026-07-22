from __future__ import annotations

import csv
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.dashboard import DashboardConfig
from open_trader.dashboard_quotes import DashboardQuoteService
from open_trader.futu_quote import DashboardQuoteSnapshot, FutuQuoteError
from open_trader.portfolio import PORTFOLIO_FIELDNAMES


class FakeQuoteClient:
    def __init__(
        self,
        snapshots: dict[str, DashboardQuoteSnapshot],
        states: dict[str, str] | None = None,
        state_error: FutuQuoteError | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.states = states or {}
        self.state_error = state_error
        self.requested_symbols: list[str] = []
        self.requested_batches: list[list[str]] = []
        self.requested_state_symbols: list[str] = []
        self.closed = False

    def get_dashboard_snapshots(
        self, futu_symbols: Sequence[str]
    ) -> dict[str, DashboardQuoteSnapshot]:
        symbols = list(futu_symbols)
        self.requested_batches.append(symbols)
        self.requested_symbols.extend(symbols)
        return self.snapshots

    def get_market_states(self, futu_symbols: Sequence[str]) -> dict[str, str]:
        self.requested_state_symbols = list(futu_symbols)
        if self.state_error is not None:
            raise self.state_error
        return self.states

    def close(self) -> None:
        self.closed = True


class RaisingQuoteClient:
    def __init__(self) -> None:
        self.closed = False

    def get_dashboard_snapshots(
        self, futu_symbols: Sequence[str]
    ) -> dict[str, DashboardQuoteSnapshot]:
        raise FutuQuoteError(
            "网络中断",
            error_type="quote_server_interrupted",
            next_step="请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。",
            opend_reachable=True,
            context_ok=True,
            snapshot_ok=False,
        )

    def close(self) -> None:
        self.closed = True


def write_portfolio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(
            [
                {
                    "sort_group": "4",
                    "market": "US",
                    "asset_class": "stock",
                    "symbol": "MSFT",
                    "name": "Microsoft",
                    "currency": "USD",
                    "total_quantity": "3",
                    "avg_cost_price": "420",
                    "last_price": "500",
                    "market_value": "1500",
                    "cost_value": "1260",
                    "unrealized_pnl": "240",
                    "unrealized_pnl_pct": "19.05%",
                    "fx_source": "fixture",
                    "fx_date": "2026-05-31",
                    "fx_to_hkd": "7.8",
                    "market_value_hkd": "11700",
                    "cost_value_hkd": "9828",
                    "portfolio_weight_hkd": "60.00%",
                    "brokers": "futu",
                    "accounts": "main",
                    "ai_eligible": "true",
                    "analysis_symbol": "MSFT",
                    "risk_flag": "normal",
                    "confidence": "high",
                    "notes": "",
                },
                {
                    "sort_group": "4",
                    "market": "US",
                    "asset_class": "stock",
                    "symbol": "AAPL",
                    "name": "Apple",
                    "currency": "USD",
                    "total_quantity": "2",
                    "avg_cost_price": "150",
                    "last_price": "160",
                    "market_value": "320",
                    "cost_value": "300",
                    "unrealized_pnl": "20",
                    "unrealized_pnl_pct": "6.67%",
                    "fx_source": "fixture",
                    "fx_date": "2026-05-31",
                    "fx_to_hkd": "7.8",
                    "market_value_hkd": "2496",
                    "cost_value_hkd": "2340",
                    "portfolio_weight_hkd": "12.80%",
                    "brokers": "futu",
                    "accounts": "main",
                    "ai_eligible": "true",
                    "analysis_symbol": "AAPL",
                    "risk_flag": "normal",
                    "confidence": "high",
                    "notes": "",
                },
                {
                    "sort_group": "6",
                    "market": "CASH",
                    "asset_class": "cash",
                    "symbol": "HKD_CASH",
                    "name": "HKD Cash",
                    "currency": "HKD",
                    "total_quantity": "1",
                    "avg_cost_price": "",
                    "last_price": "",
                    "market_value": "1000",
                    "cost_value": "",
                    "unrealized_pnl": "",
                    "unrealized_pnl_pct": "",
                    "fx_source": "fixture",
                    "fx_date": "2026-05-31",
                    "fx_to_hkd": "1",
                    "market_value_hkd": "1000",
                    "cost_value_hkd": "",
                    "portfolio_weight_hkd": "5.13%",
                    "brokers": "futu",
                    "accounts": "main",
                    "ai_eligible": "false",
                    "analysis_symbol": "",
                    "risk_flag": "normal",
                    "confidence": "high",
                    "notes": "",
                },
            ]
        )


def dashboard_config(tmp_path: Path) -> DashboardConfig:
    return DashboardConfig(
        portfolio_path=tmp_path / "data" / "latest" / "portfolio.csv",
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        poll_seconds=1.5,
        futu_host="127.0.0.1",
        futu_port=11111,
    )


def session_snapshot(**prices: str | None) -> DashboardQuoteSnapshot:
    return DashboardQuoteSnapshot(
        futu_symbol="US.MSFT",
        last_price=Decimal(prices["last"]) if prices.get("last") else None,
        pre_price=Decimal(prices["pre"]) if prices.get("pre") else None,
        after_price=Decimal(prices["after"]) if prices.get("after") else None,
        overnight_price=Decimal(prices["overnight"]) if prices.get("overnight") else None,
        update_time="2026-07-15 03:03:01.150",
    )


@pytest.mark.parametrize(
    ("state", "expected_price", "expected_session"),
    [
        ("OVERNIGHT", "61.5", "overnight"),
        ("PRE_MARKET_BEGIN", "60.73", "pre_market"),
        ("MORNING", "61.23", "regular"),
        ("AFTER_HOURS_BEGIN", "62.22", "after_hours"),
    ],
)
def test_quote_service_selects_active_us_session_price(
    tmp_path: Path, state: str, expected_price: str, expected_session: str
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(
        last="61.23", pre="60.73", after="62.22", overnight="61.50"
    )
    client = FakeQuoteClient(
        {"US.MSFT": snapshot, "US.AAPL": snapshot},
        {"US.MSFT": state, "US.AAPL": state},
    )

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh()
    quote = result.quotes["US.MSFT"]

    assert quote["last_price"] == expected_price
    assert quote["price_session"] == expected_session
    assert quote["price_time"] == "2026-07-15 03:03:01.150"
    assert quote["current_session_quote"] is True
    assert result.fallback_count == 0


def test_quote_service_labels_active_session_fallback_without_fake_time(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(
        last="61.23", pre=None, after="62.22", overnight=None
    )
    client = FakeQuoteClient(
        {"US.MSFT": snapshot, "US.AAPL": snapshot},
        {"US.MSFT": "OVERNIGHT", "US.AAPL": "OVERNIGHT"},
    )

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh()
    quote = result.quotes["US.MSFT"]

    assert result.status == "partial"
    assert result.fallback_count == 2
    assert quote["last_price"] == "62.22"
    assert quote["price_session"] == "after_hours"
    assert quote["price_time"] == ""
    assert quote["current_session_quote"] is False
    assert "当前时段无报价" in result.diagnostic["message"]


def test_quote_service_treats_closed_fallback_as_normal(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(
        last="61.23", pre="60.73", after="62.22", overnight="61.50"
    )
    client = FakeQuoteClient(
        {"US.MSFT": snapshot, "US.AAPL": snapshot},
        {"US.MSFT": "CLOSED", "US.AAPL": "CLOSED"},
    )

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh()

    assert result.status == "ok"
    assert result.fallback_count == 0
    assert result.us_session_status == "closed"
    assert result.quotes["US.MSFT"]["price_session"] == "after_hours"
    assert result.quotes["US.MSFT"]["current_session_quote"] is False


def test_quote_service_degrades_to_regular_price_when_market_state_fails(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(
        last="61.23", pre="60.73", after="62.22", overnight="61.50"
    )
    error = FutuQuoteError(
        "state failed", error_type="market_state_failed", snapshot_ok=True
    )
    client = FakeQuoteClient(
        {"US.MSFT": snapshot, "US.AAPL": snapshot}, state_error=error
    )

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh()

    assert result.status == "partial"
    assert result.us_session_status == "unknown"
    assert result.quotes["US.MSFT"]["last_price"] == "61.23"
    assert result.quotes["US.MSFT"]["price_session"] == ""
    assert result.quotes["US.MSFT"]["current_session_quote"] is False
    assert result.diagnostic["error_type"] == "market_state_failed"


def test_quote_service_degrades_when_any_us_market_state_is_missing(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(
        last="61.23", pre="60.73", after="62.22", overnight="61.50"
    )
    client = FakeQuoteClient(
        {"US.MSFT": snapshot, "US.AAPL": snapshot},
        {"US.MSFT": "OVERNIGHT"},
    )

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh()

    assert result.status == "partial"
    assert result.us_session_status == "unknown"
    assert result.quotes["US.MSFT"]["last_price"] == "61.23"
    assert result.quotes["US.MSFT"]["price_session"] == ""
    assert "市场状态不可用" in result.diagnostic["message"]


def test_quote_service_reuses_last_good_us_sessions_when_market_state_refresh_fails(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    first_client = FakeQuoteClient(
        {
            "US.MSFT": session_snapshot(last="500"),
            "US.AAPL": session_snapshot(last="160"),
        },
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: first_client)
    first_result = service.refresh().to_dict()
    state_error = FutuQuoteError(
        "state failed", error_type="market_state_failed", snapshot_ok=True
    )
    second_client = FakeQuoteClient(
        {
            "US.MSFT": session_snapshot(last="510.25"),
            "US.AAPL": session_snapshot(last="165"),
        },
        state_error=state_error,
    )
    service.client_factory = lambda: second_client

    result = service.refresh().to_dict()

    assert result["status"] == "partial"
    assert result["stale"] is True
    assert result["us_session_status"] == "active"
    assert result["last_success_at"] == first_result["last_success_at"]
    assert result["quotes"]["US.MSFT"] == {
        **first_result["quotes"]["US.MSFT"],
        "stale": True,
    }
    assert "上一笔有效分时段行情" in result["diagnostic"]["message"]


def test_quote_service_returns_ok_and_never_writes_portfolio(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    original_portfolio = config.portfolio_path.read_text(encoding="utf-8")
    client = FakeQuoteClient(
        {
            "US.MSFT": session_snapshot(last="500"),
            "US.AAPL": session_snapshot(last="160"),
        },
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: client)

    result = service.refresh().to_dict()

    assert client.requested_symbols == ["US.AAPL", "US.MSFT"]
    assert result["status"] == "ok"
    assert result["requested_count"] == 2
    assert result["quote_count"] == 2
    assert result["missing_count"] == 0
    assert result["stale"] is False
    assert list(result["quotes"]) == ["US.AAPL", "US.MSFT"]
    assert result["quotes"]["US.AAPL"]["last_price"] == "160"
    assert result["quotes"]["US.MSFT"]["last_price"] == "500"
    assert all(quote["status"] == "ok" for quote in result["quotes"].values())
    assert all(quote["stale"] is False for quote in result["quotes"].values())
    assert result["last_success_at"]
    assert client.closed is True
    assert config.portfolio_path.read_text(encoding="utf-8") == original_portfolio


def test_quote_service_requests_cn_holding_with_futu_exchange_prefix(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    with config.portfolio_path.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES).writerow(
            {
                "market": "CN",
                "asset_class": "stock",
                "symbol": "600025",
                "name": "华能水电",
                "total_quantity": "6000",
            }
        )
    client = FakeQuoteClient(
        {
            "US.MSFT": session_snapshot(last="500"),
            "US.AAPL": session_snapshot(last="160"),
            "SH.600025": session_snapshot(last="9.81"),
        },
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )

    result = DashboardQuoteService(
        config=config,
        client_factory=lambda: client,
    ).refresh()

    assert client.requested_symbols == ["SH.600025", "US.AAPL", "US.MSFT"]
    assert client.requested_batches == [["SH.600025"], ["US.AAPL", "US.MSFT"]]
    assert result.quotes["SH.600025"]["last_price"] == "9.81"


def test_quote_service_keeps_us_quotes_when_cn_snapshot_batch_fails(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    with config.portfolio_path.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES).writerow(
            {
                "market": "CN",
                "asset_class": "stock",
                "symbol": "600900",
                "name": "长江电力",
                "total_quantity": "100",
            }
        )
    full_client = FakeQuoteClient(
        {
            "SH.600900": session_snapshot(last="30"),
            "US.AAPL": session_snapshot(last="160"),
            "US.MSFT": session_snapshot(last="500"),
        },
        {"US.AAPL": "MORNING", "US.MSFT": "MORNING"},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: full_client)
    full_result = service.refresh().to_dict()
    cached_quotes = {symbol: dict(quote) for symbol, quote in service.last_quotes.items()}

    class CnPermissionErrorClient:
        def __init__(self) -> None:
            self.snapshot_requests: list[list[str]] = []
            self.state_requests: list[list[str]] = []
            self.close_count = 0

        def get_dashboard_snapshots(
            self, futu_symbols: Sequence[str]
        ) -> dict[str, DashboardQuoteSnapshot]:
            symbols = list(futu_symbols)
            self.snapshot_requests.append(symbols)
            if any(symbol.startswith(("SH.", "SZ.", "BJ.")) for symbol in symbols):
                raise FutuQuoteError(
                    "无权限获取SH.600900的行情，请检查A股市场股票行情权限",
                    error_type="snapshot_failed",
                    opend_reachable=True,
                    context_ok=True,
                    snapshot_ok=False,
                )
            return {
                "US.AAPL": session_snapshot(last="165"),
                "US.MSFT": session_snapshot(last="510"),
            }

        def get_market_states(self, futu_symbols: Sequence[str]) -> dict[str, str]:
            self.state_requests.append(list(futu_symbols))
            return {symbol: "MORNING" for symbol in futu_symbols}

        def close(self) -> None:
            self.close_count += 1

    client = CnPermissionErrorClient()
    created_clients: list[CnPermissionErrorClient] = []

    def client_factory() -> CnPermissionErrorClient:
        created_clients.append(client)
        return client

    service.client_factory = client_factory

    result = service.refresh().to_dict()

    assert len(created_clients) == 1
    assert client.snapshot_requests == [
        ["SH.600900"],
        ["US.AAPL", "US.MSFT"],
    ]
    assert client.state_requests == [["US.AAPL", "US.MSFT"]]
    assert client.close_count == 1
    assert result["status"] == "partial"
    assert result["quote_count"] == 2
    assert result["missing_count"] == 1
    assert result["quotes"]["SH.600900"]["status"] == "missing_quote"
    assert result["quotes"]["US.AAPL"]["last_price"] == "165"
    assert result["quotes"]["US.MSFT"]["last_price"] == "510"
    assert result["us_session_status"] == "active"
    assert result["diagnostic"]["market"] == "CN"
    assert "无权限获取SH.600900的行情" in result["diagnostic"]["message"]
    assert result["last_success_at"] == full_result["last_success_at"]
    assert service.last_quotes == cached_quotes


def test_quote_service_returns_partial_for_missing_quotes(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    client = FakeQuoteClient(
        {"US.MSFT": session_snapshot(last="510.25")},
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: client)

    result = service.refresh().to_dict()

    assert result["status"] == "partial"
    assert result["requested_count"] == 2
    assert result["quote_count"] == 1
    assert result["missing_count"] == 1
    assert result["stale"] is False
    assert result["quotes"]["US.MSFT"]["last_price"] == "510.25"
    assert result["quotes"]["US.MSFT"]["status"] == "ok"
    assert result["quotes"]["US.AAPL"]["status"] == "missing_quote"
    assert result["quotes"]["US.AAPL"]["last_price"] == ""
    assert result["diagnostic"]["error_type"] == "missing_quotes"


def test_quote_service_returns_failed_and_keeps_last_success(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    first_client = FakeQuoteClient(
        {
            "US.MSFT": session_snapshot(last="500"),
            "US.AAPL": session_snapshot(last="160"),
        },
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: first_client)
    first_result = service.refresh().to_dict()

    def raise_futu_error() -> FakeQuoteClient:
        raise FutuQuoteError(
            "网络中断",
            error_type="quote_server_interrupted",
            next_step="请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。",
            opend_reachable=True,
            context_ok=True,
            snapshot_ok=False,
        )

    service.client_factory = raise_futu_error
    failed_result = service.refresh().to_dict()

    assert failed_result["status"] == "failed"
    assert failed_result["stale"] is True
    assert failed_result["last_success_at"] == first_result["last_success_at"]
    assert failed_result["diagnostic"]["error_type"] == "quote_server_interrupted"
    assert failed_result["diagnostic"]["message"] == "网络中断"
    assert failed_result["diagnostic"]["opend_reachable"] is True
    assert failed_result["diagnostic"]["context_ok"] is True
    assert failed_result["diagnostic"]["snapshot_ok"] is False
    assert failed_result["quotes"]
    assert all(quote["stale"] is True for quote in failed_result["quotes"].values())
    assert {quote["last_price"] for quote in failed_result["quotes"].values()} == {
        "160",
        "500",
    }
    assert {
        quote["fetched_at"] for quote in failed_result["quotes"].values()
    } == {first_result["fetched_at"]}
    assert failed_result["fetched_at"] >= first_result["fetched_at"]


def test_partial_refresh_does_not_replace_complete_success_cache(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    full_client = FakeQuoteClient(
        {
            "US.MSFT": session_snapshot(last="500"),
            "US.AAPL": session_snapshot(last="160"),
        },
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: full_client)
    first_result = service.refresh().to_dict()

    partial_client = FakeQuoteClient(
        {"US.MSFT": session_snapshot(last="510.25")},
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )
    service.client_factory = lambda: partial_client
    partial_result = service.refresh().to_dict()
    assert partial_result["status"] == "partial"
    assert partial_result["quotes"]["US.AAPL"]["last_price"] == ""
    assert partial_result["last_success_at"] == first_result["last_success_at"]

    def raise_futu_error() -> FakeQuoteClient:
        raise FutuQuoteError(
            "网络中断",
            error_type="quote_server_interrupted",
            next_step="请重启 OpenD，确认 qot_logined=True 后重新运行每日盘前流程。",
            opend_reachable=True,
            context_ok=True,
            snapshot_ok=False,
        )

    service.client_factory = raise_futu_error
    failed_result = service.refresh().to_dict()

    assert failed_result["status"] == "failed"
    assert failed_result["last_success_at"] == first_result["last_success_at"]
    assert failed_result["quotes"]["US.AAPL"]["last_price"] == "160"
    assert failed_result["quotes"]["US.AAPL"]["stale"] is True


def test_quote_service_closes_client_when_snapshot_call_fails(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    client = RaisingQuoteClient()
    service = DashboardQuoteService(config=config, client_factory=lambda: client)

    result = service.refresh().to_dict()

    assert result["status"] == "failed"
    assert result["diagnostic"]["error_type"] == "quote_server_interrupted"
    assert client.closed is True
