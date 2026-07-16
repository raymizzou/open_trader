from __future__ import annotations

import csv
from decimal import Decimal
import importlib
from pathlib import Path

import pytest

from open_trader.models import AssetClass, CashBalance, Market, Position
from open_trader.parsers.base import ParseResult
from open_trader.portfolio import PORTFOLIO_FIELDNAMES


PDF_BYTES = b"%PDF-1.7\nfake statement"


class FakePhillipsParser:
    broker = "phillips"
    parser_version = "test-1"

    def __init__(self, statement_date: str = "2026-07-10") -> None:
        self.detected_date = statement_date

    def statement_date(self, path: Path) -> str:
        return self.detected_date

    def parse(self, path: Path, period: str) -> ParseResult:
        statement_id = f"{period}-phillips"
        return ParseResult(
            statement_id=statement_id,
            broker="phillips",
            positions=[
                Position(
                    statement_id=statement_id,
                    broker="phillips",
                    account_alias="phillips_main",
                    market=Market.HK,
                    asset_class=AssetClass.STOCK,
                    symbol="00700",
                    name="Tencent",
                    currency="HKD",
                    quantity=Decimal("1"),
                    cost_price=Decimal("500"),
                    last_price=Decimal("510"),
                    market_value=Decimal("510"),
                    cost_value=Decimal("500"),
                    unrealized_pnl=Decimal("10"),
                    confidence="high",
                    notes="",
                )
            ],
            cash_balances=[
                CashBalance(
                    statement_id=statement_id,
                    broker="phillips",
                    account_alias="phillips_main",
                    currency="HKD",
                    cash_balance=Decimal("90"),
                    available_balance=Decimal("90"),
                    confidence="high",
                    notes="",
                )
            ],
            page_count=1,
        )


class FakeEastmoneyParser:
    broker = "eastmoney"
    parser_version = "test-1"
    passwords: list[str] = []

    def __init__(self, password: str) -> None:
        self.passwords.append(password)

    def statement_date(self, path: Path) -> str:
        return "2026-07-12"

    def parse(self, path: Path, period: str) -> ParseResult:
        statement_id = f"{period}-eastmoney"
        return ParseResult(
            statement_id=statement_id,
            broker="eastmoney",
            cash_balances=[
                CashBalance(
                    statement_id=statement_id,
                    broker="eastmoney",
                    account_alias="eastmoney_main",
                    currency="CNY",
                    cash_balance=Decimal("100"),
                    available_balance=Decimal("100"),
                    confidence="high",
                    notes="",
                )
            ],
            page_count=1,
        )


def write_existing_portfolio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {field: "" for field in PORTFOLIO_FIELDNAMES}
    row.update(
        {
            "sort_group": "2",
            "market": "US",
            "asset_class": "stock",
            "symbol": "AAPL",
            "currency": "USD",
            "market_value": "100",
            "cost_value": "80",
            "fx_to_hkd": "7.8",
            "brokers": "futu",
            "risk_flag": "normal",
        }
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerow(row)


def test_import_pdf_archives_and_replaces_only_target_broker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", FakePhillipsParser
    )
    portfolio_path = tmp_path / "current" / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )

    result = service.import_pdf("phillips", PDF_BYTES)

    assert result == {
        "status": "ok",
        "broker": "phillips",
        "statement_date": "2026-07-10",
        "positions": 1,
        "cash": 1,
        "warnings": 0,
    }
    assert (
        tmp_path / "data/statements/phillips/2026-07-10/statement.pdf"
    ).read_bytes() == PDF_BYTES
    rows = list(csv.DictReader(portfolio_path.open(encoding="utf-8")))
    assert {row["brokers"] for row in rows} == {"futu", "phillips"}


def test_import_pdf_rejects_older_statement_and_preserves_current_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    current = FakePhillipsParser("2026-07-10")
    monkeypatch.setattr(statement_import, "PhillipsStatementParser", lambda: current)
    portfolio_path = tmp_path / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )
    service.import_pdf("phillips", PDF_BYTES)
    before_portfolio = portfolio_path.read_bytes()
    before_archive = (
        tmp_path / "data/statements/phillips/2026-07-10/statement.pdf"
    ).read_bytes()
    current.detected_date = "2026-07-09"

    with pytest.raises(ValueError, match="早于当前结单"):
        service.import_pdf("phillips", b"%PDF-1.7\nolder")

    assert portfolio_path.read_bytes() == before_portfolio
    assert (
        tmp_path / "data/statements/phillips/2026-07-10/statement.pdf"
    ).read_bytes() == before_archive
    assert not (tmp_path / "data/statements/phillips/2026-07-09").exists()


def test_import_pdf_restores_archive_when_pipeline_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", FakePhillipsParser
    )
    portfolio_path = tmp_path / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    archive = tmp_path / "data/statements/phillips/2026-07-10/statement.pdf"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"old statement")
    monkeypatch.setattr(
        statement_import,
        "run_uploaded_statement",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("pipeline failed")),
    )
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )

    with pytest.raises(RuntimeError, match="pipeline failed"):
        service.import_pdf("phillips", PDF_BYTES)

    assert archive.read_bytes() == b"old statement"


def test_import_pdf_rejects_empty_parse_without_archiving(
    tmp_path: Path,
    monkeypatch,
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")

    class EmptyParser(FakePhillipsParser):
        def parse(self, path: Path, period: str) -> ParseResult:
            return ParseResult(statement_id=f"{period}-phillips", broker="phillips")

    monkeypatch.setattr(statement_import, "PhillipsStatementParser", EmptyParser)
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        portfolio_path=tmp_path / "portfolio.csv",
        eastmoney_password="secret",
    )

    with pytest.raises(ValueError, match="没有可导入"):
        service.import_pdf("phillips", PDF_BYTES)

    assert not (tmp_path / "data/statements").exists()


def test_import_pdf_rejects_unsupported_broker(tmp_path: Path) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        portfolio_path=tmp_path / "portfolio.csv",
        eastmoney_password="secret",
    )

    with pytest.raises(ValueError, match="不支持的券商"):
        service.import_pdf("futu", PDF_BYTES)


def test_import_pdf_uses_eastmoney_password_month_archive_and_fixed_fx(
    tmp_path: Path,
    monkeypatch,
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    FakeEastmoneyParser.passwords.clear()
    monkeypatch.setattr(
        statement_import, "EastmoneyStatementParser", FakeEastmoneyParser
    )
    portfolio_path = tmp_path / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        portfolio_path=portfolio_path,
        eastmoney_password="local-secret",
    )

    result = service.import_pdf("eastmoney", PDF_BYTES)

    assert result["statement_date"] == "2026-07-12"
    assert FakeEastmoneyParser.passwords == ["local-secret"]
    assert (
        tmp_path / "data/statements/eastmoney/2026-07/statement.pdf"
    ).read_bytes() == PDF_BYTES
    eastmoney = next(
        row
        for row in csv.DictReader(portfolio_path.open(encoding="utf-8"))
        if row["brokers"] == "eastmoney"
    )
    assert eastmoney["fx_to_hkd"] == "1.08"
    assert eastmoney["fx_date"] == "2026-07-12"


def test_import_pdf_allows_same_date_replacement(tmp_path: Path, monkeypatch) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", FakePhillipsParser
    )
    portfolio_path = tmp_path / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )
    service.import_pdf("phillips", PDF_BYTES)
    replacement = b"%PDF-1.7\ncorrected statement"

    service.import_pdf("phillips", replacement)

    assert (
        tmp_path / "data/statements/phillips/2026-07-10/statement.pdf"
    ).read_bytes() == replacement
