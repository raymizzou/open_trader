from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.trading_plan import (
    TRADING_PLAN_FIELDNAMES,
    _english_reason_themes,
    PlanQuoteStatus,
    TradingPlanBuildResult,
    TradingPlanRow,
    build_trading_plan,
    evaluate_plan_quote,
    load_trading_plan_rows,
)


ADVICE_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "asset_class",
    "portfolio_weight_hkd",
    "risk_flag",
    "source",
    "advice_action",
    "advice_summary",
    "raw_decision",
    "status",
    "error",
    "source_status",
    "fallback_reason",
    "fallback_from_date",
]


def write_advice(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ADVICE_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def msft_advice_summary() -> str:
    return "\n".join(
        [
            "评级：Overweight",
            "操作计划：在380-400美元区间分3-4次买入目标仓位的60%，350美元附近加仓剩余40%。",
            "风控：统一停损线设在340美元。",
            "仓位：总仓位控制在投资组合的8%-12%。",
            "催化剂：10月底财报为关键催化剂。",
            "目标价：450 / 500",
            "时间窗口：3-6个月",
            "理由：微软AI商业化路径清晰。",
        ]
    )


def mrvl_underweight_summary() -> str:
    return "\n".join(
        [
            "评级：Underweight",
            (
                "操作计划：Reduce MRVL to approximately half the portfolio's normal "
                "weighting by selling into the $290-300 zone."
            ),
            "风控：Set a hard stop at $244.",
            "仓位：",
            "催化剂：Nvidia partnership remains supportive.",
            "目标价：200.0",
            "时间窗口：3-6 months",
            (
                "理由：The bear asked what does MRVL actually earn, arguing "
                "that the current setup still implies a ~316x P/E, while "
                "MACD divergence and collapsing volume show technical exhaustion."
            ),
        ]
    )


def qqq_trim_summary() -> str:
    return "\n".join(
        [
            "评级：Underweight",
            "操作计划：Trim QQQ into strength near 550.",
            "风控：Use 510 as the main stop reference.",
            "仓位：",
            "催化剂：Fed commentary remains a swing factor.",
            "目标价：520.0",
            "时间窗口：1-3 months",
            (
                "理由：Risk/reward looks skewed after the recent squeeze, while "
                "hawkish Fed rhetoric keeps macro downside elevated."
            ),
        ]
    )


def msft_advice_summary_ascii_colons() -> str:
    return "\n".join(
        [
            "评级: Overweight",
            "操作计划: 在380-400美元区间分3-4次买入目标仓位的60%，350美元附近加仓剩余40%。",
            "风控: 统一停损线设在340美元。",
            "仓位: 总仓位控制在投资组合的8%-12%。",
            "催化剂: 10月底财报为关键催化剂。",
            "目标价: 450 / 500",
            "时间窗口: 3-6个月",
            "理由: 微软AI商业化路径清晰。",
        ]
    )


def test_build_trading_plan_extracts_structured_prices_and_writes_latest(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/latest/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "MSFT",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.13%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Overweight",
                "advice_summary": msft_advice_summary(),
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
            },
            {
                "run_date": "2026-06-16",
                "symbol": "BAD",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "0.1%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "",
                "advice_summary": "",
                "raw_decision": "",
                "status": "error",
                "error": "timeout",
            },
        ],
    )

    result = build_trading_plan(advice_path, tmp_path / "data")

    assert result == TradingPlanBuildResult(
        run_date="2026-06-16",
        plan_count=2,
        plan_path=tmp_path / "data/runs/2026-06-16/trading_plan.csv",
        latest_path=tmp_path / "data/latest/trading_plan.csv",
    )
    rows = list(csv.DictReader(result.plan_path.open(encoding="utf-8")))
    assert list(rows[0]) == TRADING_PLAN_FIELDNAMES
    assert rows[0]["symbol"] == "MSFT"
    assert rows[0]["rating"] == "Overweight"
    assert rows[0]["entry_zone_low"] == "380"
    assert rows[0]["entry_zone_high"] == "400"
    assert rows[0]["add_price"] == "350"
    assert rows[0]["stop_loss"] == "340"
    assert rows[0]["target_1"] == "450"
    assert rows[0]["target_2"] == "500"
    assert rows[0]["max_weight"] == "12%"
    assert rows[0]["catalyst"] == "10月底财报为关键催化剂。"
    assert rows[0]["time_horizon"] == "3-6个月"
    assert rows[0]["agent_reason"] == "微软AI商业化路径清晰。"
    assert rows[0]["agent_excerpt"] == "微软AI商业化路径清晰。"
    assert rows[0]["status"] == "active"
    assert rows[1]["symbol"] == "BAD"
    assert rows[1]["status"] == "error"
    assert rows[1]["error"] == "timeout"
    assert result.latest_path.read_text(encoding="utf-8") == result.plan_path.read_text(
        encoding="utf-8"
    )


def test_build_trading_plan_writes_market_scoped_hk_paths(tmp_path: Path) -> None:
    advice = tmp_path / "advice.csv"
    write_advice(
        advice,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "00700",
                "market": "HK",
                "advice_action": "Overweight",
                "advice_summary": msft_advice_summary(),
                "status": "ok",
                "error": "",
            }
        ],
    )

    result = build_trading_plan(
        advice,
        tmp_path / "data",
        run_date="2026-06-19",
        update_latest=True,
        market="HK",
    )
    rows = load_trading_plan_rows(result.plan_path)

    assert result.plan_path == tmp_path / "data/runs/2026-06-19/HK/trading_plan.csv"
    assert result.latest_path == tmp_path / "data/latest/HK/trading_plan.csv"
    assert rows[0].futu_symbol == "HK.00700"


@pytest.mark.parametrize("market", ["JP", "../HK", ""])
def test_build_trading_plan_rejects_invalid_market_before_writing(
    tmp_path: Path,
    market: str,
) -> None:
    advice = tmp_path / "advice.csv"
    data_dir = tmp_path / "data"
    write_advice(
        advice,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "00700",
                "market": "HK",
                "advice_action": "Overweight",
                "advice_summary": msft_advice_summary(),
                "status": "ok",
                "error": "",
            }
        ],
    )

    with pytest.raises(ValueError, match="market must be one of: HK, US, CN"):
        build_trading_plan(
            advice,
            data_dir,
            run_date="2026-06-19",
            update_latest=True,
            market=market,
        )

    assert not (data_dir / "runs").exists()
    assert not (data_dir / "latest").exists()


def test_build_trading_plan_dry_run_does_not_update_latest(tmp_path: Path) -> None:
    advice_path = tmp_path / "advice.csv"
    latest_path = tmp_path / "data/latest/trading_plan.csv"
    latest_path.parent.mkdir(parents=True)
    latest_path.write_text("old latest", encoding="utf-8")
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "MSFT",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.13%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Overweight",
                "advice_summary": msft_advice_summary(),
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
            }
        ],
    )

    result = build_trading_plan(
        advice_path,
        tmp_path / "data",
        run_date="2026-06-16",
        update_latest=False,
    )

    assert result.plan_path.exists()
    assert latest_path.read_text(encoding="utf-8") == "old latest"


def test_build_trading_plan_accepts_large_raw_decision_field(tmp_path: Path) -> None:
    advice_path = tmp_path / "advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "MSFT",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.13%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Overweight",
                "advice_summary": msft_advice_summary(),
                "raw_decision": "x" * 200_000,
                "status": "ok",
                "error": "",
            }
        ],
    )

    result = build_trading_plan(advice_path, tmp_path / "data")

    assert result.plan_count == 1


def test_build_trading_plan_accepts_fallback_advice_and_preserves_source_status(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-17",
                "symbol": "MSFT",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.13%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Overweight",
                "advice_summary": msft_advice_summary(),
                "raw_decision": "{}",
                "status": "fallback",
                "error": "",
                "source_status": "fallback",
                "fallback_reason": "daily deadline exceeded",
                "fallback_from_date": "2026-06-16",
            }
        ],
    )

    result = build_trading_plan(advice_path, tmp_path / "data")
    rows = list(csv.DictReader(result.plan_path.open(encoding="utf-8")))

    assert rows[0]["symbol"] == "MSFT"
    assert rows[0]["status"] == "active"
    assert rows[0]["source_status"] == "fallback"
    assert rows[0]["fallback_reason"] == "daily deadline exceeded"
    assert rows[0]["fallback_from_date"] == "2026-06-16"


def test_build_trading_plan_extracts_agent_reason_and_excerpt(tmp_path: Path) -> None:
    advice_path = tmp_path / "advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "MRVL",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "2.0%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": mrvl_underweight_summary(),
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
            }
        ],
    )

    result = build_trading_plan(advice_path, tmp_path / "data")
    rows = list(csv.DictReader(result.plan_path.open(encoding="utf-8")))

    assert rows[0]["target_1"] == "200"
    assert rows[0]["agent_reason"].startswith("TradingAgents建议减仓，理由是")
    assert "估值或盈利质量风险上升" in rows[0]["agent_reason"]
    assert "技术动能转弱" in rows[0]["agent_reason"]
    assert "理由见原文摘录" not in rows[0]["agent_reason"]
    assert "The bear demonstrated" not in rows[0]["agent_reason"]
    assert rows[0]["agent_excerpt"].startswith(
        "The bear asked what does MRVL actually earn"
    )
    assert "目标价：200.0" not in rows[0]["agent_reason"]


def test_build_trading_plan_english_reasons_map_to_different_chinese_themes(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "MRVL",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "2.0%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": mrvl_underweight_summary(),
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
            },
            {
                "run_date": "2026-06-18",
                "symbol": "QQQ",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "3.0%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": qqq_trim_summary(),
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
            },
        ],
    )

    result = build_trading_plan(advice_path, tmp_path / "data")
    rows = {row["symbol"]: row for row in csv.DictReader(result.plan_path.open(encoding="utf-8"))}

    assert rows["MRVL"]["agent_reason"] != rows["QQQ"]["agent_reason"]
    assert "估值或盈利质量风险上升" in rows["MRVL"]["agent_reason"]
    assert "技术动能转弱" in rows["MRVL"]["agent_reason"]
    assert "风险回报不利" in rows["QQQ"]["agent_reason"]
    assert "宏观或事件风险偏高" in rows["QQQ"]["agent_reason"]


def test_english_reason_themes_treats_earn_as_valuation_signal() -> None:
    themes = _english_reason_themes("What does MRVL actually earn?")

    assert "估值或盈利质量风险上升" in themes


def test_build_trading_plan_accepts_ascii_section_separators(tmp_path: Path) -> None:
    advice_path = tmp_path / "advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "MSFT",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.13%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Overweight",
                "advice_summary": msft_advice_summary_ascii_colons(),
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
            }
        ],
    )

    result = build_trading_plan(advice_path, tmp_path / "data")
    rows = list(csv.DictReader(result.plan_path.open(encoding="utf-8")))

    assert rows[0]["status"] == "active"
    assert rows[0]["rating"] == "Overweight"
    assert rows[0]["target_1"] == "450"
    assert rows[0]["target_2"] == "500"
    assert rows[0]["agent_reason"] == "微软AI商业化路径清晰。"
    assert rows[0]["agent_excerpt"] == "微软AI商业化路径清晰。"


def test_load_trading_plan_rows_reads_active_rows(tmp_path: Path) -> None:
    path = tmp_path / "trading_plan.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADING_PLAN_FIELDNAMES)
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "MSFT",
                "market": "US",
                "rating": "Overweight",
                "entry_zone_low": "380",
                "entry_zone_high": "400",
                "add_price": "350",
                "stop_loss": "340",
                "target_1": "450",
                "target_2": "500",
                "max_weight": "12%",
                "catalyst": "10月底财报",
                "time_horizon": "3-6个月",
                "plan_text": "plan",
                "agent_reason": "agent reason",
                "agent_excerpt": "agent excerpt",
                "status": "active",
                "error": "",
            }
        )

    assert load_trading_plan_rows(path) == [
        TradingPlanRow(
            run_date="2026-06-16",
            symbol="MSFT",
            market="US",
            source_status="ok",
            fallback_reason="",
            fallback_from_date="",
            rating="Overweight",
            entry_zone_low=Decimal("380"),
            entry_zone_high=Decimal("400"),
            add_price=Decimal("350"),
            stop_loss=Decimal("340"),
            target_1=Decimal("450"),
            target_2=Decimal("500"),
            max_weight="12%",
            catalyst="10月底财报",
            time_horizon="3-6个月",
            plan_text="plan",
            agent_reason="agent reason",
            agent_excerpt="agent excerpt",
            status="active",
            error="",
        )
    ]


def test_load_trading_plan_rows_accepts_legacy_rows_without_source_status(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy_plan.csv"
    legacy_fieldnames = [
        field
        for field in TRADING_PLAN_FIELDNAMES
        if field
        not in {
            "source_status",
            "fallback_reason",
            "fallback_from_date",
            "agent_reason",
            "agent_excerpt",
        }
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=legacy_fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "MSFT",
                "market": "US",
                "rating": "Overweight",
                "entry_zone_low": "380",
                "entry_zone_high": "400",
                "add_price": "350",
                "stop_loss": "340",
                "target_1": "450",
                "target_2": "500",
                "max_weight": "12%",
                "catalyst": "10月底财报",
                "time_horizon": "3-6个月",
                "plan_text": "plan",
                "status": "active",
                "error": "",
            }
        )

    rows = load_trading_plan_rows(path)

    assert rows[0].source_status == "ok"
    assert rows[0].fallback_reason == ""
    assert rows[0].fallback_from_date == ""
    assert rows[0].agent_reason == ""
    assert rows[0].agent_excerpt == ""


def test_trading_plan_row_defaults_agent_fields() -> None:
    row = TradingPlanRow(
        run_date="2026-06-16",
        symbol="MSFT",
        market="US",
        source_status="ok",
        fallback_reason="",
        fallback_from_date="",
        rating="Overweight",
        entry_zone_low=Decimal("380"),
        entry_zone_high=Decimal("400"),
        add_price=Decimal("350"),
        stop_loss=Decimal("340"),
        target_1=Decimal("450"),
        target_2=Decimal("500"),
        max_weight="12%",
        catalyst="10月底财报",
        time_horizon="3-6个月",
        plan_text="plan",
        status="active",
        error="",
    )

    assert row.agent_reason == ""
    assert row.agent_excerpt == ""


def test_trading_plan_row_preserves_old_positional_constructor_order() -> None:
    row = TradingPlanRow(
        "2026-06-16",
        "MSFT",
        "US",
        "ok",
        "",
        "",
        "Overweight",
        Decimal("380"),
        Decimal("400"),
        Decimal("350"),
        Decimal("340"),
        Decimal("450"),
        Decimal("500"),
        "12%",
        "10月底财报",
        "3-6个月",
        "plan",
        "active",
        "",
    )

    assert row.status == "active"
    assert row.error == ""
    assert row.agent_reason == ""
    assert row.agent_excerpt == ""


def test_evaluate_plan_quote_classifies_current_price() -> None:
    plan = TradingPlanRow(
        run_date="2026-06-16",
        symbol="MSFT",
        market="US",
        source_status="ok",
        fallback_reason="",
        fallback_from_date="",
        rating="Overweight",
        entry_zone_low=Decimal("380"),
        entry_zone_high=Decimal("400"),
        add_price=Decimal("350"),
        stop_loss=Decimal("340"),
        target_1=Decimal("450"),
        target_2=Decimal("500"),
        max_weight="12%",
        catalyst="10月底财报",
        time_horizon="3-6个月",
        plan_text="plan",
        agent_reason="",
        agent_excerpt="",
        status="active",
        error="",
    )

    assert evaluate_plan_quote(plan, Decimal("339")).status == "stop_loss_hit"
    assert evaluate_plan_quote(plan, Decimal("399")).status == "entry_zone"
    assert evaluate_plan_quote(plan, Decimal("351")).status == "add_zone"
    assert evaluate_plan_quote(plan, Decimal("451")).status == "target_1_hit"
    assert evaluate_plan_quote(plan, Decimal("501")).status == "target_2_hit"
    assert evaluate_plan_quote(plan, Decimal("420")) == PlanQuoteStatus(
        symbol="MSFT",
        futu_symbol="US.MSFT",
        last_price=Decimal("420"),
        status="watch",
        message="No plan trigger is active.",
    )
