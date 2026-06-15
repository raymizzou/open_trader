from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.models import AssetClass, Market
from open_trader.parsers.base import ParseResult
import open_trader.parsers.futu as futu_parser
import open_trader.parsers.phillips as phillips_parser
import open_trader.parsers.tiger as tiger_parser
from open_trader.parsers.futu import FutuStatementParser, parse_futu_text
from open_trader.parsers.phillips import PhillipsStatementParser, parse_phillips_text
from open_trader.parsers.tiger import TigerStatementParser, parse_tiger_text


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pdf_text"


class FakePage:
    def __init__(self, text: str | None) -> None:
        self.text = text

    def extract_text(self) -> str | None:
        return self.text


class FakePdf:
    def __init__(self, pages: list[str | None]) -> None:
        self.pages = [FakePage(page) for page in pages]

    def __enter__(self) -> FakePdf:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def fake_pdf_open_for(pages: list[str | None]) -> Callable[[Path], FakePdf]:
    def fake_open(path: Path) -> FakePdf:
        assert path == Path("fake.pdf")
        return FakePdf(pages)

    return fake_open


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


def test_parse_futu_text_extracts_summary_cash_from_real_statement_layout() -> None:
    result = parse_futu_text(
        """期末概覽
期末資產淨值總覽 合計(HKD) 港幣資產 美元資產 人民幣資產 日元資產 新加坡元資產 韓元資產
股票和股票期權 162,327.84 104,054.00 7,436.36 0.00 0.00 0.00 0.00
現金結餘 236,134.20 236,134.20 0.00 0.00 0.00 0.00 0.00
資產淨值 836,315.02 350,140.19 62,041.06 0.00 0.00 0.00 0.00
""",
        "2026-05",
    )

    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("HKD", Decimal("236134.20"))
    ]


def test_parse_futu_text_extracts_hkd_summary_cash_not_total_when_usd_cash_exists() -> None:
    result = parse_futu_text(
        """期末概覽
期末資產淨值總覽 合計(HKD) 港幣資產 美元資產 人民幣資產 日元資產 新加坡元資產 韓元資產
現金結餘 228,284.20 236,134.20 -1,000.00 0.00 0.00 0.00 0.00
""",
        "2026-05",
    )

    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("HKD", Decimal("236134.20")),
        ("USD", Decimal("-1000.00")),
    ]


def test_parse_futu_text_ignores_non_ending_multicurrency_cash_summary() -> None:
    result = parse_futu_text(
        """期初概覽-資產
現金結餘 -190,663.65 -192,328.80 212.48 0.00 0.00 0.00 0.00
期末概覽
期末資產淨值總覽 合計(HKD) 港幣資產 美元資產 人民幣資產 日元資產 新加坡元資產 韓元資產
現金結餘 236,134.20 236,134.20 0.00 0.00 0.00 0.00 0.00
""",
        "2026-05",
    )

    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("HKD", Decimal("236134.20"))
    ]


def test_parse_futu_text_joins_wrapped_position_display_name() -> None:
    result = parse_futu_text(
        """期末概覽-股票和股票期權
代碼名稱 交易所/市場 貨幣種類 數量 價格 乘數 市值 初始保證金要求 維持保證金要求 維持保證金率
BOTZ(Global X Robotics & Artificial US USD 50 37.2600 - 1,863.00 1,117.80 838.35 0.4500
Intelligence Thematic ETF)
""",
        "2026-05",
    )

    assert len(result.positions) == 1
    assert result.positions[0].symbol == "BOTZ"
    assert result.positions[0].name == "Global X Robotics & Artificial Intelligence Thematic ETF"
    assert result.positions[0].asset_class == AssetClass.ETF


def test_parse_tiger_text_extracts_multiline_positions_and_currency_cash() -> None:
    result = parse_tiger_text(
        """按货币分类: USD
总数 证券 期货 基金
期末现金 -12,678.64 -12,678.64 0.00 0.00
按货币分类: HKD
总数 证券 期货 基金
期末现金 145,412.41 145,412.41 0.00 0.00
期末持仓
基金
代码 数量 成本价格 收盘价格 市值 未实现的损益 初始保证金要求 维持保证金要求 币种
华泰港元货币市场基金A
543253.5521 1.0997307 1.09990 597,524.58 91.99 29,876.23 29,876.23 HKD
(HK0000951506.HKD)
股票
代码 数量 乘数 成本价格 收盘价格 市值 未实现的损益 初始保证金要求 维持保证金要求 币种
ARM Holdings
4 1.0 281.3371000 353.29000 1,413.16 287.81 635.92 565.26 USD
(ARM)
""",
        "2026-05",
    )

    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("USD", Decimal("-12678.64")),
        ("HKD", Decimal("145412.41")),
    ]
    fund = next(position for position in result.positions if position.symbol == "HK0000951506.HKD")
    assert fund.asset_class == AssetClass.MONEY_MARKET_FUND
    assert fund.market == Market.HK
    assert fund.market_value == Decimal("597524.58")
    arm = next(position for position in result.positions if position.symbol == "ARM")
    assert arm.name == "ARM Holdings"
    assert arm.cost_value == Decimal("1125.3484000")


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


def test_parse_phillips_text_extracts_equity_rows_and_account_cash() -> None:
    result = parse_phillips_text(
        """戶口資料 Account Details
貨幣 轉下結餘 未交收結餘 T+1 未交收結餘 T+2 未交收結餘 ≥ T+3 累計利息 可用結餘 参考匯率 借貸利率
Currency Balance C/F Unsettled Balance Unsettled Balance Unsettled Balance ≥ T+3 Accrued Interest Available Balance Ref ExRate DR Int Rate
Normal 普通戶口
HKD -89,367.42 28,890.54 0.00 0.00 -42.33 -118,300.29 1.0000 列表1(Sch1)
USD 63.20 0.00 0.00 0.00 0.00 63.20 7.8363 列表1(Sch1)
HKD(Base) -88,872.17 28,890.54 0.00 0.00 -42.33 -117,805.04
股股票票投投資資組組合合 SSeeccuurriittiieess PPoorrttffoolliioo
產品 市場 產品代號 代號名稱 上日存貨 最後買貨日期 是日存貨 收市價 市值 按貨比率 按倉值
Product Market InstrumentCd DisplayName Qty B/F LastBoughtOn Qty C/F ClsPrice Market Value MgnRatio Margin Value
Normal 普通戶口 Currency : HKD
Equity XHKG 002476 VGT 300 12/05/26 300 378.8000 113,640.00 0.5000 56,820.00
股票 勝宏科技
""",
        "2026-05",
    )

    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("HKD", Decimal("-89367.42")),
        ("USD", Decimal("63.20")),
    ]
    assert len(result.positions) == 1
    position = result.positions[0]
    assert position.symbol == "02476"
    assert position.name == "VGT"
    assert position.market == Market.HK
    assert position.currency == "HKD"
    assert position.quantity == Decimal("300")
    assert position.market_value == Decimal("113640.00")


def test_parse_phillips_text_enters_account_details_from_currency_balance_header() -> None:
    result = parse_phillips_text(
        """Currency Balance C/F Unsettled Balance Unsettled Balance Unsettled Balance ≥ T+3 Accrued Interest Available Balance Ref ExRate DR Int Rate
Normal 普通戶口
USD 63.20 0.00 0.00 0.00 0.00 63.20 7.8363 列表1(Sch1)
""",
        "2026-05",
    )

    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("USD", Decimal("63.20"))
    ]


def test_parse_phillips_text_does_not_parse_currency_rows_outside_account_details() -> None:
    result = parse_phillips_text(
        """Cash Balance
HKD 8000.00
Other Section
USD 9999.00 unexpected non-account row
""",
        "2026-05",
    )

    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("HKD", Decimal("8000.00"))
    ]


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


def test_parse_tiger_text_infers_hk_market_from_hkd_position_currency() -> None:
    result = parse_tiger_text(
        """期末持仓
股票
代码 数量 乘数 成本价格 收盘价格 市值 未实现的损益 初始保证金要求 维持保证金要求 币种
00700(腾讯控股) 100 1.0 300.00 380.00 38000.00 8000.00 19000.00 15200.00 HKD
""",
        "2026-05",
    )

    assert len(result.positions) == 1
    assert result.positions[0].symbol == "00700"
    assert result.positions[0].market == Market.HK
    assert result.positions[0].currency == "HKD"


def test_parse_tiger_text_applies_multiplier_to_cost_value() -> None:
    result = parse_tiger_text(
        """期末持仓
股票
代码 数量 乘数 成本价格 收盘价格 市值 未实现的损益 初始保证金要求 维持保证金要求 币种
ARM Holdings (ARM) 4 2.0 281.00 353.00 1412.00 288.00 706.00 564.80 USD
""",
        "2026-05",
    )

    assert len(result.positions) == 1
    assert result.positions[0].cost_value == Decimal("2248.000")


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


def test_parse_phillips_text_accepts_hyphenated_symbols() -> None:
    result = parse_phillips_text(
        """Securities Portfolio
Product Market ProductCode ProductName Previous LastBuyDate Quantity Close MarketValue Ratio MarginValue
Stock US BRK-B Berkshire Hathaway 0 2026/05/20 1 500.00 500.00 0.50 250.00
""",
        "2026-05",
    )

    assert len(result.positions) == 1
    assert result.positions[0].symbol == "BRK-B"


def test_futu_statement_parser_joins_pdf_pages_and_sets_page_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        futu_parser.pdfplumber,
        "open",
        fake_pdf_open_for(
            [
                "期末概覽-股票和股票期權\n"
                "代碼名稱 交易所/市場 貨幣種類 數量 價格 乘數 市值 初始保證金要求 維持保證金要求 維持保證金率",
                "NVDA(NVIDIA) US USD 10 130.00 - 1300.00 650.00 520.00 0.40",
                "現金結餘\nUSD 1000.00",
            ]
        ),
    )

    result = FutuStatementParser().parse(Path("fake.pdf"), "2026-05")

    assert result.broker == "futu"
    assert result.page_count == 3
    assert [position.symbol for position in result.positions] == ["NVDA"]
    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("USD", Decimal("1000.00"))
    ]


def test_tiger_statement_parser_joins_pdf_pages_and_sets_page_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tiger_parser.pdfplumber,
        "open",
        fake_pdf_open_for(
            [
                "期末持仓\n股票\n"
                "代码 数量 乘数 成本价格 收盘价格 市值 未实现的损益 初始保证金要求 维持保证金要求 币种",
                "ARM Holdings (ARM) 4 1.0 281.00 353.00 1412.00 288.00 706.00 564.80 USD\n"
                "现金\nUSD 2000.00",
            ]
        ),
    )

    result = TigerStatementParser().parse(Path("fake.pdf"), "2026-05")

    assert result.broker == "tiger"
    assert result.page_count == 2
    assert [position.symbol for position in result.positions] == ["ARM"]
    assert result.positions[0].cost_value == Decimal("1124.00")
    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("USD", Decimal("2000.00"))
    ]


def test_phillips_statement_parser_joins_pdf_pages_and_sets_page_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        phillips_parser.pdfplumber,
        "open",
        fake_pdf_open_for(
            [
                "Securities Portfolio\n"
                "Product Market ProductCode ProductName Previous LastBuyDate Quantity Close MarketValue Ratio MarginValue",
                "Stock US NVDA NVIDIA 0 2026/05/20 5 130.00 650.00 0.50 325.00",
                "Cash Balance\nHKD 8000.00",
            ]
        ),
    )

    result = PhillipsStatementParser().parse(Path("fake.pdf"), "2026-05")

    assert result.broker == "phillips"
    assert result.page_count == 3
    assert [position.symbol for position in result.positions] == ["NVDA"]
    assert result.positions[0].market == Market.US
    assert [(cash.currency, cash.cash_balance) for cash in result.cash_balances] == [
        ("HKD", Decimal("8000.00"))
    ]
