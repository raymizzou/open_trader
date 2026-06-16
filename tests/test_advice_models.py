from __future__ import annotations

from open_trader.advice.models import (
    CHANGE_CLASSIFICATION_FIELDNAMES,
    PREMARKET_ACTION_FIELDNAMES,
    TRADING_ADVICE_FIELDNAMES,
    ChangeClassification,
    PortfolioInputRow,
    PremarketAction,
    TradingAdvice,
)


EXPECTED_TRADING_ADVICE_FIELDNAMES = [
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

EXPECTED_CHANGE_CLASSIFICATION_FIELDNAMES = [
    "run_date",
    "symbol",
    "include_in_report",
    "change_type",
    "severity",
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
    "status",
    "error",
]

EXPECTED_PREMARKET_ACTION_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "portfolio_weight_hkd",
    "severity",
    "change_type",
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
]


def test_csv_fieldnames_match_external_contract() -> None:
    assert TRADING_ADVICE_FIELDNAMES == EXPECTED_TRADING_ADVICE_FIELDNAMES
    assert CHANGE_CLASSIFICATION_FIELDNAMES == EXPECTED_CHANGE_CLASSIFICATION_FIELDNAMES
    assert PREMARKET_ACTION_FIELDNAMES == EXPECTED_PREMARKET_ACTION_FIELDNAMES


def test_trading_advice_to_row_has_stable_csv_fields() -> None:
    advice = TradingAdvice(
        run_date="2026-06-16",
        symbol="VIXY",
        market="US",
        asset_class="etf",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        source="tradingagents",
        advice_action="reduce",
        advice_summary="Trim volatility ETF exposure.",
        raw_decision='{"action":"reduce"}',
        status="ok",
        error="",
    )

    row = advice.to_row()

    assert list(row) == EXPECTED_TRADING_ADVICE_FIELDNAMES
    assert row["symbol"] == "VIXY"
    assert row["advice_action"] == "reduce"
    assert row["error"] == ""


def test_trading_advice_row_includes_fallback_metadata() -> None:
    advice = TradingAdvice(
        run_date="2026-06-17",
        symbol="MSFT",
        market="US",
        asset_class="stock",
        portfolio_weight_hkd="1.13%",
        risk_flag="normal",
        source="tradingagents",
        advice_action="Overweight",
        advice_summary="评级：Overweight",
        raw_decision="{}",
        status="fallback",
        error="",
        source_status="fallback",
        fallback_reason="daily deadline exceeded",
        fallback_from_date="2026-06-16",
    )

    row = advice.to_row()

    assert "source_status" in TRADING_ADVICE_FIELDNAMES
    assert "fallback_reason" in TRADING_ADVICE_FIELDNAMES
    assert "fallback_from_date" in TRADING_ADVICE_FIELDNAMES
    assert row["status"] == "fallback"
    assert row["source_status"] == "fallback"
    assert row["fallback_reason"] == "daily deadline exceeded"
    assert row["fallback_from_date"] == "2026-06-16"


def test_change_classification_to_row_has_required_fields() -> None:
    classification = ChangeClassification(
        run_date="2026-06-16",
        symbol="VIXY",
        include_in_report=True,
        change_type="action_changed",
        severity="high",
        suggested_action="reduce",
        summary="VIXY changed from hold to reduce.",
        rationale="The latest advice materially lowers risk appetite.",
        watch_trigger="Open below prior close.",
        status="ok",
        error="",
    )

    row = classification.to_row()

    assert list(row) == EXPECTED_CHANGE_CLASSIFICATION_FIELDNAMES
    assert row["include_in_report"] == "true"
    assert row["severity"] == "high"


def test_premarket_action_is_derived_from_portfolio_and_classification() -> None:
    portfolio = PortfolioInputRow(
        symbol="VIXY",
        market="US",
        asset_class="etf",
        name="Volatility ETF",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        analysis_symbol="VIXY",
    )
    classification = ChangeClassification(
        run_date="2026-06-16",
        symbol="VIXY",
        include_in_report=True,
        change_type="action_changed",
        severity="high",
        suggested_action="reduce",
        summary="VIXY changed from hold to reduce.",
        rationale="The latest advice materially lowers risk appetite.",
        watch_trigger="Open below prior close.",
        status="ok",
        error="",
    )

    action = PremarketAction.from_classification(portfolio, classification)

    assert list(action.to_row()) == EXPECTED_PREMARKET_ACTION_FIELDNAMES
    assert action.symbol == "VIXY"
    assert action.market == "US"
    assert action.portfolio_weight_hkd == "3.05%"
