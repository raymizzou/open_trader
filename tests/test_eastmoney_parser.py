from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import traceback

import pytest

from open_trader.models import AssetClass, Market
from open_trader.parsers.eastmoney import (
    EastmoneyStatementParser,
    parse_eastmoney_page,
)


POSITIONS = [
    ["交易市场", "证券代码", "证券名称", "持仓数量", "市价", "成本价", "证券市值"],
    ["沪市A股", "600025", "华能水电", "6000", "9.620", "8.891", "57720.00"],
]

TRANSACTIONS = [
    [
        "发生日期",
        "买卖类别",
        "证券代码",
        "证券名称",
        "成交数量",
        "成交价格",
        "总发生金额",
        "手续费",
        "印花税",
        "过户费",
        "资金余额",
    ],
    [
        "20260716",
        "证券买入",
        "688796",
        "百奥赛图",
        "200",
        "144.7500",
        "-28955.29",
        "5.00",
        "0.00",
        "0.29",
        "34911.30",
    ],
    [
        "20260717",
        "证券卖出",
        "688796",
        "百奥赛图",
        "200",
        "113.9000",
        "22763.38",
        "5.00",
        "11.39",
        "0.23",
        "10000.10",
    ],
]


def test_parse_eastmoney_first_page_only() -> None:
    result = parse_eastmoney_page(
        "总资产(RMB)： 462939.55\n资金余额(RMB)： 10000.00\n资金可用(RMB)： 405219.55",
        [POSITIONS, [["发生日期", "买卖类别", "证券代码"]]],
        "2026-07",
    )

    assert [(p.market, p.symbol, p.quantity) for p in result.positions] == [
        (Market.CN, "600025", Decimal("6000")),
    ]
    assert result.positions[0].asset_class == AssetClass.STOCK
    assert result.positions[0].currency == "CNY"
    assert result.positions[0].cost_value == Decimal("53346.000")
    assert result.positions[0].unrealized_pnl == Decimal("4374.000")
    assert result.cash_balances[0].cash_balance == Decimal("405219.55")
    assert result.cash_balances[0].available_balance == Decimal("405219.55")


def test_parse_eastmoney_extracts_auditable_trade_facts_and_complete_fees() -> None:
    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, TRANSACTIONS],
        "2026-07",
    )

    assert [trade.side for trade in result.trades] == ["buy", "sell"]
    assert result.trades[0].market == Market.CN
    assert result.trades[0].symbol == "688796"
    assert result.trades[0].quantity == Decimal("200")
    assert result.trades[0].price == Decimal("144.7500")
    assert result.trades[0].fee == Decimal("5.29")
    assert result.trades[0].traded_at == "2026-07-16T15:00:00+08:00"
    assert result.trades[0].execution_granularity == "statement_trade_date"
    assert result.trades[0].statement_sequence == 1
    assert result.trades[1].fee == Decimal("16.62")
    assert result.trades[1].costs_complete is True


def test_parse_eastmoney_ignores_non_trade_cash_ledger_rows() -> None:
    non_trade = [
        "20260716",
        "银行利息",
        "",
        "",
        "",
        "",
        "1.23",
        "0",
        "0",
        "0",
        "10001.23",
    ]

    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, [TRANSACTIONS[0], non_trade, *TRANSACTIONS[1:]]],
        "2026-07",
    )

    assert len(result.trades) == 2


def test_parse_eastmoney_cash_when_currency_balances_share_lines() -> None:
    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00 总资产(HKD)： 2.00 总资产(USD)： 3.00\n"
        "资金余额(RMB)： 10000.00 资金余额(HKD)： 2.00 资金余额(USD)： 3.00\n"
        "资金可用(RMB)： 405219.55 资金可用(HKD)： 5.00 资金可用(USD)： 6.00",
        [POSITIONS],
        "2026-07",
    )

    assert result.cash_balances[0].cash_balance == Decimal("10.00")
    assert result.cash_balances[0].available_balance == Decimal("405219.55")


def test_parse_eastmoney_skips_closed_zero_positions() -> None:
    closed = ["沪市A股", "600000", "已清仓", "0", "1", "1", "0"]

    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [[POSITIONS[0], closed, POSITIONS[1]]],
        "2026-07",
    )

    assert [position.symbol for position in result.positions] == ["600025"]


def test_parser_rejects_missing_summary_table() -> None:
    with pytest.raises(ValueError, match="汇总股票资料"):
        parse_eastmoney_page("资金余额", [], "2026-07")


def test_parser_rejects_invalid_summary_rows_and_cash() -> None:
    invalid_positions = [
        POSITIONS[0],
        ["港股", "00700", "腾讯", "1", "1", "1", "1"],
        ["深市A股", "000001", "平安银行", "0", "1", "1", "1"],
        ["深市A股", "000002", "万科", "1", "NaN", "1", "1"],
    ]

    with pytest.raises(ValueError, match="持仓行"):
        parse_eastmoney_page(
            "总资产(RMB)： 2\n资金余额(RMB)： 1\n资金可用(RMB)： 1",
            [invalid_positions],
            "2026-07",
        )

    with pytest.raises(ValueError, match="人民币资金"):
        parse_eastmoney_page("资金余额(RMB)： 1", [POSITIONS], "2026-07")


def test_encrypted_parser_reads_cash_from_first_page_and_trade_tables_from_all_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def extract_text(self) -> str:
            return "总资产(RMB)： 57722\n资金余额(RMB)： 1\n资金可用(RMB)： 2"

        def extract_tables(self) -> list[list[list[str]]]:
            return [POSITIONS]

    class UnreadablePage:
        def extract_text(self) -> str:
            raise AssertionError("second page must not be read")

        def extract_tables(self) -> list[list[list[str]]]:
            return [TRANSACTIONS]

    class FakePdf:
        pages = [FakePage(), UnreadablePage()]

        def __enter__(self) -> FakePdf:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    opened: dict[str, object] = {}

    def fake_open(path: Path, *, password: str) -> FakePdf:
        opened.update(path=path, password=password)
        return FakePdf()

    monkeypatch.setattr("open_trader.parsers.eastmoney.pdfplumber.open", fake_open)
    parser = EastmoneyStatementParser(password="sanitized-secret")

    result = parser.parse(Path("sanitized.pdf"), "2026-07")

    assert opened == {
        "path": Path("sanitized.pdf"),
        "password": "sanitized-secret",
    }
    assert result.page_count == 2
    assert len(result.trades) == 2
    assert "sanitized-secret" not in repr(result)


def test_encrypted_parser_wraps_errors_without_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_open(path: Path, *, password: str) -> None:
        raise RuntimeError(f"bad password: {password}")

    monkeypatch.setattr("open_trader.parsers.eastmoney.pdfplumber.open", fake_open)
    password = "sanitized-secret"

    with pytest.raises(ValueError, match="无法打开或解密东方财富对账单") as exc_info:
        EastmoneyStatementParser(password=password).parse(Path("sanitized.pdf"), "2026-07")

    assert "sanitized-secret" not in str(exc_info.value)
    assert "sanitized-secret" not in "".join(
        traceback.format_exception(exc_info.value)
    )


def test_encrypted_parser_extracts_print_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def extract_text(self) -> str:
            return "东方财富证券\n打印日期：2026-07-12"

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self) -> FakePdf:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "open_trader.parsers.eastmoney.pdfplumber.open",
        lambda path, *, password: FakePdf(),
    )

    assert (
        EastmoneyStatementParser(password="sanitized-secret").statement_date(
            Path("sanitized.pdf")
        )
        == "2026-07-12"
    )


def test_encrypted_parser_rejects_missing_print_date_without_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def extract_text(self) -> str:
            return "东方财富证券"

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self) -> FakePdf:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "open_trader.parsers.eastmoney.pdfplumber.open",
        lambda path, *, password: FakePdf(),
    )
    password = "sanitized-secret"

    with pytest.raises(ValueError, match="打印日期") as exc_info:
        EastmoneyStatementParser(password=password).statement_date(Path("sanitized.pdf"))

    assert password not in str(exc_info.value)
