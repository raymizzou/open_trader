from decimal import Decimal

from open_trader.models import AssetClass, CashBalance, Market, Position, WarningRecord


def test_position_identity_key_merges_by_market_asset_symbol_currency():
    position = Position(
        statement_id="2026-05-futu",
        broker="futu",
        account_alias="futu_main",
        market=Market.US,
        asset_class=AssetClass.STOCK,
        symbol="NVDA",
        name="NVIDIA",
        currency="USD",
        quantity=Decimal("10"),
        cost_price=Decimal("120"),
        last_price=Decimal("130"),
        market_value=Decimal("1300"),
        cost_value=Decimal("1200"),
        unrealized_pnl=Decimal("100"),
        confidence="high",
        notes="",
    )

    assert position.identity_key() == (Market.US, AssetClass.STOCK, "NVDA", "USD")


def test_cash_balance_uses_synthetic_symbol():
    cash = CashBalance(
        statement_id="2026-05-tiger",
        broker="tiger",
        account_alias="tiger_main",
        currency="USD",
        cash_balance=Decimal("1000"),
        available_balance=Decimal("900"),
        confidence="high",
        notes="",
    )

    assert cash.symbol == "USD_CASH"
    assert cash.market == Market.CASH
    assert cash.asset_class == AssetClass.CASH


def test_warning_record_has_stable_csv_fields():
    warning = WarningRecord(
        statement_id="2026-05-phillips",
        broker="phillips",
        page=2,
        severity="warning",
        code="missing_cost",
        message="Missing cost value for 00001",
    )

    assert warning.to_row()["code"] == "missing_cost"
    assert warning.to_row()["page"] == "2"
