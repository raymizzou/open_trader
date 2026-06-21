from __future__ import annotations

import csv
import json
from pathlib import Path

from open_trader.advice.models import (
    PREMARKET_ACTION_FIELDNAMES,
    TRADING_ADVICE_FIELDNAMES,
)
from open_trader.dashboard import DashboardConfig, load_dashboard_state
from open_trader.portfolio import PORTFOLIO_FIELDNAMES
from open_trader.technical_facts import source_hash
from open_trader.trade_actions import TRADE_ACTION_FIELDNAMES
from open_trader.trading_plan import TRADING_PLAN_FIELDNAMES


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


def raw_decision_with_market_report(report: str) -> str:
    return json.dumps({"state": {"market_report": report}}, ensure_ascii=False)


def write_technical_facts(
    path: Path,
    *,
    report_hash: str,
    market: str = "US",
    extraction_status: str = "ok",
    timeframes: list[dict[str, object]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.technical_facts_cache.v1",
                "generated_at": "2026-06-19T08:30:00+08:00",
                "run_date": "2026-06-19",
                "market": "",
                "records": [
                    {
                        "run_date": "2026-06-19",
                        "market": market,
                        "symbol": "VIXY",
                        "source_status": "ok",
                        "source_advice_hash": report_hash,
                        "extraction_status": extraction_status,
                        "error": "" if extraction_status == "ok" else "llm unavailable",
                        "facts": {
                            "schema_version": "open_trader.technical_facts.v1",
                            "status": "present",
                            "source_date": "2026-06-19",
                            "market_data_as_of": "2026-06-18",
                            "symbol": f"{market}.VIXY",
                            "timeframes": timeframes
                            if timeframes is not None
                            else [
                                {
                                    "timeframe": "daily",
                                    "timeframe_label": "日线",
                                    "rsi": {"value": "56.88"},
                                }
                            ],
                        },
                        "freshness": {
                            "status": "fresh",
                            "message": "日线数据截至 2026-06-18",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
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
    assert state["summary"]["holding_count"] == 1
    assert state["summary"]["portfolio_value_hkd"] == "38680.00"
    assert state["summary"]["holding_value_hkd"] == "37830.00"
    assert state["summary"]["cash_like_value_hkd"] == "850.00"
    assert state["summary"]["holding_weight_hkd"] == "97.80%"
    assert state["summary"]["cash_like_weight_hkd"] == "2.20%"
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
    assert state["summary"]["holding_count"] == 1
    holdings_by_symbol = {row["symbol"]: row for row in state["holdings"]}
    assert "VIXY" in holdings_by_symbol
    assert holdings_by_symbol["VIXY"]["broker_detail_count"] == 0
    assert holdings_by_symbol["VIXY"]["broker_details"] == []
    assert holdings_by_symbol["VIXY"]["trade_action"] == {"available": False, "error": ""}


def test_load_dashboard_state_excludes_cash_like_rows_from_holdings(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    rows = [
        portfolio_rows()[0],
        {
            **portfolio_rows()[0],
            "sort_group": "3",
            "market": "HK",
            "asset_class": "money_market_fund",
            "symbol": "HK0000951506.HKD",
            "name": "华泰港元货币市场基金A",
            "currency": "HKD",
            "market_value_hkd": "597524.58",
            "portfolio_weight_hkd": "35.14%",
            "brokers": "tiger",
            "ai_eligible": "false",
            "analysis_symbol": "",
        },
        {
            **portfolio_rows()[1],
            "symbol": "FUTU_UNMAPPED_ASSETS",
            "name": "富途未明细账户资产",
            "market_value_hkd": "849884.06",
            "portfolio_weight_hkd": "49.98%",
        },
        {
            **portfolio_rows()[1],
            "symbol": "USD_CASH",
            "name": "USD Cash",
            "market_value_hkd": "-87760.17",
            "portfolio_weight_hkd": "-5.16%",
        },
    ]
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)

    state = load_dashboard_state(config).to_dict()

    assert state["summary"]["holding_count"] == 1
    assert state["summary"]["portfolio_value_hkd"] == "1397478.47"
    assert state["summary"]["holding_value_hkd"] == "37830.00"
    assert state["summary"]["cash_like_value_hkd"] == "1359648.47"
    assert state["summary"]["holding_weight_hkd"] == "2.71%"
    assert state["summary"]["cash_like_weight_hkd"] == "97.29%"
    assert [row["symbol"] for row in state["holdings"]] == ["VIXY"]


def test_load_dashboard_state_merges_agent_report_strategy_and_actions(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        [*TRADING_ADVICE_FIELDNAMES, "advice_summary_zh"],
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "agent",
                "advice_action": "reduce",
                "advice_summary": "Trim volatility exposure.",
                "advice_summary_zh": "减低波动率仓位。",
                "raw_decision": '{"rating":"reduce"}',
                "status": "ok",
                "error": "",
                "source_status": "fresh",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "trading_plan.csv",
        [*TRADING_PLAN_FIELDNAMES, "plan_text_zh"],
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "source_status": "fresh",
                "fallback_reason": "",
                "fallback_from_date": "",
                "rating": "reduce",
                "entry_zone_low": "",
                "entry_zone_high": "",
                "add_price": "",
                "stop_loss": "42.00",
                "target_1": "50.00",
                "target_2": "55.00",
                "max_weight": "5%",
                "catalyst": "Volatility spike",
                "time_horizon": "short",
                "plan_text": "Reduce after target hit.",
                "plan_text_zh": "达到目标价后减仓。",
                "agent_reason": "Risk is elevated.",
                "agent_excerpt": "Trim exposure.",
                "status": "ok",
                "error": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "premarket_actions.csv",
        PREMARKET_ACTION_FIELDNAMES,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "97.80%",
                "severity": "medium",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "Target hit.",
                "rationale": "Lock in gains.",
                "watch_trigger": "above 50",
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
                "suggested_quantity": "50",
                "status": "ready",
                "reason": "trim into strength",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["agent_report"] == {
        "available": True,
        "run_date": "2026-06-18",
        "market": "US",
        "symbol": "VIXY",
        "rating": "reduce",
        "summary": "Trim volatility exposure.",
        "summary_zh": "减低波动率仓位。",
        "raw_decision": '{"rating":"reduce"}',
        "source_status": "fresh",
        "fallback_reason": "",
        "fallback_from_date": "",
        "status": "ok",
        "error": "",
    }
    assert vixy["strategy"]["available"] is True
    assert vixy["strategy"]["stop_loss"] == "42.00"
    assert vixy["strategy"]["target_1"] == "50.00"
    assert vixy["strategy"]["plan_text"] == "Reduce after target hit."
    assert vixy["strategy"]["plan_text_zh"] == "达到目标价后减仓。"
    assert vixy["premarket_action"]["available"] is True
    assert vixy["premarket_action"]["suggested_action"] == "reduce"
    assert vixy["trade_action"]["available"] is True
    assert vixy["trade_action"]["action"] == "TRIM"
    assert vixy["trade_action"]["suggested_quantity"] == "50"


def test_load_dashboard_state_attaches_fresh_technical_facts(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    report = "Daily RSI is 56.88 with price above the 50 day average."
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Watch volatility.",
                "raw_decision": raw_decision_with_market_report(report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_technical_facts(
        config.data_dir / "latest" / "technical_facts.json",
        report_hash=source_hash(report),
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["technical_facts"]["available"] is True
    assert vixy["technical_facts"]["status"] == "usable"
    assert vixy["technical_facts"]["run_date"] == "2026-06-19"
    assert vixy["technical_facts"]["data_date"] == "2026-06-18"
    assert vixy["technical_facts"]["source_hash"] == source_hash(report)
    assert vixy["technical_facts"]["facts"]["timeframes"][0]["timeframe"] == "daily"


def test_load_dashboard_state_marks_missing_technical_facts_file_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["technical_facts"] == {
        "available": False,
        "status": "missing_file",
        "run_date": "",
        "data_date": "",
        "source_hash": "",
        "current_source_hash": "",
        "error": "technical_facts.json not found",
        "freshness": {},
        "facts": {},
    }


def test_load_dashboard_state_marks_stale_technical_facts_hash_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    current_report = "Current report says RSI is 40."
    old_report = "Old report says RSI is 70."
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Watch volatility.",
                "raw_decision": raw_decision_with_market_report(current_report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_technical_facts(
        config.data_dir / "latest" / "technical_facts.json",
        report_hash=source_hash(old_report),
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["technical_facts"]["available"] is False
    assert vixy["technical_facts"]["status"] == "stale_source_hash"
    assert vixy["technical_facts"]["run_date"] == "2026-06-19"
    assert vixy["technical_facts"]["data_date"] == "2026-06-18"
    assert vixy["technical_facts"]["source_hash"] == source_hash(old_report)
    assert vixy["technical_facts"]["current_source_hash"] == source_hash(current_report)
    assert vixy["technical_facts"]["facts"] == {}


def test_load_dashboard_state_prefers_market_scoped_technical_facts_and_advice(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    old_report = "Old unscoped report says RSI is 70."
    current_report = "Current scoped US report says RSI is 40."
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Old advice.",
                "raw_decision": raw_decision_with_market_report(old_report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Scoped advice.",
                "raw_decision": raw_decision_with_market_report(current_report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_technical_facts(
        config.data_dir / "latest" / "technical_facts.json",
        report_hash=source_hash(old_report),
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["agent_report"]["run_date"] == "2026-06-19"
    assert vixy["technical_facts"]["available"] is False
    assert vixy["technical_facts"]["status"] == "missing_file"
    assert vixy["technical_facts"]["current_source_hash"] == source_hash(current_report)


def test_load_dashboard_state_uses_scoped_facts_when_both_latest_layouts_exist(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    old_report = "Old unscoped report says RSI is 70."
    current_report = "Current scoped US report says RSI is 40."
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Old advice.",
                "raw_decision": raw_decision_with_market_report(old_report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Scoped advice.",
                "raw_decision": raw_decision_with_market_report(current_report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_technical_facts(
        config.data_dir / "latest" / "technical_facts.json",
        report_hash=source_hash(old_report),
    )
    write_technical_facts(
        config.data_dir / "latest" / "US" / "technical_facts.json",
        report_hash=source_hash(current_report),
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["technical_facts"]["available"] is True
    assert vixy["technical_facts"]["status"] == "usable"
    assert vixy["technical_facts"]["source_hash"] == source_hash(current_report)
    assert vixy["technical_facts"]["current_source_hash"] == source_hash(current_report)


def test_load_dashboard_state_marks_missing_agent_sections_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    unavailable = {"available": False, "error": ""}
    assert vixy["agent_report"] == unavailable
    assert vixy["strategy"] == unavailable
    assert vixy["premarket_action"] == unavailable
    assert vixy["trade_action"] == unavailable


def test_load_dashboard_state_reads_large_agent_report_fields(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    raw_decision = "x" * 150_000
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "agent",
                "advice_action": "reduce",
                "advice_summary": "Large raw decision.",
                "raw_decision": raw_decision,
                "status": "ok",
                "error": "",
                "source_status": "fresh",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["agent_report"]["raw_decision"] == raw_decision


def test_load_dashboard_state_attaches_research_view(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    bundle = config.data_dir / "research_data" / "US" / "VIXY" / "2026-06-19"
    bundle.mkdir(parents=True)
    (bundle / "dashboard_view.json").write_text(
        json.dumps(
            {
                "schema_version": "dashboard.research_view.v1",
                "market": "US",
                "symbol": "VIXY",
                "research_date": "2026-06-19",
                "tradingagents_conclusion": {
                    "status": "present",
                    "content": "低配，当前动作为减仓。",
                },
                "user_llm_conclusion": {"status": "missing", "content": ""},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["research_view"]["available"] is True
    assert vixy["research_view"]["research_date"] == "2026-06-19"
    assert (
        vixy["research_view"]["tradingagents_conclusion"]["content"]
        == "低配，当前动作为减仓。"
    )
    assert vixy["research_view"]["user_llm_conclusion"] == {
        "status": "missing",
        "content": "",
    }


def test_load_dashboard_state_marks_missing_research_view(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["research_view"]["available"] is False
    assert vixy["research_view"]["tradingagents_conclusion"] == {
        "status": "missing",
        "content": "",
    }


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


def test_load_dashboard_state_builds_broker_summaries_from_detail_rows(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-06-19"
    write_csv(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu",
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
                "statement_id": "2026-06-19-tiger",
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
                "statement_id": "2026-06-19-futu",
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

    state = load_dashboard_state(config).to_dict()

    holdings_by_symbol = {row["symbol"]: row for row in state["holdings"]}
    vixy_details = {
        row["broker"]: row for row in holdings_by_symbol["VIXY"]["broker_details"]
    }
    assert vixy_details["futu"]["market_value_hkd"] == "15132.00"
    assert vixy_details["tiger"]["market_value_hkd"] == "22698.00"

    summaries = {row["broker"]: row for row in state["broker_summaries"]}
    assert summaries["futu"]["label"] == "富途"
    assert summaries["futu"]["source_kind"] == "live_account"
    assert summaries["futu"]["holding_value_hkd"] == "15132.00"
    assert summaries["futu"]["cash_like_value_hkd"] == "850.00"
    assert summaries["futu"]["portfolio_value_hkd"] == "15982.00"
    assert summaries["futu"]["holding_count"] == 1
    assert summaries["tiger"]["label"] == "老虎"
    assert summaries["tiger"]["holding_value_hkd"] == "22698.00"
    assert summaries["tiger"]["cash_like_value_hkd"] == "0.00"
    assert summaries["tiger"]["portfolio_value_hkd"] == "22698.00"
    assert summaries["tiger"]["holding_count"] == 1
    assert summaries["phillips"]["label"] == "辉立"
    assert summaries["phillips"]["portfolio_value_hkd"] == ""
    assert summaries["phillips"]["source_kind"] == "statement"
    assert summaries["phillips"]["detail_available"] is False

    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["futu"]["display_text"] == "仅月结单明细"
    assert statuses["futu"]["status"] == "non_realtime"
    assert statuses["tiger"]["display_text"] == "仅月结单明细"
    assert statuses["tiger"]["status"] == "non_realtime"
    assert statuses["phillips"]["display_text"] == "暂无月结单明细"
    assert statuses["phillips"]["status"] == "non_realtime"


def test_load_dashboard_state_exposes_cash_rows_for_dashboard_view(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    assert [row["symbol"] for row in state["cash_rows"]] == ["HKD_CASH"]
    assert state["cash_rows"][0]["market_value_hkd"] == "850.00"
    assert state["cash_rows"][0]["brokers"] == "futu"


def test_load_dashboard_state_discovers_cash_only_detail_runs(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "runs" / "2026-06-19" / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-tiger",
                "broker": "tiger",
                "account_alias": "growth",
                "currency": "USD",
                "cash_balance": "10.00",
                "available_balance": "10.00",
                "confidence": "high",
                "notes": "",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    assert state["broker_detail_month"] == "2026-06-19"
    assert state["detail_available"] is True
    assert len(state["cash_details"]) == 1
    summaries = {row["broker"]: row for row in state["broker_summaries"]}
    assert summaries["tiger"]["detail_available"] is True
    assert summaries["tiger"]["holding_value_hkd"] == "0.00"
    assert summaries["tiger"]["cash_like_value_hkd"] == "78.00"
    assert summaries["tiger"]["portfolio_value_hkd"] == "78.00"
    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["tiger"]["status"] == "non_realtime"
    assert statuses["tiger"]["display_text"] == "仅月结单明细"


def test_load_dashboard_state_marks_futu_and_tiger_live_only_from_live_statement_ids(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-06-19"
    write_csv(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu-live",
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
        ],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-tiger-live",
                "broker": "tiger",
                "account_alias": "growth",
                "currency": "USD",
                "cash_balance": "10.00",
                "available_balance": "10.00",
                "confidence": "high",
                "notes": "",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["futu"]["status"] == "ok"
    assert statuses["futu"]["display_text"] == "账户实时同步"
    assert statuses["tiger"]["status"] == "ok"
    assert statuses["tiger"]["display_text"] == "账户实时同步，行情走富途"


def test_load_dashboard_state_rejects_live_marker_unless_statement_id_suffix(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-06-19"
    row = {
        "statement_id": "2026-05-futu-live-statement-import",
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
    }
    write_csv(run_dir / "extracted_positions.csv", POSITION_FIELDNAMES, [row])

    state = load_dashboard_state(config).to_dict()

    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["futu"]["status"] == "non_realtime"
    assert statuses["futu"]["display_text"] == "仅月结单明细"

    write_csv(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [{**row, "statement_id": "2026-06-19-futu-live"}],
    )

    state = load_dashboard_state(config).to_dict()

    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["futu"]["status"] == "ok"
    assert statuses["futu"]["display_text"] == "账户实时同步"


def test_load_dashboard_state_uses_phillips_statement_id_for_source_status(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "runs" / "2026-06-19" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-05-phillips",
                "broker": "phillips",
                "account_alias": "cash",
                "market": "HK",
                "asset_class": "stock",
                "symbol": "00700",
                "name": "Tencent",
                "currency": "HKD",
                "quantity": "100",
                "cost_price": "100.00",
                "last_price": "150.00",
                "market_value": "15000.00",
                "cost_value": "10000.00",
                "unrealized_pnl": "5000.00",
                "confidence": "high",
                "notes": "",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["phillips"]["display_text"] == "2026-05 月结单导入"


def test_load_dashboard_state_blanks_unsupported_or_malformed_detail_money(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-06-19"
    write_csv(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "VIXY",
                "currency": "USD",
                "quantity": "1",
                "cost_price": "",
                "last_price": "",
                "market_value": "10.00",
                "cost_value": "",
                "unrealized_pnl": "",
                "confidence": "high",
                "notes": "",
            },
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "market": "HK",
                "asset_class": "stock",
                "symbol": "00001",
                "name": "Unsupported Currency",
                "currency": "EUR",
                "quantity": "1",
                "cost_price": "",
                "last_price": "",
                "market_value": "100.00",
                "cost_value": "",
                "unrealized_pnl": "",
                "confidence": "low",
                "notes": "",
            },
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "market": "HK",
                "asset_class": "stock",
                "symbol": "00002",
                "name": "Malformed Value",
                "currency": "HKD",
                "quantity": "1",
                "cost_price": "",
                "last_price": "",
                "market_value": "not-money",
                "cost_value": "",
                "unrealized_pnl": "",
                "confidence": "low",
                "notes": "",
            },
        ],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "currency": "USD",
                "cash_balance": "bad-cash",
                "available_balance": "bad-cash",
                "confidence": "low",
                "notes": "",
            },
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "currency": "CNY",
                "cash_balance": "100.00",
                "available_balance": "100.00",
                "confidence": "high",
                "notes": "",
            },
        ],
    )

    state = load_dashboard_state(config).to_dict()

    summaries = {row["broker"]: row for row in state["broker_summaries"]}
    assert summaries["futu"]["holding_value_hkd"] == ""
    assert summaries["futu"]["cash_like_value_hkd"] == ""
    assert summaries["futu"]["portfolio_value_hkd"] == ""


def test_load_dashboard_state_uses_single_broker_portfolio_fallback(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    rows = [
        {**portfolio_rows()[0], "brokers": "phillips", "accounts": "cash"},
        {**portfolio_rows()[1], "brokers": "phillips", "accounts": "cash"},
    ]
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)

    state = load_dashboard_state(config).to_dict()

    summaries = {row["broker"]: row for row in state["broker_summaries"]}
    assert summaries["phillips"]["detail_available"] is False
    assert summaries["phillips"]["holding_value_hkd"] == "37830.00"
    assert summaries["phillips"]["cash_like_value_hkd"] == "850.00"
    assert summaries["phillips"]["portfolio_value_hkd"] == "38680.00"
    assert summaries["phillips"]["holding_count"] == 1


def test_load_dashboard_state_blanks_multi_broker_portfolio_fallback(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    summaries = {row["broker"]: row for row in state["broker_summaries"]}
    assert summaries["futu"]["holding_value_hkd"] == ""
    assert summaries["futu"]["cash_like_value_hkd"] == ""
    assert summaries["futu"]["portfolio_value_hkd"] == ""
    assert summaries["futu"]["holding_count"] == 0
    assert summaries["tiger"]["holding_value_hkd"] == ""
    assert summaries["tiger"]["cash_like_value_hkd"] == ""
    assert summaries["tiger"]["portfolio_value_hkd"] == ""
    assert summaries["tiger"]["holding_count"] == 0
