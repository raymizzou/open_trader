from __future__ import annotations

import csv
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from open_trader.dashboard import DashboardConfig
from open_trader.dashboard_quotes import DashboardQuoteService
from open_trader.futu_quote import FutuQuoteError
from open_trader.futu_watch import QuoteSnapshot
from open_trader.portfolio import PORTFOLIO_FIELDNAMES


class FakeQuoteClient:
    def __init__(self, snapshots: dict[str, QuoteSnapshot]) -> None:
        self.snapshots = snapshots
        self.requested_symbols: list[str] = []
        self.closed = False

    def get_snapshots(self, futu_symbols: Sequence[str]) -> dict[str, QuoteSnapshot]:
        self.requested_symbols = list(futu_symbols)
        return self.snapshots

    def close(self) -> None:
        self.closed = True


class RaisingQuoteClient:
    def __init__(self) -> None:
        self.closed = False

    def get_snapshots(self, futu_symbols: Sequence[str]) -> dict[str, QuoteSnapshot]:
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


def test_quote_service_returns_ok_and_never_writes_portfolio(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    original_portfolio = config.portfolio_path.read_text(encoding="utf-8")
    client = FakeQuoteClient(
        {
            "US.MSFT": QuoteSnapshot("US.MSFT", Decimal("500")),
            "US.AAPL": QuoteSnapshot("US.AAPL", Decimal("160")),
        }
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


def test_quote_service_returns_partial_for_missing_quotes(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    client = FakeQuoteClient(
        {"US.MSFT": QuoteSnapshot("US.MSFT", Decimal("510.25"))}
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
            "US.MSFT": QuoteSnapshot("US.MSFT", Decimal("500")),
            "US.AAPL": QuoteSnapshot("US.AAPL", Decimal("160")),
        }
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
            "US.MSFT": QuoteSnapshot("US.MSFT", Decimal("500")),
            "US.AAPL": QuoteSnapshot("US.AAPL", Decimal("160")),
        }
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: full_client)
    first_result = service.refresh().to_dict()

    partial_client = FakeQuoteClient(
        {"US.MSFT": QuoteSnapshot("US.MSFT", Decimal("510.25"))}
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
