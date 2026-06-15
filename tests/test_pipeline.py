from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.cli import build_parser
from open_trader.fx import StaticMonthEndFxProvider
from open_trader.models import AssetClass, CashBalance, Market, Position, WarningRecord
from open_trader.parsers.base import ParseResult
from open_trader.pipeline import run_import


class FakeParser:
    broker = "fake"
    parser_version = "test-1"

    def parse(self, path: Path, month: str) -> ParseResult:
        return ParseResult(
            statement_id=f"{month}-fake",
            broker=self.broker,
            positions=[
                Position(
                    statement_id=f"{month}-fake",
                    broker=self.broker,
                    account_alias="main",
                    market=Market.US,
                    asset_class=AssetClass.STOCK,
                    symbol="NVDA",
                    name="NVIDIA Corp",
                    currency="USD",
                    quantity=Decimal("2"),
                    cost_price=Decimal("100"),
                    last_price=Decimal("130"),
                    market_value=Decimal("260"),
                    cost_value=Decimal("200"),
                    unrealized_pnl=Decimal("60"),
                    confidence="high",
                    notes="",
                )
            ],
            cash_balances=[
                CashBalance(
                    statement_id=f"{month}-fake",
                    broker=self.broker,
                    account_alias="main",
                    currency="USD",
                    cash_balance=Decimal("50"),
                    available_balance=Decimal("45"),
                    confidence="high",
                    notes="",
                )
            ],
            warnings=[
                WarningRecord(
                    statement_id=f"{month}-fake",
                    broker=self.broker,
                    page=1,
                    severity="warning",
                    code="fake_warning",
                    message="fake warning",
                )
            ],
            page_count=3,
        )


def test_run_import_writes_portfolio_and_latest(tmp_path: Path) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    data_dir = tmp_path / "data"
    fx_provider = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})

    result = run_import(
        month="2026-05",
        statement_paths={"fake": source},
        parsers=[FakeParser()],
        data_dir=data_dir,
        fx_provider=fx_provider,
    )

    run_dir = data_dir / "runs" / "2026-05"
    assert result.run_dir == run_dir
    assert result.portfolio_path == run_dir / "portfolio.csv"
    assert result.latest_path == data_dir / "latest" / "portfolio.csv"
    assert result.positions_count == 1
    assert result.cash_count == 1
    assert result.warnings_count == 1

    portfolio_content = result.portfolio_path.read_text(encoding="utf-8")
    assert result.latest_path.read_text(encoding="utf-8") == portfolio_content
    assert "NVDA" in portfolio_content

    manifest_rows = list(csv.DictReader((run_dir / "manifest.csv").open(encoding="utf-8")))
    assert manifest_rows == [
        {
            "month": "2026-05",
            "broker": "fake",
            "source_file": str(source),
            "source_sha256": (
                "a0958d60fa8069e38bc46399b856ee3b619b66c7363e4d27aa253e6e5f92281b"
            ),
            "parsed_at": manifest_rows[0]["parsed_at"],
            "page_count": "3",
            "parser_version": "test-1",
            "status": "parsed",
        }
    ]

    positions = list(csv.DictReader((run_dir / "extracted_positions.csv").open(encoding="utf-8")))
    cash = list(csv.DictReader((run_dir / "extracted_cash.csv").open(encoding="utf-8")))
    warnings = list(csv.DictReader((run_dir / "parse_warnings.csv").open(encoding="utf-8")))
    assert positions[0]["symbol"] == "NVDA"
    assert cash[0]["currency"] == "USD"
    assert warnings[0]["code"] == "fake_warning"


def test_import_statements_help_includes_usd_hkd(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["import-statements", "--help"])

    assert exc_info.value.code == 0
    assert "--usd-hkd" in capsys.readouterr().out
