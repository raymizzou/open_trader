from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.models import AssetClass, Market
from open_trader.parsers.base import ParseResult
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
    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("USD", Decimal("1000.00")),
        ("HKD", Decimal("5000.00")),
    ]


def test_parse_tiger_text_extracts_us_positions_and_cash() -> None:
    result = parse_tiger_text(
        FIXTURE_DIR.joinpath("tiger.txt").read_text(encoding="utf-8"), "2026-05"
    )

    assert result.statement_id == "2026-05-tiger"
    assert {position.symbol for position in result.positions} == {"ARM", "COHR"}
    assert all(position.market == Market.US for position in result.positions)
    assert result.positions[0].currency == "USD"
    assert len(result.cash_balances) == 1
    assert result.cash_balances[0].currency == "USD"
    assert result.cash_balances[0].cash_balance == Decimal("2000.00")

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
    assert result.cash_balances[0].currency == "HKD"
    assert result.cash_balances[0].cash_balance == Decimal("8000.00")

    hk = next(position for position in result.positions if position.symbol == "0300476")
    assert hk.market == Market.HK
    assert hk.currency == "HKD"
    assert hk.confidence == "medium"
    assert "currency" in hk.notes

    us = next(position for position in result.positions if position.symbol == "NVDA")
    assert us.market == Market.US
    assert us.currency == "USD"


def test_parse_tiger_text_accepts_parenthesized_unrealized_pnl() -> None:
    text = """期末持仓
股票
代码 数量 乘数 成本价格 收盘价格 市值 未实现的损益 初始保证金要求 维持保证金要求 币种
ARM Holdings (ARM) 4 1.0 281.00 353.00 1412.00 (288.00) 706.00 564.80 USD
"""

    result = parse_tiger_text(text, "2026-05")

    assert len(result.positions) == 1
    assert result.positions[0].symbol == "ARM"
    assert result.positions[0].unrealized_pnl == Decimal("-288.00")


def test_parse_futu_and_phillips_text_accept_parenthesized_numeric_fields() -> None:
    futu = parse_futu_text(
        """期末概覽-股票和股票期權
代碼名稱 交易所/市場 貨幣種類 數量 價格 乘數 市值 初始保證金要求 維持保證金要求 維持保證金率
NVDA(NVIDIA) US USD 10 130.00 - (1,300.00) 650.00 520.00 0.40
""",
        "2026-05",
    )
    phillips = parse_phillips_text(
        """Securities Portfolio
產品 市場 產品代號 代號名稱 上日存貨 最後買貨日期 是日存貨 收市價 市值 按貨比率 按倉值
股票 US NVDA NVIDIA 0 2026/05/20 5 130.00 (650.00) 0.50 325.00
""",
        "2026-05",
    )

    assert futu.positions[0].market_value == Decimal("-1300.00")
    assert phillips.positions[0].market_value == Decimal("-650.00")


@pytest.mark.parametrize(
    ("parser", "text", "expected_cash"),
    [
        (
            parse_futu_text,
            """現金結餘
USD 1000.00
Other Section
USD 9999.00
""",
            [("USD", Decimal("1000.00"))],
        ),
        (
            parse_tiger_text,
            """现金
USD 2000.00
Other Section
USD 9999.00
""",
            [("USD", Decimal("2000.00"))],
        ),
        (
            parse_phillips_text,
            """Cash Balance
HKD 8000.00
Other Section
USD 9999.00
""",
            [("HKD", Decimal("8000.00"))],
        ),
    ],
)
def test_parse_text_stops_cash_section_after_unknown_non_cash_line(
    parser: Callable[[str, str], ParseResult],
    text: str,
    expected_cash: list[tuple[str, Decimal]],
) -> None:
    result = parser(text, "2026-05")

    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == expected_cash


def test_parse_phillips_text_accepts_english_stock_rows() -> None:
    result = parse_phillips_text(
        """Securities Portfolio
Product Market ProductCode ProductName Previous LastBuyDate Quantity Close MarketValue Ratio MarginValue
Stock US NVDA NVIDIA 0 2026/05/20 5 130.00 650.00 0.50 325.00
""",
        "2026-05",
    )

    assert len(result.positions) == 1
    assert result.positions[0].symbol == "NVDA"
    assert result.positions[0].market == Market.US
    assert result.positions[0].asset_class == AssetClass.STOCK
