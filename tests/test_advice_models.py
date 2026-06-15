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

    assert list(row) == TRADING_ADVICE_FIELDNAMES
    assert row["symbol"] == "VIXY"
    assert row["advice_action"] == "reduce"
    assert row["error"] == ""


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

    assert list(row) == CHANGE_CLASSIFICATION_FIELDNAMES
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

    assert list(action.to_row()) == PREMARKET_ACTION_FIELDNAMES
    assert action.symbol == "VIXY"
    assert action.market == "US"
    assert action.portfolio_weight_hkd == "3.05%"
