from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from open_trader.dashboard import DashboardConfig
from open_trader.dashboard_acceptance import validate_quotes_payload
from open_trader.dashboard_quotes import DashboardQuoteService, load_published_quotes
from open_trader.futu_quote import DashboardQuoteSnapshot, FutuQuoteError
from open_trader.futu_universe import (
    FutuQuoteUniverse,
    FutuUniverseItem,
    SkippedFutuUniverseRow,
    load_futu_quote_universe,
)
from open_trader.portfolio import PORTFOLIO_FIELDNAMES


class FakeQuoteClient:
    def __init__(
        self,
        snapshots: dict[str, DashboardQuoteSnapshot],
        states: dict[str, str] | None = None,
        state_error: FutuQuoteError | None = None,
        snapshot_errors: dict[str, FutuQuoteError] | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.states = states or {}
        self.state_error = state_error
        self.snapshot_errors = snapshot_errors or {}
        self.requested_symbols: list[str] = []
        self.requested_batches: list[list[str]] = []
        self.requested_state_symbols: list[str] = []
        self.closed = False
        self.close_count = 0

    def get_dashboard_snapshots(
        self, futu_symbols: Sequence[str]
    ) -> dict[str, DashboardQuoteSnapshot]:
        symbols = list(futu_symbols)
        self.requested_batches.append(symbols)
        self.requested_symbols.extend(symbols)
        if error := self.snapshot_errors.get(symbols[0].split(".", 1)[0]):
            raise error
        return {
            symbol: self.snapshots[symbol]
            for symbol in symbols
            if symbol in self.snapshots
        }

    def get_market_states(self, futu_symbols: Sequence[str]) -> dict[str, str]:
        self.requested_state_symbols = list(futu_symbols)
        if self.state_error is not None:
            raise self.state_error
        return self.states

    def close(self) -> None:
        self.closed = True
        self.close_count += 1


def test_load_published_quotes_rejects_missing_or_malformed_files_and_derives_staleness(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quotes.json"
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    assert load_published_quotes(path, now=now)["status"] == "unknown"
    path.write_text("not json", encoding="utf-8")
    assert load_published_quotes(path, now=now)["status"] == "unknown"
    path.write_text(
        json.dumps(
            {
                "status": "ok",
                "last_success_at": (now - timedelta(seconds=16)).isoformat(),
                "stale": False,
                "quotes": {"US.MSFT": {"last_price": "500", "stale": False}},
            }
        ),
        encoding="utf-8",
    )

    payload = load_published_quotes(path, now=now)

    assert payload["status"] == "ok"
    assert payload["stale"] is True
    assert payload["quotes"] == {"US.MSFT": {"last_price": "500", "stale": True}}


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


def session_snapshot(
    *, update_time: str = "2026-07-15 03:03:01.150", **prices: str | None
) -> DashboardQuoteSnapshot:
    return DashboardQuoteSnapshot(
        futu_symbol="US.MSFT",
        last_price=Decimal(prices["last"]) if prices.get("last") else None,
        pre_price=Decimal(prices["pre"]) if prices.get("pre") else None,
        after_price=Decimal(prices["after"]) if prices.get("after") else None,
        overnight_price=Decimal(prices["overnight"]) if prices.get("overnight") else None,
        update_time=update_time,
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

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh(load_futu_quote_universe(config.portfolio_path))
    quote = result.quotes["US.MSFT"]

    assert quote["last_price"] == expected_price
    assert quote["price_session"] == expected_session
    assert quote["price_time"] == "2026-07-15 03:03:01.150"
    assert quote["current_session_quote"] is True
    assert result.fallback_count == 0


def test_quote_service_normalizes_active_us_price_time_to_milliseconds(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(
        update_time="2026-07-15 03:03:01",
        last="61.23",
        pre="60.73",
        after="62.22",
        overnight="61.50",
    )
    client = FakeQuoteClient(
        {"US.MSFT": snapshot, "US.AAPL": snapshot},
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh(load_futu_quote_universe(config.portfolio_path))

    assert result.quotes["US.MSFT"]["price_time"] == "2026-07-15 03:03:01.000"


@pytest.mark.parametrize(
    ("update_time", "expected_price_time"),
    [
        ("2026-07-15", "2026-07-15"),
        ("not-a-timestamp", "not-a-timestamp"),
        ("2026-07-15T03:03:01+00:00", "2026-07-15T03:03:01+00:00"),
    ],
)
def test_quote_service_preserves_non_naive_futu_price_time(
    tmp_path: Path, update_time: str, expected_price_time: str
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    snapshot = session_snapshot(
        update_time=update_time,
        last="61.23",
        pre="60.73",
        after="62.22",
        overnight="61.50",
    )
    client = FakeQuoteClient(
        {"US.MSFT": snapshot, "US.AAPL": snapshot},
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh(load_futu_quote_universe(config.portfolio_path))

    assert result.quotes["US.MSFT"]["price_time"] == expected_price_time


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

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh(load_futu_quote_universe(config.portfolio_path))
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

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh(load_futu_quote_universe(config.portfolio_path))

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

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh(load_futu_quote_universe(config.portfolio_path))

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

    result = DashboardQuoteService(config, client_factory=lambda: client).refresh(load_futu_quote_universe(config.portfolio_path))

    assert result.status == "partial"
    assert result.us_session_status == "unknown"
    assert result.quotes["US.MSFT"]["last_price"] == "61.23"
    assert result.quotes["US.MSFT"]["price_session"] == ""
    assert "市场状态不可用" in result.diagnostic["message"]


def test_quote_service_reuses_cache_when_us_market_state_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_times = iter(
        ["2026-07-22T16:38:34+08:00", "2026-07-22T16:38:39+08:00"]
    )
    monkeypatch.setattr(
        "open_trader.dashboard_quotes._now_text", lambda: next(fetched_times)
    )
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
    first_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()
    cached_quotes = {symbol: dict(quote) for symbol, quote in service.last_quotes.items()}
    second_client = FakeQuoteClient(
        {
            "US.MSFT": session_snapshot(last="510"),
            "US.AAPL": session_snapshot(last="165"),
        },
        {"US.MSFT": "", "US.AAPL": ""},
    )
    service.client_factory = lambda: second_client

    result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

    assert validate_quotes_payload(result) == []
    assert result["status"] == "partial"
    assert result["stale"] is True
    assert result["us_session_status"] == "active"
    assert result["last_success_at"] == first_result["last_success_at"]
    assert result["fetched_at"] != result["last_success_at"]
    assert service.last_quotes == cached_quotes
    assert "上一笔有效分时段行情" in result["diagnostic"]["message"]


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
    first_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()
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

    result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

    assert result["status"] == "partial"
    assert result["stale"] is True
    assert result["us_session_status"] == "active"
    assert result["last_success_at"] == first_result["last_success_at"]
    assert result["quotes"]["US.MSFT"] == {
        **first_result["quotes"]["US.MSFT"],
        "stale": True,
    }
    assert "上一笔有效分时段行情" in result["diagnostic"]["message"]


def test_quote_service_caches_complete_us_subset_despite_sh_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_times = iter(
        ["2026-07-22T16:38:34+08:00", "2026-07-22T16:38:39+08:00"]
    )
    monkeypatch.setattr(
        "open_trader.dashboard_quotes._now_text", lambda: next(fetched_times)
    )
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
    snapshots = {
        "US.MSFT": session_snapshot(last="500"),
        "US.AAPL": session_snapshot(last="160"),
    }
    sh_error = FutuQuoteError(
        "无权限获取SH.600900的行情",
        error_type="snapshot_failed",
        snapshot_ok=False,
    )
    first_client = FakeQuoteClient(
        snapshots,
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
        snapshot_errors={"SH": sh_error},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: first_client)
    first_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()
    state_error = FutuQuoteError(
        "state failed", error_type="market_state_failed", snapshot_ok=True
    )
    second_client = FakeQuoteClient(
        snapshots,
        state_error=state_error,
        snapshot_errors={"SH": sh_error},
    )
    service.client_factory = lambda: second_client

    result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

    assert validate_quotes_payload(result) == []
    assert first_result["last_success_at"] == ""
    assert result["last_success_at"] == first_result["last_success_at"]
    assert result["fetched_at"] != result["last_success_at"]
    assert result["status"] == "partial"
    assert result["stale"] is True
    assert result["us_session_status"] == "active"
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

    result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

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


def test_quote_service_refreshes_the_explicit_account_universe_without_reading_portfolio(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    original_portfolio = config.portfolio_path.read_text(encoding="utf-8")
    client = FakeQuoteClient(
        {"HK.02824": session_snapshot(last="48.38")},
    )
    universe = FutuQuoteUniverse(
        items=[
            FutuUniverseItem(
                row_number=1,
                market="HK",
                asset_class="stock",
                symbol="02824",
                futu_symbol="HK.02824",
                name="易方达黄金",
            )
        ],
        skipped=[],
    )

    result = DashboardQuoteService(config=config, client_factory=lambda: client).refresh(
        universe
    )

    assert client.requested_symbols == ["HK.02824"]
    assert result.quotes["HK.02824"]["last_price"] == "48.38"
    assert config.portfolio_path.read_text(encoding="utf-8") == original_portfolio


def test_quote_service_preserves_hk_opend_update_time_with_market_timezone(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    client = FakeQuoteClient(
        {
            "HK.02824": session_snapshot(
                update_time="2026-08-04 10:30:05.123", last="48.38"
            )
        }
    )
    universe = FutuQuoteUniverse(
        items=[
            FutuUniverseItem(
                row_number=1,
                market="HK",
                asset_class="stock",
                symbol="02824",
                futu_symbol="HK.02824",
                name="易方达黄金",
            )
        ],
        skipped=[],
    )

    result = DashboardQuoteService(config=config, client_factory=lambda: client).refresh(
        universe
    )

    assert result.quotes["HK.02824"]["price_time"] == "2026-08-04T10:30:05.123+08:00"


def test_quote_service_batches_holdings_by_futu_exchange_prefix(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    with config.portfolio_path.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES).writerows(
            [
                {
                    "market": "CN",
                    "asset_class": "stock",
                    "symbol": "600025",
                    "name": "华能水电",
                    "total_quantity": "6000",
                },
                {
                    "market": "CN",
                    "asset_class": "stock",
                    "symbol": "000001",
                    "name": "平安银行",
                    "total_quantity": "100",
                },
                {
                    "market": "CN",
                    "asset_class": "stock",
                    "symbol": "920000",
                    "name": "北交所样例",
                    "total_quantity": "100",
                },
            ]
        )
    client = FakeQuoteClient(
        {
            "US.MSFT": session_snapshot(last="500"),
            "US.AAPL": session_snapshot(last="160"),
            "SH.600025": session_snapshot(last="9.81"),
            "SZ.000001": session_snapshot(last="12.30"),
            "BJ.920000": session_snapshot(last="8.50"),
        },
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )

    result = DashboardQuoteService(
        config=config,
        client_factory=lambda: client,
    ).refresh(load_futu_quote_universe(config.portfolio_path))

    assert client.requested_symbols == [
        "BJ.920000",
        "SH.600025",
        "SZ.000001",
        "US.AAPL",
        "US.MSFT",
    ]
    assert client.requested_batches == [
        ["BJ.920000"],
        ["SH.600025"],
        ["SZ.000001"],
        ["US.AAPL", "US.MSFT"],
    ]
    assert result.quotes["SH.600025"]["last_price"] == "9.81"


def test_quote_service_retains_stale_quotes_for_a_failed_prefix(
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
    first_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()
    cached_quotes = {symbol: dict(quote) for symbol, quote in service.last_quotes.items()}
    with config.portfolio_path.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES).writerow(
            {
                "market": "CN",
                "asset_class": "stock",
                "symbol": "600901",
                "name": "新标的",
                "total_quantity": "100",
            }
        )

    client = FakeQuoteClient(
        {
            "US.AAPL": session_snapshot(last="165"),
            "US.MSFT": session_snapshot(last="510"),
        },
        {"US.AAPL": "MORNING", "US.MSFT": "MORNING"},
        snapshot_errors={
            "SH": FutuQuoteError(
                "无权限获取SH.600900的行情，请检查A股市场股票行情权限",
                error_type="snapshot_failed",
                opend_reachable=True,
                context_ok=True,
                snapshot_ok=False,
            )
        },
    )
    created_clients: list[FakeQuoteClient] = []

    def client_factory() -> FakeQuoteClient:
        created_clients.append(client)
        return client

    service.client_factory = client_factory

    result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

    assert created_clients == [client]
    assert client.requested_batches == [
        ["SH.600900", "SH.600901"],
        ["US.AAPL", "US.MSFT"],
    ]
    assert client.requested_state_symbols == ["US.AAPL", "US.MSFT"]
    assert client.close_count == 1
    assert result["status"] == "partial"
    assert result["quote_count"] == 3
    assert result["missing_count"] == 1
    assert result["stale"] is True
    assert result["quotes"]["SH.600900"] == {
        **cached_quotes["SH.600900"],
        "stale": True,
    }
    assert result["quotes"]["SH.600901"]["status"] == "missing_quote"
    assert result["quotes"]["US.AAPL"]["last_price"] == "165"
    assert result["quotes"]["US.MSFT"]["last_price"] == "510"
    assert result["us_session_status"] == "active"
    assert result["diagnostic"]["market"] == "SH"
    assert "无权限获取SH.600900的行情" in result["diagnostic"]["message"]
    assert result["last_success_at"] == first_result["last_success_at"]
    assert service.last_quotes["SH.600900"] == cached_quotes["SH.600900"]
    assert service.last_quotes["US.AAPL"] == result["quotes"]["US.AAPL"]
    assert service.last_quotes["US.MSFT"] == result["quotes"]["US.MSFT"]


def test_quote_service_retains_stale_quotes_when_us_snapshot_prefix_fails(
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

    first_client = FakeQuoteClient(
        {
            "SH.600900": session_snapshot(last="30"),
            "US.AAPL": session_snapshot(last="160"),
            "US.MSFT": session_snapshot(last="500"),
        },
        {"US.AAPL": "MORNING", "US.MSFT": "MORNING"},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: first_client)
    first_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()
    cached_quotes = {symbol: dict(quote) for symbol, quote in service.last_quotes.items()}
    us_snapshot_error = FutuQuoteError(
        "无权限获取美股行情",
        error_type="us_snapshot_failed",
        snapshot_ok=False,
    )
    client = FakeQuoteClient(
        {"SH.600900": session_snapshot(last="31")},
        snapshot_errors={
            "US": us_snapshot_error,
        },
    )
    service.client_factory = lambda: client

    result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

    assert client.requested_batches == [
        ["SH.600900"],
        ["US.AAPL", "US.MSFT"],
    ]
    assert client.requested_state_symbols == []
    assert client.close_count == 1
    assert result["status"] == "partial"
    assert result["quote_count"] == 3
    assert result["missing_count"] == 0
    assert result["stale"] is True
    assert result["quotes"]["SH.600900"]["last_price"] == "31"
    assert result["quotes"]["US.AAPL"] == {
        **cached_quotes["US.AAPL"],
        "stale": True,
    }
    assert result["quotes"]["US.MSFT"] == {
        **cached_quotes["US.MSFT"],
        "stale": True,
    }
    assert result["us_session_status"] == "unknown"
    assert result["diagnostic"]["market"] == "US"
    assert result["diagnostic"]["error_type"] == "us_snapshot_failed"
    assert "无权限获取美股行情" in result["diagnostic"]["message"]
    assert result["last_success_at"] == first_result["last_success_at"]
    assert service.last_quotes["SH.600900"] == result["quotes"]["SH.600900"]
    assert service.last_quotes["US.AAPL"] == cached_quotes["US.AAPL"]
    assert service.last_quotes["US.MSFT"] == cached_quotes["US.MSFT"]


def test_quote_service_retains_all_stale_quotes_when_every_prefix_fails(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    first_client = FakeQuoteClient(
        {
            "US.AAPL": session_snapshot(last="160"),
            "US.MSFT": session_snapshot(last="500"),
        },
        {"US.AAPL": "MORNING", "US.MSFT": "MORNING"},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: first_client)
    first_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()
    us_error = FutuQuoteError(
        "美股 snapshot 失败", error_type="us_snapshot_failed", snapshot_ok=False
    )
    failed_client = FakeQuoteClient({}, snapshot_errors={"US": us_error})
    service.client_factory = lambda: failed_client

    result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

    assert result["status"] == "failed"
    assert result["quotes"] == {
        symbol: {**quote, "stale": True}
        for symbol, quote in first_result["quotes"].items()
    }
    assert result["stale"] is True
    assert result["last_success_at"] == first_result["last_success_at"]


def test_quote_service_returns_partial_for_missing_quotes(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    client = FakeQuoteClient(
        {"US.MSFT": session_snapshot(last="510.25")},
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: client)

    result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

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


def test_quote_service_keeps_skipped_account_rows_in_diagnostics(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    universe = FutuQuoteUniverse(
        items=[],
        skipped=[SkippedFutuUniverseRow(4, "CN", "cash", "CNY_CASH", "excluded_asset_class")],
    )

    result = DashboardQuoteService(config).refresh(universe).to_dict()

    assert result["status"] == "ok"
    assert result["diagnostic"]["skipped"] == [{
        "row_number": 4,
        "market": "CN",
        "asset_class": "cash",
        "symbol": "CNY_CASH",
        "reason": "excluded_asset_class",
    }]


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
    first_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

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
    failed_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

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
    first_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

    partial_client = FakeQuoteClient(
        {"US.MSFT": session_snapshot(last="510.25")},
        {"US.MSFT": "MORNING", "US.AAPL": "MORNING"},
    )
    service.client_factory = lambda: partial_client
    partial_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()
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
    failed_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

    assert failed_result["status"] == "failed"
    assert failed_result["last_success_at"] == first_result["last_success_at"]
    assert failed_result["quotes"]["US.AAPL"]["last_price"] == "160"
    assert failed_result["quotes"]["US.AAPL"]["stale"] is True


def test_quote_service_all_prefix_batches_fail_with_first_error_and_stale_cache(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_portfolio(config.portfolio_path)
    with config.portfolio_path.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES).writerows(
            [
                {
                    "market": "CN",
                    "asset_class": "stock",
                    "symbol": "600900",
                    "name": "长江电力",
                    "total_quantity": "100",
                },
                {
                    "market": "CN",
                    "asset_class": "stock",
                    "symbol": "000001",
                    "name": "平安银行",
                    "total_quantity": "100",
                },
            ]
        )
    first_client = FakeQuoteClient(
        {
            "SH.600900": session_snapshot(last="30"),
            "SZ.000001": session_snapshot(last="12"),
            "US.AAPL": session_snapshot(last="160"),
            "US.MSFT": session_snapshot(last="500"),
        },
        {"US.AAPL": "MORNING", "US.MSFT": "MORNING"},
    )
    service = DashboardQuoteService(config=config, client_factory=lambda: first_client)
    first_result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()
    client = FakeQuoteClient(
        {},
        snapshot_errors={
            prefix: FutuQuoteError(
                f"{prefix} 行情失败",
                error_type=f"{prefix.lower()}_snapshot_failed",
                next_step=f"检查 {prefix} 行情权限。",
                opend_reachable=True,
                context_ok=True,
                snapshot_ok=False,
            )
            for prefix in ("SH", "SZ", "US")
        },
    )
    service.client_factory = lambda: client

    result = service.refresh(load_futu_quote_universe(config.portfolio_path)).to_dict()

    assert client.requested_batches == [
        ["SH.600900"],
        ["SZ.000001"],
        ["US.AAPL", "US.MSFT"],
    ]
    assert client.close_count == 1
    assert result["status"] == "failed"
    assert result["diagnostic"]["error_type"] == "sh_snapshot_failed"
    assert result["diagnostic"]["message"] == "SH 行情失败"
    assert result["stale"] is True
    assert result["last_success_at"] == first_result["last_success_at"]
    assert result["quotes"] == {
        symbol: {**quote, "stale": True}
        for symbol, quote in first_result["quotes"].items()
    }
