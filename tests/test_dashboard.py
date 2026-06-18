from __future__ import annotations

import csv
from pathlib import Path

from open_trader.dashboard import DashboardConfig, load_dashboard_state
from open_trader.portfolio import PORTFOLIO_FIELDNAMES
from open_trader.trade_actions import TRADE_ACTION_FIELDNAMES


POSITION_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "market",
    "asset_class",
    "symbol",
    "name",
    "currency",
    "quantity",
    "cost_price",
    "last_price",
    "market_value",
    "cost_value",
    "unrealized_pnl",
    "confidence",
    "notes",
]

CASH_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "currency",
    "cash_balance",
    "available_balance",
    "confidence",
    "notes",
]


def write_csv(path: Path, fieldnames: list[str] | tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def dashboard_config(tmp_path: Path) -> DashboardConfig:
    return DashboardConfig(
        portfolio_path=tmp_path / "data" / "latest" / "portfolio.csv",
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        poll_seconds=1.5,
        futu_host="127.0.0.1",
        futu_port=11111,
    )


def portfolio_rows() -> list[dict[str, str]]:
    return [
        {
            "sort_group": "4",
            "market": "US",
            "asset_class": "etf",
            "symbol": "VIXY",
            "name": "ProShares VIX Short-Term Futures ETF",
            "currency": "USD",
            "total_quantity": "100",
            "avg_cost_price": "45.00",
            "last_price": "48.50",
            "market_value": "4850.00",
            "cost_value": "4500.00",
            "unrealized_pnl": "350.00",
            "unrealized_pnl_pct": "7.78%",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "37830.00",
            "cost_value_hkd": "35100.00",
            "portfolio_weight_hkd": "97.80%",
            "brokers": "futu;tiger",
            "accounts": "main;growth",
            "ai_eligible": "true",
            "analysis_symbol": "VIXY",
            "risk_flag": "overweight",
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
            "market_value": "850.00",
            "cost_value": "",
            "unrealized_pnl": "",
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "1",
            "market_value_hkd": "850.00",
            "cost_value_hkd": "",
            "portfolio_weight_hkd": "2.20%",
            "brokers": "futu",
            "accounts": "main",
            "ai_eligible": "false",
            "analysis_symbol": "",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ]


def test_load_dashboard_state_merges_portfolio_details_cash_and_trade_actions(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-05"
    write_csv(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-05-futu",
                "broker": "futu",
                "account_alias": "main",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "ProShares VIX Short-Term Futures ETF",
                "currency": "USD",
                "quantity": "40",
                "cost_price": "44.00",
                "last_price": "48.50",
                "market_value": "1940.00",
                "cost_value": "1760.00",
                "unrealized_pnl": "180.00",
                "confidence": "high",
                "notes": "",
            },
            {
                "statement_id": "2026-05-tiger",
                "broker": "tiger",
                "account_alias": "growth",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "ProShares VIX Short-Term Futures ETF",
                "currency": "USD",
                "quantity": "60",
                "cost_price": "45.67",
                "last_price": "48.50",
                "market_value": "2910.00",
                "cost_value": "2740.00",
                "unrealized_pnl": "170.00",
                "confidence": "high",
                "notes": "",
            },
        ],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [
            {
                "statement_id": "2026-05-futu",
                "broker": "futu",
                "account_alias": "main",
                "currency": "HKD",
                "cash_balance": "850.00",
                "available_balance": "850.00",
                "confidence": "high",
                "notes": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "trade_actions.csv",
        TRADE_ACTION_FIELDNAMES,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "futu_symbol": "US.VIXY",
                "action": "TRIM",
                "priority": "medium",
                "last_price": "48.50",
                "trigger_status": "target_1_hit",
                "status": "ready",
                "reason": "trim into strength",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    assert state["portfolio_path"] == str(config.portfolio_path)
    assert state["data_dir"] == str(config.data_dir)
    assert state["reports_dir"] == str(config.reports_dir)
    assert state["poll_seconds"] == 1.5
    assert state["futu_host"] == "127.0.0.1"
    assert state["futu_port"] == 11111
    assert state["broker_detail_month"] == "2026-05"
    assert state["detail_available"] is True
    assert state["summary"]["holding_count"] == 2
    assert state["summary"]["portfolio_value_hkd"] == "38680.00"
    assert state["summary"]["broker_count"] == 2
    assert len(state["broker_positions"]) == 2
    assert len(state["cash_details"]) == 1
    assert len(state["trade_actions"]) == 1

    holdings_by_symbol = {row["symbol"]: row for row in state["holdings"]}
    assert holdings_by_symbol["VIXY"]["broker_detail_count"] == 2
    assert [
        {
            "broker": row["broker"],
            "account_alias": row["account_alias"],
            "quantity": row["quantity"],
            "market_value": row["market_value"],
        }
        for row in holdings_by_symbol["VIXY"]["broker_details"]
    ] == [
        {
            "broker": "futu",
            "account_alias": "main",
            "quantity": "40",
            "market_value": "1940.00",
        },
        {
            "broker": "tiger",
            "account_alias": "growth",
            "quantity": "60",
            "market_value": "2910.00",
        },
    ]
    assert holdings_by_symbol["VIXY"]["trade_action"]["action"] == "TRIM"


def test_load_dashboard_state_uses_portfolio_when_monthly_details_are_absent(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    assert state["broker_detail_month"] == ""
    assert state["detail_available"] is False
    assert state["summary"]["holding_count"] == 2
    holdings_by_symbol = {row["symbol"]: row for row in state["holdings"]}
    assert "VIXY" in holdings_by_symbol
    assert holdings_by_symbol["VIXY"]["broker_detail_count"] == 0
    assert holdings_by_symbol["VIXY"]["broker_details"] == []
    assert holdings_by_symbol["VIXY"]["trade_action"] == {}


def test_load_dashboard_state_prefers_latest_daily_sync_details(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "runs" / "2026-05" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-05-futu",
                "broker": "futu",
                "account_alias": "old",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "VIXY",
                "currency": "USD",
                "quantity": "165",
                "cost_price": "",
                "last_price": "24.41",
                "market_value": "4027.65",
                "cost_value": "",
                "unrealized_pnl": "",
                "confidence": "high",
                "notes": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "runs" / "2026-06-19" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu-live",
                "broker": "futu",
                "account_alias": "live",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "VIXY",
                "currency": "USD",
                "quantity": "100",
                "cost_price": "42.62",
                "last_price": "21.93",
                "market_value": "2193.00",
                "cost_value": "4261.60",
                "unrealized_pnl": "-2068.60",
                "confidence": "high",
                "notes": "Futu live account position",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    assert state["broker_detail_month"] == "2026-06-19"
    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["broker_details"][0]["account_alias"] == "live"
    assert vixy["broker_details"][0]["quantity"] == "100"
