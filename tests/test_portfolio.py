from decimal import Decimal

import pytest

from open_trader.fx import StaticMonthEndFxProvider
from open_trader.models import AssetClass, CashBalance, Market, Position
import open_trader.portfolio as portfolio
from open_trader.portfolio import PORTFOLIO_FIELDNAMES, build_portfolio_rows


def portfolio_row(**overrides: str) -> dict[str, str]:
    row = {field: "" for field in PORTFOLIO_FIELDNAMES}
    row.update({
        "sort_group": "2", "market": "US", "asset_class": "stock", "symbol": "AAPL",
        "currency": "USD", "market_value": "10", "cost_value": "8", "fx_to_hkd": "7.8",
        "market_value_hkd": "100.00", "cost_value_hkd": "1.00", "unrealized_pnl": "999.00",
        "unrealized_pnl_pct": "999.00%", "portfolio_weight_hkd": "99.99%",
        "brokers": "futu", "risk_flag": "normal",
    })
    row.update(overrides)
    return row


def test_merge_eastmoney_rows_preserves_other_brokers_and_recalculates_weights() -> None:
    preserved = [
        portfolio_row(symbol="AAPL", brokers="futu", market_value="10", cost_value="8"),
        portfolio_row(symbol="TSLA", brokers="tiger", market_value="20", cost_value="10"),
        portfolio_row(symbol="NVDA", brokers="phillips", market_value="30", cost_value="25"),
        portfolio_row(market="CASH", asset_class="cash", symbol="HKD_CASH", currency="HKD", brokers="futu", market_value="400", fx_to_hkd="1", cost_value="", market_value_hkd="stale"),
        portfolio_row(market="CN", symbol="600519", currency="CNY", brokers="eastmoney", market_value_hkd="1.00"),
    ]
    new = [portfolio_row(market="CN", symbol="000001", currency="CNY", brokers="eastmoney", market_value="1000", fx_to_hkd="1.08")]

    rows = portfolio.merge_eastmoney_portfolio_rows(preserved, new)

    assert list(rows[0]) == PORTFOLIO_FIELDNAMES
    assert {row["symbol"] for row in rows} == {"AAPL", "TSLA", "NVDA", "HKD_CASH", "000001"}
    aapl = next(row for row in rows if row["symbol"] == "AAPL")
    assert (aapl["market"], aapl["symbol"], aapl["brokers"]) == ("US", "AAPL", "futu")
    assert aapl["market_value_hkd"] == "78.00"
    assert aapl["cost_value_hkd"] == "62.40"
    assert aapl["unrealized_pnl"] == "2.00"
    assert aapl["unrealized_pnl_pct"] == "25.00%"
    cash = next(row for row in rows if row["symbol"] == "HKD_CASH")
    assert cash["market_value_hkd"] == "400.00"
    assert cash["cost_value_hkd"] == cash["unrealized_pnl"] == cash["unrealized_pnl_pct"] == ""
    assert sum(Decimal(row["market_value_hkd"]) for row in rows) == Decimal("1948.00")
    assert sum(Decimal(row["portfolio_weight_hkd"].rstrip("%")) for row in rows) == Decimal("100.00")


def test_merge_eastmoney_rows_rejects_mixed_broker_row() -> None:
    with pytest.raises(ValueError, match="mixes Eastmoney"):
        portfolio.merge_eastmoney_portfolio_rows([portfolio_row(brokers="futu;eastmoney")], [])


def test_merge_eastmoney_weights_keep_two_decimal_total_at_one_hundred_percent() -> None:
    rows = portfolio.merge_eastmoney_portfolio_rows(
        [portfolio_row(symbol="A", market_value_hkd="1"), portfolio_row(symbol="B", market_value_hkd="1")],
        [portfolio_row(symbol="C", brokers="eastmoney", market_value_hkd="1")],
    )
    assert sum(Decimal(row["portfolio_weight_hkd"].rstrip("%")) for row in rows) == Decimal("100.00")


def test_merge_eastmoney_preserves_valid_non_eastmoney_risk_flag() -> None:
    rows = portfolio.merge_eastmoney_portfolio_rows(
        [portfolio_row(symbol="A", market_value="1", cost_value="0.5", fx_to_hkd="1", risk_flag="overweight")],
        [portfolio_row(symbol="B", brokers="eastmoney", market_value="99", cost_value="50", fx_to_hkd="1")],
    )
    preserved = next(row for row in rows if row["symbol"] == "A")
    assert preserved["portfolio_weight_hkd"] == "1.00%"
    assert preserved["market_value_hkd"] == "1.00"
    assert preserved["unrealized_pnl"] == "0.50"
    assert preserved["risk_flag"] == "overweight"


def test_merge_eastmoney_clears_missing_cost_derived_values_and_marks_data_check() -> None:
    row = portfolio_row(
        cost_value="",
        avg_cost_price="stale",
        cost_value_hkd="stale",
        unrealized_pnl="stale",
        unrealized_pnl_pct="stale",
    )
    merged = portfolio.merge_eastmoney_portfolio_rows([row], [])[0]
    assert (
        merged["avg_cost_price"]
        == merged["cost_value_hkd"]
        == merged["unrealized_pnl"]
        == merged["unrealized_pnl_pct"]
        == ""
    )
    assert merged["risk_flag"] == "data_check"


@pytest.mark.parametrize("market_values", [("0", "0"), ("-2", "1")])
def test_merge_eastmoney_rejects_non_positive_combined_total(market_values: tuple[str, str]) -> None:
    with pytest.raises(ValueError, match="combined HKD total"):
        portfolio.merge_eastmoney_portfolio_rows(
            [portfolio_row(symbol="A", market_value=market_values[0], fx_to_hkd="1")],
            [portfolio_row(symbol="B", brokers="eastmoney", market_value=market_values[1], fx_to_hkd="1")],
        )


def test_merge_eastmoney_rows_rejects_preserved_new_identity_collision() -> None:
    with pytest.raises(ValueError, match="identity collision"):
        portfolio.merge_eastmoney_portfolio_rows(
            [portfolio_row(symbol="600519", market="CN", currency="CNY", brokers="futu")],
            [portfolio_row(symbol="600519", market="CN", currency="CNY", brokers="eastmoney")],
        )


@pytest.mark.parametrize("value", ["", "NaN", "Infinity"])
def test_merge_eastmoney_rows_rejects_invalid_market_values(value: str) -> None:
    with pytest.raises(ValueError, match="market_value"):
        portfolio.merge_eastmoney_portfolio_rows([portfolio_row(market_value=value)], [])


def test_merge_eastmoney_rows_rejects_missing_non_hkd_fx() -> None:
    with pytest.raises(ValueError, match="fx_to_hkd"):
        portfolio.merge_eastmoney_portfolio_rows(
            [portfolio_row(market="CN", currency="CNY", fx_to_hkd="")], []
        )


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


def test_build_portfolio_rows_merges_same_symbol_when_one_asset_class_is_unknown():
    fx = StaticMonthEndFxProvider("2026-06", {"HKD": Decimal("1")})
    positions = [
        position(
            "tiger",
            "01688",
            "2640",
            "26875.2",
            "25634.4",
            market=Market.HK,
            asset_class=AssetClass.STOCK,
            currency="HKD",
            unrealized_pnl="-1240.8",
        ),
        position(
            "futu",
            "01688",
            "0",
            "0",
            "0",
            market=Market.HK,
            asset_class=AssetClass.UNKNOWN,
            currency="HKD",
            unrealized_pnl="-277.2",
        ),
    ]

    rows = build_portfolio_rows("2026-06", positions, [], fx)

    assert len([row for row in rows if row["symbol"] == "01688"]) == 1
    row = next(row for row in rows if row["symbol"] == "01688")
    assert row["market"] == "HK"
    assert row["asset_class"] == "stock"
    assert row["total_quantity"] == "2640"
    assert row["market_value"] == "25634.4"
    assert row["cost_value"] == "26875.2"
    assert row["market_value_hkd"] == "25634.40"
    assert row["brokers"] == "futu;tiger"
    assert row["accounts"] == "futu_main;tiger_main"
    assert row["ai_eligible"] == "true"
    assert row["analysis_symbol"] == "01688"


def test_build_portfolio_rows_rejects_conflicting_known_asset_classes_for_same_identity():
    fx = StaticMonthEndFxProvider("2026-06", {"HKD": Decimal("1")})

    with pytest.raises(
        ValueError,
        match=r"conflicting asset classes for HK\.01688: etf, stock",
    ):
        build_portfolio_rows(
            "2026-06",
            [
                position(
                    "tiger",
                    "01688",
                    "10",
                    "100",
                    "120",
                    market=Market.HK,
                    asset_class=AssetClass.STOCK,
                    currency="HKD",
                ),
                position(
                    "futu",
                    "01688",
                    "5",
                    "50",
                    "60",
                    market=Market.HK,
                    asset_class=AssetClass.ETF,
                    currency="HKD",
                ),
            ],
            [],
            fx,
        )


def test_build_portfolio_rows_rejects_same_symbol_with_multiple_currencies():
    fx = StaticMonthEndFxProvider(
        "2026-06",
        {"HKD": Decimal("1"), "USD": Decimal("7.8")},
    )

    with pytest.raises(
        ValueError,
        match=r"conflicting currencies for HK\.01688: HKD, USD",
    ):
        build_portfolio_rows(
            "2026-06",
            [
                position(
                    "tiger",
                    "01688",
                    "10",
                    "100",
                    "120",
                    market=Market.HK,
                    asset_class=AssetClass.STOCK,
                    currency="HKD",
                ),
                position(
                    "futu",
                    "01688",
                    "5",
                    "50",
                    "60",
                    market=Market.HK,
                    asset_class=AssetClass.STOCK,
                    currency="USD",
                ),
            ],
            [],
            fx,
        )


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

    assert [row["symbol"] for row in rows] == ["0700", "MSFT", "BABA", "FUNDX", "USD_CASH"]
    assert {row["symbol"]: row["sort_group"] for row in rows} == {
        "0700": "1",
        "MSFT": "2",
        "BABA": "4",
        "FUNDX": "6",
        "USD_CASH": "7",
    }


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


def test_hk_stock_and_etf_are_ai_eligible() -> None:
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    positions = [
        position(
            "futu",
            "00700",
            "100",
            "35000",
            "38000",
            market=Market.HK,
            currency="HKD",
        ),
        position(
            "futu",
            "02800",
            "200",
            "40000",
            "42000",
            market=Market.HK,
            asset_class=AssetClass.ETF,
            currency="HKD",
        ),
    ]

    rows = build_portfolio_rows("2026-05", positions, [], fx)

    tencent = next(row for row in rows if row["symbol"] == "00700")
    tracker = next(row for row in rows if row["symbol"] == "02800")
    assert tencent["market"] == "HK"
    assert tencent["currency"] == "HKD"
    assert tencent["ai_eligible"] == "true"
    assert tencent["analysis_symbol"] == "00700"
    assert tracker["ai_eligible"] == "true"
    assert tracker["analysis_symbol"] == "02800"


def test_cn_stock_is_strategy_eligible() -> None:
    cn_position = position(
        "eastmoney",
        "600025",
        "6000",
        "53346",
        "57720",
        market=Market.CN,
        asset_class=AssetClass.STOCK,
        currency="CNY",
    )
    rows = build_portfolio_rows(
        "2026-07",
        [cn_position],
        [],
        StaticMonthEndFxProvider("2026-07", {"CNY": Decimal("1.08")}),
    )
    assert rows[0]["market"] == "CN"
    assert rows[0]["ai_eligible"] == "true"
    assert rows[0]["analysis_symbol"] == "600025"


def test_hk_money_market_fund_stays_ai_ineligible() -> None:
    fx = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})
    rows = build_portfolio_rows(
        "2026-05",
        [
            position(
                "futu",
                "HK0000951506.HKD",
                "100",
                "100",
                "100",
                market=Market.HK,
                asset_class=AssetClass.MONEY_MARKET_FUND,
                currency="HKD",
            )
        ],
        [],
        fx,
    )

    fund = rows[0]
    assert fund["market"] == "HK"
    assert fund["ai_eligible"] == "false"
    assert fund["analysis_symbol"] == ""
