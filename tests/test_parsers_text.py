from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from open_trader.models import AssetClass, Market
from open_trader.parsers.futu import parse_futu_text
from open_trader.parsers.phillips import parse_phillips_text
from open_trader.parsers.tiger import parse_tiger_text


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pdf_text"


def test_parse_futu_text_extracts_positions_and_cash() -> None:
    result = parse_futu_text(
        FIXTURE_DIR.joinpath("futu.txt").read_text(encoding="utf-8"), "2026-05"
    )

    assert result.statement_id == "2026-05-futu"
    assert result.broker == "futu"
    assert len(result.positions) == 3
    assert len(result.cash_balances) == 2

    nvda = next(position for position in result.positions if position.symbol == "NVDA")
    assert nvda.market == Market.US
    assert nvda.asset_class == AssetClass.STOCK
    assert nvda.quantity == Decimal("10")
    assert nvda.last_price == Decimal("130.00")
    assert nvda.market_value == Decimal("1300.00")
    assert nvda.cost_value is None
    assert nvda.unrealized_pnl is None

    botz = next(position for position in result.positions if position.symbol == "BOTZ")
    assert botz.asset_class == AssetClass.ETF

    hk_position = next(position for position in result.positions if position.symbol == "00700")
    assert hk_position.market == Market.HK
    assert hk_position.currency == "HKD"


def test_parse_tiger_text_extracts_us_positions_and_cash() -> None:
    result = parse_tiger_text(
        FIXTURE_DIR.joinpath("tiger.txt").read_text(encoding="utf-8"), "2026-05"
    )

    assert result.statement_id == "2026-05-tiger"
    assert {position.symbol for position in result.positions} == {"ARM", "COHR"}
    assert all(position.market == Market.US for position in result.positions)
    assert result.positions[0].currency == "USD"
    assert len(result.cash_balances) == 1

    arm = next(position for position in result.positions if position.symbol == "ARM")
    assert arm.cost_price == Decimal("281.00")
    assert arm.cost_value == Decimal("1124.00")
    assert arm.unrealized_pnl == Decimal("288.00")


def test_parse_phillips_text_extracts_hk_and_us_positions() -> None:
    result = parse_phillips_text(
        FIXTURE_DIR.joinpath("phillips.txt").read_text(encoding="utf-8"), "2026-05"
    )

    assert result.statement_id == "2026-05-phillips"
    assert {position.symbol for position in result.positions} == {"0300476", "NVDA"}
    assert len(result.cash_balances) == 1

    hk = next(position for position in result.positions if position.symbol == "0300476")
    assert hk.market == Market.HK
    assert hk.currency == "HKD"
    assert hk.confidence == "medium"
    assert "currency" in hk.notes

    us = next(position for position in result.positions if position.symbol == "NVDA")
    assert us.market == Market.US
    assert us.currency == "USD"
