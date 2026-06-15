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

    def __init__(
        self,
        *,
        broker: str = "fake",
        result_broker: str | None = None,
        position_currency: str = "USD",
        warning_page: int | None = 1,
    ) -> None:
        self.broker = broker
        self.result_broker = result_broker or broker
        self.position_currency = position_currency
        self.warning_page = warning_page

    def parse(self, path: Path, month: str) -> ParseResult:
        return ParseResult(
            statement_id=f"{month}-{self.result_broker}",
            broker=self.result_broker,
            positions=[
                Position(
                    statement_id=f"{month}-{self.result_broker}",
                    broker=self.result_broker,
                    account_alias="main",
                    market=Market.US,
                    asset_class=AssetClass.STOCK,
                    symbol="NVDA",
                    name="NVIDIA Corp",
                    currency=self.position_currency,
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
                    statement_id=f"{month}-{self.result_broker}",
                    broker=self.result_broker,
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
                    statement_id=f"{month}-{self.result_broker}",
                    broker=self.result_broker,
                    page=self.warning_page,
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
    assert warnings[0] == {
        "statement_id": "2026-05-fake",
        "broker": "fake",
        "page": "1",
        "severity": "warning",
        "code": "fake_warning",
        "message": "fake warning",
    }


def test_run_import_does_not_write_run_dir_when_portfolio_build_fails(
    tmp_path: Path,
) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    data_dir = tmp_path / "data"

    with pytest.raises(KeyError, match="SGD"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source},
            parsers=[FakeParser(position_currency="SGD")],
            data_dir=data_dir,
            fx_provider=StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")}),
        )

    assert not (data_dir / "runs" / "2026-05").exists()
    assert not (data_dir / "latest" / "portfolio.csv").exists()


def test_run_import_rejects_missing_broker_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing.*fake"):
        run_import(
            month="2026-05",
            statement_paths={},
            parsers=[FakeParser()],
            data_dir=tmp_path / "data",
            fx_provider=StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")}),
        )


def test_run_import_rejects_extra_statement_path_key(tmp_path: Path) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")

    with pytest.raises(ValueError, match="unknown.*extra"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source, "extra": source},
            parsers=[FakeParser()],
            data_dir=tmp_path / "data",
            fx_provider=StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")}),
        )


def test_run_import_rejects_parse_result_broker_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")

    with pytest.raises(ValueError, match="fake.*other"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source},
            parsers=[FakeParser(result_broker="other")],
            data_dir=tmp_path / "data",
            fx_provider=StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")}),
        )


def test_run_import_writes_warning_with_blank_page_when_page_is_none(
    tmp_path: Path,
) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    data_dir = tmp_path / "data"

    result = run_import(
        month="2026-05",
        statement_paths={"fake": source},
        parsers=[FakeParser(warning_page=None)],
        data_dir=data_dir,
        fx_provider=StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")}),
    )

    warnings = list(
        csv.DictReader((result.run_dir / "parse_warnings.csv").open(encoding="utf-8"))
    )
    assert warnings[0] == {
        "statement_id": "2026-05-fake",
        "broker": "fake",
        "page": "",
        "severity": "warning",
        "code": "fake_warning",
        "message": "fake warning",
    }


def test_run_import_rerun_replaces_outputs(tmp_path: Path) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    data_dir = tmp_path / "data"
    fx_provider = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")})

    first = run_import(
        month="2026-05",
        statement_paths={"fake": source},
        parsers=[FakeParser()],
        data_dir=data_dir,
        fx_provider=fx_provider,
    )
    first.portfolio_path.write_text("stale\n", encoding="utf-8")

    second = run_import(
        month="2026-05",
        statement_paths={"fake": source},
        parsers=[FakeParser()],
        data_dir=data_dir,
        fx_provider=fx_provider,
    )

    assert second.portfolio_path.read_text(encoding="utf-8") != "stale\n"
    assert second.latest_path.read_text(encoding="utf-8") == second.portfolio_path.read_text(
        encoding="utf-8"
    )


def test_import_statements_help_includes_usd_hkd(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["import-statements", "--help"])

    assert exc_info.value.code == 0
    assert "--usd-hkd" in capsys.readouterr().out


@pytest.mark.parametrize("rate", ["abc", "0", "-1"])
def test_import_statements_rejects_invalid_usd_hkd(
    rate: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "import-statements",
                "--month",
                "2026-05",
                "--futu",
                "futu.pdf",
                "--tiger",
                "tiger.pdf",
                "--phillips",
                "phillips.pdf",
                "--usd-hkd",
                rate,
            ]
        )

    assert exc_info.value.code == 2
    assert "invalid" in capsys.readouterr().err
