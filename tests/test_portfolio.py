from decimal import Decimal

from open_trader.fx import StaticMonthEndFxProvider
from open_trader.models import AssetClass, CashBalance, Market, Position
from open_trader.portfolio import build_portfolio_rows


def position(
    broker: str,
    symbol: str,
    quantity: str,
    cost_value: str,
    market_value: str,
    *,
    market: Market = Market.US,
    asset_class: AssetClass = AssetClass.STOCK,
    currency: str = "USD",
) -> Position:
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
        market_value=Decimal(market_value),
        cost_value=Decimal(cost_value),
        unrealized_pnl=Decimal(market_value) - Decimal(cost_value),
        confidence="high",
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
