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


def test_parse_eastmoney_first_page_only() -> None:
    result = parse_eastmoney_page(
        "资金余额(RMB)： 10000.00\n资金可用(RMB)： 405219.55",
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
    assert result.cash_balances[0].cash_balance == Decimal("10000.00")
    assert result.cash_balances[0].available_balance == Decimal("405219.55")


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
            "资金余额(RMB)： 1\n资金可用(RMB)： 1",
            [invalid_positions],
            "2026-07",
        )

    with pytest.raises(ValueError, match="人民币资金"):
        parse_eastmoney_page("资金余额(RMB)： 1", [POSITIONS], "2026-07")


def test_encrypted_parser_reads_only_first_page_and_hides_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def extract_text(self) -> str:
            return "资金余额(RMB)： 1\n资金可用(RMB)： 2"

        def extract_tables(self) -> list[list[list[str]]]:
            return [POSITIONS]

    class UnreadablePage:
        def extract_text(self) -> str:
            raise AssertionError("second page must not be read")

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
