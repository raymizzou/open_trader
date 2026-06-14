from decimal import Decimal

import pytest

from open_trader.fx import StaticMonthEndFxProvider
from open_trader.models import AssetClass, CashBalance, Market, Position
from open_trader.portfolio import build_portfolio_rows


def position(
    broker: str,
    symbol: str,
    quantity: str,
    cost_value: str | None,
    market_value: str | None,
    *,
    market: Market = Market.US,
    asset_class: AssetClass = AssetClass.STOCK,
    currency: str = "USD",
    confidence: str = "high",
    unrealized_pnl: str | None = None,
) -> Position:
    cost_value_decimal = None if cost_value is None else Decimal(cost_value)
    market_value_decimal = None if market_value is None else Decimal(market_value)
    unrealized_pnl_decimal = None if unrealized_pnl is None else Decimal(unrealized_pnl)
    return Position(
        statement_id=f"2026-05-{broker}",
        broker=broker,
        account_alias=f"{broker}_main",
        market=market,
        asset_class=asset_class,
        symbol=symbol,
        name=symbol,
        currency=currency,
        quantity=Decimal(quantity),
        cost_price=None,
        last_price=None,
        market_value=market_value_decimal,
        cost_value=cost_value_decimal,
        unrealized_pnl=unrealized_pnl_decimal,
        confidence=confidence,
        notes="",
    )


def test_build_portfolio_rows_merges_same_us_symbol_across_brokers():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    positions = [
        position("futu", "NVDA", "10", "1000", "1300"),
        position("tiger", "NVDA", "5", "600", "700"),
    ]

    rows = build_portfolio_rows("2026-05", positions, [], fx)

    nvda = rows[0]
    assert nvda["symbol"] == "NVDA"
    assert nvda["total_quantity"] == "15"
    assert nvda["market_value"] == "2000"
    assert nvda["cost_value"] == "1600"
    assert nvda["market_value_hkd"] == "15600.00"
    assert nvda["brokers"] == "futu;tiger"
    assert nvda["ai_eligible"] == "true"
    assert nvda["analysis_symbol"] == "NVDA"


def test_cash_is_included_in_weight_denominator_but_not_overweight():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    positions = [position("futu", "NVDA", "10", "1000", "2000")]
    cash = [
        CashBalance(
            statement_id="2026-05-futu",
            broker="futu",
            account_alias="futu_main",
            currency="USD",
            cash_balance=Decimal("18000"),
            available_balance=Decimal("18000"),
            confidence="high",
            notes="",
        )
    ]

    rows = build_portfolio_rows("2026-05", positions, cash, fx)

    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    usd_cash = next(row for row in rows if row["symbol"] == "USD_CASH")
    assert nvda["portfolio_weight_hkd"] == "10.00%"
    assert nvda["risk_flag"] == "normal"
    assert usd_cash["risk_flag"] == "normal"


def test_build_portfolio_rows_merges_same_cash_symbol_across_brokers():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    cash = [
        CashBalance(
            statement_id="2026-05-futu",
            broker="futu",
            account_alias="futu_main",
            currency="USD",
            cash_balance=Decimal("1000"),
            available_balance=Decimal("1000"),
            confidence="high",
            notes="",
        ),
        CashBalance(
            statement_id="2026-05-tiger",
            broker="tiger",
            account_alias="tiger_main",
            currency="USD",
            cash_balance=Decimal("2000"),
            available_balance=Decimal("2000"),
            confidence="medium",
            notes="",
        ),
    ]

    rows = build_portfolio_rows("2026-05", [], cash, fx)

    assert len(rows) == 1
    usd_cash = rows[0]
    assert usd_cash["symbol"] == "USD_CASH"
    assert usd_cash["market_value"] == "3000"
    assert usd_cash["market_value_hkd"] == "23400.00"
    assert usd_cash["brokers"] == "futu;tiger"
    assert usd_cash["accounts"] == "futu_main;tiger_main"
    assert usd_cash["confidence"] == "medium"


def test_zero_cost_value_is_preserved_and_not_missing():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    rows = build_portfolio_rows(
        "2026-05",
        [position("futu", "TSLA", "10", "0", "100")],
        [],
        fx,
    )

    tsla = rows[0]
    assert tsla["cost_value"] == "0"
    assert tsla["cost_value_hkd"] == "0.00"
    assert tsla["risk_flag"] == "overweight"


def test_zero_market_value_remains_normal_when_fields_are_present():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    rows = build_portfolio_rows(
        "2026-05",
        [position("futu", "FLAT", "10", "0", "0")],
        [],
        fx,
    )

    flat = rows[0]
    assert flat["market_value"] == "0"
    assert flat["market_value_hkd"] == "0.00"
    assert flat["risk_flag"] == "normal"


def test_partial_missing_position_data_marks_merged_row_data_check():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    rows = build_portfolio_rows(
        "2026-05",
        [
            position("futu", "NVDA", "10", "1000", "2000"),
            position("tiger", "NVDA", "5", None, None),
        ],
        [],
        fx,
    )

    nvda = rows[0]
    assert nvda["avg_cost_price"] == ""
    assert nvda["last_price"] == ""
    assert nvda["risk_flag"] == "data_check"


def test_missing_position_values_blank_group_totals_and_hkd_totals():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    rows = build_portfolio_rows(
        "2026-05",
        [
            position("futu", "NVDA", "10", "1000", "2000"),
            position("tiger", "NVDA", "5", None, None),
            position("futu", "AAPL", "10", "1000", "2000"),
            position("tiger", "AAPL", "5", None, "500"),
            position("futu", "MSFT", "10", "1000", "0"),
        ],
        [],
        fx,
    )

    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    assert nvda["market_value"] == ""
    assert nvda["market_value_hkd"] == ""
    assert nvda["cost_value"] == ""
    assert nvda["cost_value_hkd"] == ""

    aapl = next(row for row in rows if row["symbol"] == "AAPL")
    assert aapl["market_value"] == "2500"
    assert aapl["market_value_hkd"] == "19500.00"
    assert aapl["cost_value"] == ""
    assert aapl["cost_value_hkd"] == ""

    msft = next(row for row in rows if row["symbol"] == "MSFT")
    assert msft["market_value"] == "0"
    assert msft["market_value_hkd"] == "0.00"
    assert msft["cost_value"] == "1000"
    assert msft["cost_value_hkd"] == "7800.00"


def test_build_portfolio_rows_rejects_mismatched_fx_provider_month():
    fx = StaticMonthEndFxProvider("2026-04", {"USD": Decimal("7.8")})

    with pytest.raises(ValueError, match="month.*2026-05.*fx_provider.month.*2026-04"):
        build_portfolio_rows(
            "2026-05",
            [position("futu", "NVDA", "10", "1000", "2000")],
            [],
            fx,
        )


def test_data_check_beats_overweight_for_partial_data_rows():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    rows = build_portfolio_rows(
        "2026-05",
        [
            position("futu", "AAPL", "10", "1000", "3000"),
            position("tiger", "AAPL", "5", None, None),
        ],
        [
            CashBalance(
                statement_id="2026-05-futu",
                broker="futu",
                account_alias="futu_main",
                currency="USD",
                cash_balance=Decimal("1000"),
                available_balance=Decimal("1000"),
                confidence="high",
                notes="",
            )
        ],
        fx,
    )

    aapl = next(row for row in rows if row["symbol"] == "AAPL")
    assert aapl["portfolio_weight_hkd"] == ""
    assert aapl["risk_flag"] == "data_check"


def test_missing_position_valuation_blanks_all_portfolio_weights_and_data_checks_all_rows():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    rows = build_portfolio_rows(
        "2026-05",
        [
            position("futu", "AAPL", "10", "1000", "3000"),
            position("tiger", "BROKEN", "5", "500", None),
        ],
        [
            CashBalance(
                statement_id="2026-05-futu",
                broker="futu",
                account_alias="futu_main",
                currency="USD",
                cash_balance=Decimal("1000"),
                available_balance=Decimal("1000"),
                confidence="high",
                notes="",
            )
        ],
        fx,
    )

    assert {row["portfolio_weight_hkd"] for row in rows} == {""}
    assert {row["risk_flag"] for row in rows} == {"data_check"}


def test_merged_rows_use_broker_unrealized_pnl_when_all_positions_provide_it():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    rows = build_portfolio_rows(
        "2026-05",
        [
            position("futu", "NVDA", "10", "1000", "1300", unrealized_pnl="250"),
            position("tiger", "NVDA", "5", "600", "700", unrealized_pnl="100"),
        ],
        [
            CashBalance(
                statement_id="2026-05-futu",
                broker="futu",
                account_alias="futu_main",
                currency="USD",
                cash_balance=Decimal("18000"),
                available_balance=Decimal("18000"),
                confidence="high",
                notes="",
            )
        ],
        fx,
    )

    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    assert nvda["unrealized_pnl"] == "350.00"
    assert nvda["unrealized_pnl_pct"] == "21.88%"


def test_merged_rows_fall_back_to_computed_pnl_when_any_broker_pnl_is_missing():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    rows = build_portfolio_rows(
        "2026-05",
        [
            position("futu", "NVDA", "10", "1000", "1300", unrealized_pnl="250"),
            position("tiger", "NVDA", "5", "600", "700", unrealized_pnl=None),
        ],
        [
            CashBalance(
                statement_id="2026-05-futu",
                broker="futu",
                account_alias="futu_main",
                currency="USD",
                cash_balance=Decimal("18000"),
                available_balance=Decimal("18000"),
                confidence="high",
                notes="",
            )
        ],
        fx,
    )

    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    assert nvda["unrealized_pnl"] == "400.00"
    assert nvda["unrealized_pnl_pct"] == "25.00%"


def test_build_portfolio_rows_sorts_by_group_then_market_value_hkd_desc():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8"), "EUR": Decimal("8.5")})
    rows = build_portfolio_rows(
        "2026-05",
        [
            position("futu", "MSFT", "10", "100", "600", asset_class=AssetClass.ETF),
            position("futu", "BABA", "10", "100", "500", asset_class=AssetClass.FUND),
            position("futu", "0700", "10", "100", "400", market=Market.HK, currency="HKD"),
            position("futu", "FUNDX", "10", "100", "300", market=Market.OTHER, currency="EUR"),
        ],
        [
            CashBalance(
                statement_id="2026-05-futu",
                broker="futu",
                account_alias="futu_main",
                currency="USD",
                cash_balance=Decimal("200"),
                available_balance=Decimal("200"),
                confidence="high",
                notes="",
            )
        ],
        fx,
    )

    assert [row["symbol"] for row in rows] == ["MSFT", "BABA", "0700", "FUNDX", "USD_CASH"]


def test_non_cash_position_over_ten_percent_is_overweight():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    positions = [position("futu", "NVDA", "10", "1000", "2000")]
    cash = [
        CashBalance(
            statement_id="2026-05-futu",
            broker="futu",
            account_alias="futu_main",
            currency="USD",
            cash_balance=Decimal("1000"),
            available_balance=Decimal("1000"),
            confidence="high",
            notes="",
        )
    ]

    rows = build_portfolio_rows("2026-05", positions, cash, fx)

    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    assert nvda["portfolio_weight_hkd"] == "66.67%"
    assert nvda["risk_flag"] == "overweight"


def test_money_market_fund_is_not_overweight():
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    positions = [
        position(
            "futu",
            "HK0000584752",
            "1000",
            "1000",
            "2000",
            market=Market.OTHER,
            asset_class=AssetClass.MONEY_MARKET_FUND,
        )
    ]

    rows = build_portfolio_rows("2026-05", positions, [], fx)

    fund = rows[0]
    assert fund["portfolio_weight_hkd"] == "100.00%"
    assert fund["risk_flag"] == "normal"
