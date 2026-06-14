from __future__ import annotations

from dataclasses import is_dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from open_trader.models import AssetClass, Market
from open_trader.parsers.base import (
    ParseResult,
    StatementParser,
    detect_asset_class,
    detect_market,
    parse_decimal,
    sha256_file,
    split_symbol_name,
)


def test_parse_decimal_normalizes_common_statement_values() -> None:
    assert parse_decimal("1,234.56") == Decimal("1234.56")
    assert parse_decimal("HKD 2,500.00") == Decimal("2500.00")
    assert parse_decimal("USD (3,210.99)") == Decimal("-3210.99")
    assert parse_decimal("(123.45)") == Decimal("-123.45")


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "-", "--", "not a number", "NaN", "Infinity", "-Infinity"],
)
def test_parse_decimal_returns_none_for_missing_or_invalid_values(value: str | None) -> None:
    assert parse_decimal(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("NVIDIA (NVDA)", ("NVDA", "NVIDIA")),
        (
            "BOTZ(Global X Robotics & Artificial Intelligence Thematic ETF)",
            ("BOTZ", "Global X Robotics & Artificial Intelligence Thematic ETF"),
        ),
        ("00700(腾讯控股)", ("00700", "腾讯控股")),
        ("700 HK", ("700.HK", "")),
        ("BRK B", ("BRK.B", "")),
        ("Tencent (700 HK)", ("700.HK", "Tencent")),
        ("Berkshire Hathaway (BRK B)", ("BRK.B", "Berkshire Hathaway")),
        ("700 HK (Tencent)", ("700.HK", "Tencent")),
        ("BRK B (Berkshire Hathaway)", ("BRK.B", "Berkshire Hathaway")),
    ],
)
def test_split_symbol_name_handles_symbol_and_name_orders(
    value: str, expected: tuple[str, str]
) -> None:
    assert split_symbol_name(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("US", Market.US),
        ("NASDAQ", Market.US),
        ("CBOE", Market.US),
        ("ARCA", Market.US),
        ("NYSE ARCA", Market.US),
        ("SEHK", Market.HK),
        ("HK", Market.HK),
        ("SG", Market.OTHER),
    ],
)
def test_detect_market_maps_known_markets(value: str, expected: Market) -> None:
    assert detect_market(value) == expected


@pytest.mark.parametrize(
    ("symbol", "name", "expected"),
    [
        ("BOTZ", "Global X Robotics & Artificial Intelligence Thematic ETF", AssetClass.ETF),
        ("2800", "Tracker Fund of Hong Kong ETF", AssetClass.ETF),
        ("USDXX", "美元货币市场基金 ETF", AssetClass.MONEY_MARKET_FUND),
        ("USDMMF", "高腾微金美元貨幣基金", AssetClass.MONEY_MARKET_FUND),
        ("HKFUND", "盈富基金", AssetClass.FUND),
        ("NVDA 240621C00120000", "NVIDIA CALL", AssetClass.OPTION),
        ("TSLA", "Tesla PUT Option", AssetClass.OPTION),
        ("NVDA", "NVIDIA 期权", AssetClass.OPTION),
        ("NVDA", "NVIDIA 期權", AssetClass.OPTION),
        ("NVDA", "NVIDIA Corporation", AssetClass.STOCK),
    ],
)
def test_detect_asset_class_uses_simple_statement_clues(
    symbol: str, name: str, expected: AssetClass
) -> None:
    assert detect_asset_class(symbol, name) == expected


def test_statement_parser_base_parse_must_be_implemented() -> None:
    parser = StatementParser()

    with pytest.raises(NotImplementedError):
        parser.parse("statement.pdf", "2026-05")  # type: ignore[arg-type]


def test_parse_result_shape_and_sha256_file(tmp_path) -> None:
    statement = tmp_path / "statement.txt"
    statement.write_text("open trader\n", encoding="utf-8")

    result = ParseResult(
        statement_id="abc",
        broker="futu",
        positions=[],
        cash_balances=[],
        warnings=[],
        page_count=3,
    )

    assert is_dataclass(result)
    assert result.page_count == 3
    assert (
        sha256_file(statement)
        == "ab9d1a6a6801519bbdb22ef561948b27c791ad07a9f768eb744b62a10a310ba5"
    )


def test_sha256_file_reads_bounded_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    data = (b"x" * (1024 * 1024)) + b"tail"
    read_sizes: list[int] = []

    class FakeHandle:
        def __init__(self) -> None:
            self.offset = 0

        def __enter__(self) -> FakeHandle:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            if self.offset >= len(data):
                return b""
            if size < 0:
                chunk = data[self.offset :]
                self.offset = len(data)
                return chunk
            chunk = data[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    def fake_open(self: Path, mode: str) -> FakeHandle:
        assert mode == "rb"
        return FakeHandle()

    monkeypatch.setattr(Path, "open", fake_open)

    assert sha256_file(Path("fake.pdf")) == sha256(data).hexdigest()
    assert read_sizes == [1024 * 1024, 1024 * 1024, 1024 * 1024]


def test_parse_result_defaults_collections_and_page_count() -> None:
    result = ParseResult(statement_id="x", broker="futu")

    assert result.positions == []
    assert result.cash_balances == []
    assert result.warnings == []
    assert result.page_count == 0
