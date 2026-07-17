from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import traceback

import pytest

from open_trader.models import AssetClass, Market
from open_trader.parsers.eastmoney import (
    EXECUTION_HEADER,
    EastmoneyStatementParser,
    parse_eastmoney_page,
)


POSITIONS = [
    ["交易市场", "证券代码", "证券名称", "持仓数量", "市价", "成本价", "证券市值"],
    ["沪市A股", "600025", "华能水电", "6000", "9.620", "8.891", "57720.00"],
]


def test_parse_eastmoney_summary_positions_and_cash() -> None:
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


def test_eastmoney_statement_extracts_actual_trade_fills() -> None:
    executions = [
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
            "20260710",
            "证券买入",
            "600900",
            "脱敏股票",
            "2000",
            "28.50",
            "-57006.20",
            "5.00",
            "0.00",
            "1.20",
            "100000.00",
        ],
    ]

    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, executions],
        "2026-07",
    )

    assert [
        (fill.market.value, fill.symbol, fill.side, fill.quantity)
        for fill in result.fills
    ] == [("CN", "600900", "BUY", Decimal("2000"))]
    assert result.fills[0].price == Decimal("28.50")
    assert result.fills[0].fees == Decimal("6.20")
    assert result.fills[0].executed_at == "2026-07-10"
    assert result.fills[0].source_id
    assert result.fills_complete is True
    assert result.fills_coverage_start is None
    assert result.fills_coverage_end is None


def test_eastmoney_extracts_declared_fill_coverage_start() -> None:
    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00\n"
        "查询区间：2026/06/16-2026/07/16",
        [POSITIONS, [list(EXECUTION_HEADER)]],
        "2026-07",
    )

    assert result.fills_coverage_start == "2026-06-16"
    assert result.fills_coverage_end == "2026-07-16"


def test_eastmoney_without_execution_header_does_not_claim_fill_completeness() -> None:
    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS],
        "2026-07",
    )

    assert result.fills == []
    assert result.fills_complete is False


@pytest.mark.parametrize(
    "activity",
    [
        "证券红利",
        "证券转入",
        "证券转出",
        "红利入账",
        "天天宝申购",
        "天天宝赎回",
        "银行转证券",
        "证券转银行",
        "利息归本",
    ],
)
def test_eastmoney_non_trade_security_activity_does_not_block_completeness(
    activity: str,
) -> None:
    header = [
        "发生日期", "买卖类别", "证券代码", "证券名称", "成交数量",
        "成交价格", "总发生金额", "手续费", "印花税", "过户费", "资金余额",
    ]
    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, [header, ["20260710", activity, "600900", "股票", "100", "10", "10", "", "", "", ""]]],
        "2026-07",
    )

    assert result.fills_complete is True
    assert result.warnings == []


@pytest.mark.parametrize("missing_index", [0, 2, 4, 5])
def test_eastmoney_statement_warns_for_incomplete_execution_row(
    missing_index: int,
) -> None:
    executions = [
        list(
            (
                "发生日期", "买卖类别", "证券代码", "证券名称", "成交数量",
                "成交价格", "总发生金额", "手续费", "印花税", "过户费", "资金余额",
            )
        ),
        ["20260710", "证券买入", "600900", "脱敏股票", "2000", "28.50",
         "", "0", "0", "0", ""],
    ]
    executions[1][missing_index] = ""

    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, executions],
        "2026-07",
    )

    assert [warning.code for warning in result.warnings] == [
        "invalid_execution_row"
    ]


@pytest.mark.parametrize("activity", ["", "证券未知"])
def test_eastmoney_trade_shaped_row_without_known_side_is_incomplete(
    activity: str,
) -> None:
    header = [
        "发生日期", "买卖类别", "证券代码", "证券名称", "成交数量",
        "成交价格", "总发生金额", "手续费", "印花税", "过户费", "资金余额",
    ]
    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, [header, ["20260710", activity, "600900", "股票", "100", "10", "", "", "", "", ""]]],
        "2026-07",
    )

    assert result.fills_complete is False
    assert [warning.code for warning in result.warnings] == [
        "invalid_execution_row"
    ]


@pytest.mark.parametrize(
    "row",
    [
        ["20260710", "证券买入", "600900", "股票", "", "", "", "", "", "", ""],
        ["20260710", "", "600900", "股票", "", "", "", "", "", "", ""],
        ["", "证券未知", "", "", "", "", "", "", "", "", ""],
    ],
)
def test_eastmoney_any_nonempty_unparsed_execution_row_is_incomplete(
    row: list[str],
) -> None:
    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, [list(EXECUTION_HEADER), row]],
        "2026-07",
    )

    assert result.fills_complete is False
    assert [warning.code for warning in result.warnings] == [
        "invalid_execution_row"
    ]


def test_eastmoney_empty_execution_row_does_not_block_completeness() -> None:
    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, [list(EXECUTION_HEADER), [""] * len(EXECUTION_HEADER)]],
        "2026-07",
    )

    assert result.fills_complete is True
    assert result.warnings == []


def test_eastmoney_fill_fees_require_all_fee_columns() -> None:
    executions = [
        list(
            (
                "发生日期", "买卖类别", "证券代码", "证券名称", "成交数量",
                "成交价格", "总发生金额", "手续费", "印花税", "过户费", "资金余额",
            )
        ),
        [
            "20260710", "证券买入", "600900", "脱敏股票", "2000", "28.50",
            "", "5.00", "", "1.20", "",
        ],
    ]

    result = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, executions],
        "2026-07",
    )

    assert result.fills[0].fees is None


def test_eastmoney_identical_execution_rows_get_stable_distinct_ids() -> None:
    header = [
        "发生日期", "买卖类别", "证券代码", "证券名称", "成交数量",
        "成交价格", "总发生金额", "手续费", "印花税", "过户费", "资金余额",
    ]
    row = [
        "20260710", "证券买入", "600900", "脱敏股票", "2000", "28.50",
        "", "5.00", "0", "1.20", "",
    ]

    first = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, [header, row, row]],
        "2026-07",
    )
    repeated = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, [header, row, row]],
        "2026-07",
    )

    assert len({fill.source_id for fill in first.fills}) == 2
    assert [fill.source_id for fill in first.fills] == [
        fill.source_id for fill in repeated.fills
    ]
    assert [fill.source_sequence for fill in first.fills] == [0, 1]


def test_eastmoney_source_sequence_is_local_to_symbol_and_execution_date() -> None:
    header = list(EXECUTION_HEADER)
    target_one = [
        "20260710", "证券买入", "600900", "目标", "100", "10", "", "0", "0", "0", "",
    ]
    target_two = [
        "20260710", "证券卖出", "600900", "目标", "100", "11", "", "0", "0", "0", "",
    ]
    other_day = [
        "20260709", "证券买入", "600900", "目标", "100", "9", "", "0", "0", "0", "",
    ]
    other_symbol = [
        "20260710", "证券买入", "600901", "其他", "100", "8", "", "0", "0", "0", "",
    ]

    first = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, [header, other_day, target_one, other_symbol, target_two]],
        "2026-07",
    )
    reordered = parse_eastmoney_page(
        "总资产(RMB)： 57730.00\n资金可用(RMB)： 10.00",
        [POSITIONS, [header, other_symbol, target_one, target_two, other_day]],
        "2026-07",
    )

    assert [
        fill.source_sequence
        for fill in first.fills
        if fill.symbol == "600900" and fill.executed_at == "2026-07-10"
    ] == [0, 1]
    assert [
        fill.source_sequence
        for fill in reordered.fills
        if fill.symbol == "600900" and fill.executed_at == "2026-07-10"
    ] == [0, 1]


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


def test_encrypted_parser_reads_trade_tables_from_all_pages_and_hides_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FirstPage:
        def extract_text(self) -> str:
            return "总资产(RMB)： 57722\n资金余额(RMB)： 1\n资金可用(RMB)： 2"

        def extract_tables(self) -> list[list[list[str]]]:
            return [POSITIONS]

    class SecondPage:
        def extract_text(self) -> str:
            return ""

        def extract_tables(self) -> list[list[list[str]]]:
            return [
                [
                    [
                        "发生日期", "买卖类别", "证券代码", "证券名称", "成交数量",
                        "成交价格", "总发生金额", "手续费", "印花税", "过户费", "资金余额",
                    ],
                    [
                        "20260710", "证券卖出", "600900", "脱敏股票", "2000", "28.50",
                        "56990.00", "5.00", "4.00", "1.00", "100000.00",
                    ],
                ]
            ]

    class FakePdf:
        pages = [FirstPage(), SecondPage()]

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
    assert [(fill.symbol, fill.side) for fill in result.fills] == [
        ("600900", "SELL")
    ]
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
