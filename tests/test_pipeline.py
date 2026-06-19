from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

import open_trader.cli as cli
import open_trader.pipeline as pipeline
from open_trader.cli import build_parser
from open_trader.fx import StaticMonthEndFxProvider
from open_trader.models import AssetClass, CashBalance, Market, Position, WarningRecord
from open_trader.parsers.base import ParseResult
from open_trader.pipeline import ImportResult, run_import


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
        position_broker: str | None = None,
        cash_broker: str | None = None,
        warning_broker: str | None = None,
    ) -> None:
        self.broker = broker
        self.result_broker = result_broker or broker
        self.position_currency = position_currency
        self.warning_page = warning_page
        self.position_broker = position_broker or self.result_broker
        self.cash_broker = cash_broker or self.result_broker
        self.warning_broker = warning_broker or self.result_broker

    def parse(self, path: Path, month: str) -> ParseResult:
        return ParseResult(
            statement_id=f"{month}-{self.result_broker}",
            broker=self.result_broker,
            positions=[
                Position(
                    statement_id=f"{month}-{self.result_broker}",
                    broker=self.position_broker,
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
                    broker=self.cash_broker,
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
                    broker=self.warning_broker,
                    page=self.warning_page,
                    severity="warning",
                    code="fake_warning",
                    message="fake warning",
                )
            ],
            page_count=3,
        )


class SpyParser(FakeParser):
    def __init__(self) -> None:
        super().__init__()
        self.parse_called = False

    def parse(self, path: Path, month: str) -> ParseResult:
        self.parse_called = True
        return super().parse(path, month)


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


@pytest.mark.parametrize("month", ["2026-5", "2026-00", "2026-13", "26-05"])
def test_run_import_rejects_invalid_month_before_parsing_or_creating_dirs(
    month: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    data_dir = tmp_path / "data"
    parser = SpyParser()

    with pytest.raises(ValueError, match="month.*YYYY-MM"):
        run_import(
            month=month,
            statement_paths={"fake": source},
            parsers=[parser],
            data_dir=data_dir,
            fx_provider=StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")}),
        )

    assert not parser.parse_called
    assert not data_dir.exists()


def test_run_import_failed_rerun_keeps_previous_outputs(tmp_path: Path) -> None:
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
    assert first.run_dir.exists()
    assert first.latest_path.exists()
    original_portfolio = first.portfolio_path.read_text(encoding="utf-8")
    original_latest = first.latest_path.read_text(encoding="utf-8")

    with pytest.raises(KeyError, match="SGD"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source},
            parsers=[FakeParser(position_currency="SGD")],
            data_dir=data_dir,
            fx_provider=fx_provider,
        )

    assert first.run_dir.exists()
    assert first.portfolio_path.read_text(encoding="utf-8") == original_portfolio
    assert first.latest_path.exists()
    assert first.latest_path.read_text(encoding="utf-8") == original_latest


def test_run_import_different_month_failure_keeps_previous_latest(tmp_path: Path) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    data_dir = tmp_path / "data"

    first = run_import(
        month="2026-05",
        statement_paths={"fake": source},
        parsers=[FakeParser()],
        data_dir=data_dir,
        fx_provider=StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")}),
    )
    original_latest = first.latest_path.read_text(encoding="utf-8")

    with pytest.raises(KeyError, match="SGD"):
        run_import(
            month="2026-06",
            statement_paths={"fake": source},
            parsers=[FakeParser(position_currency="SGD")],
            data_dir=data_dir,
            fx_provider=StaticMonthEndFxProvider("2026-06", {"USD": Decimal("7.8")}),
        )

    assert first.latest_path.exists()
    assert first.latest_path.read_text(encoding="utf-8") == original_latest
    assert not (data_dir / "runs" / "2026-06").exists()


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


def test_run_import_rejects_duplicate_parser_brokers(tmp_path: Path) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")

    with pytest.raises(ValueError, match="duplicate.*fake"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source},
            parsers=[FakeParser(), FakeParser()],
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


@pytest.mark.parametrize(
    ("collection", "parser_kwargs"),
    [
        ("positions", {"position_broker": "other"}),
        ("cash_balances", {"cash_broker": "other"}),
        ("warnings", {"warning_broker": "other"}),
    ],
)
def test_run_import_rejects_nested_broker_mismatch(
    collection: str,
    parser_kwargs: dict[str, str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")

    with pytest.raises(ValueError, match=f"{collection}.*other"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source},
            parsers=[FakeParser(**parser_kwargs)],
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


def test_run_import_write_failure_keeps_previous_outputs_and_cleans_temp_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    original_portfolio = first.portfolio_path.read_text(encoding="utf-8")
    original_latest = first.latest_path.read_text(encoding="utf-8")
    real_write_rows = pipeline.write_rows

    def fail_on_cash(path: Path, fieldnames: list[str], rows: object) -> None:
        if path.name == "extracted_cash.csv":
            raise OSError("simulated write failure")
        real_write_rows(path, fieldnames, rows)

    monkeypatch.setattr(pipeline, "write_rows", fail_on_cash)

    with pytest.raises(OSError, match="simulated write failure"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source},
            parsers=[FakeParser()],
            data_dir=data_dir,
            fx_provider=fx_provider,
        )

    assert first.run_dir.exists()
    assert first.portfolio_path.read_text(encoding="utf-8") == original_portfolio
    assert first.latest_path.read_text(encoding="utf-8") == original_latest
    assert list((data_dir / "runs").glob(".2026-05*.tmp")) == []


def test_run_import_latest_copy_failure_keeps_previous_latest_and_run_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    original_portfolio = first.portfolio_path.read_text(encoding="utf-8")
    original_latest = first.latest_path.read_text(encoding="utf-8")
    real_copyfile = pipeline.copyfile

    def fail_latest_copy(src: Path, dst: Path) -> None:
        if dst.parent.name == "latest":
            dst.write_text("partial latest\n", encoding="utf-8")
            raise OSError("simulated latest copy failure")
        real_copyfile(src, dst)

    monkeypatch.setattr(pipeline, "copyfile", fail_latest_copy)

    with pytest.raises(OSError, match="simulated latest copy failure"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source},
            parsers=[FakeParser()],
            data_dir=data_dir,
            fx_provider=fx_provider,
        )

    assert first.run_dir.exists()
    assert first.portfolio_path.read_text(encoding="utf-8") == original_portfolio
    assert first.latest_path.read_text(encoding="utf-8") == original_latest
    assert list((data_dir / "latest").glob(".portfolio.*.tmp")) == []
    assert list((data_dir / "runs").glob(".2026-05*.tmp")) == []


def test_run_import_promotion_rename_failure_restores_previous_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    first.portfolio_path.write_text("previous run\n", encoding="utf-8")
    first.latest_path.write_text("previous latest\n", encoding="utf-8")
    real_rename = Path.rename
    latest_replace_attempted = False

    def fail_temp_run_promotion(self: Path, target: Path) -> Path:
        if self.name.startswith(".2026-05.") and self.suffix == ".tmp":
            raise OSError("simulated run promotion failure")
        return real_rename(self, target)

    real_replace = Path.replace

    def track_latest_replace(self: Path, target: Path) -> Path:
        nonlocal latest_replace_attempted
        if self.name.startswith(".portfolio.") and self.suffix == ".tmp":
            latest_replace_attempted = True
        return real_replace(self, target)

    monkeypatch.setattr(Path, "rename", fail_temp_run_promotion)
    monkeypatch.setattr(Path, "replace", track_latest_replace)

    with pytest.raises(OSError, match="simulated run promotion failure"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source},
            parsers=[FakeParser()],
            data_dir=data_dir,
            fx_provider=fx_provider,
        )

    assert latest_replace_attempted
    assert first.run_dir.exists()
    assert first.portfolio_path.read_text(encoding="utf-8") == "previous run\n"
    assert first.latest_path.read_text(encoding="utf-8") == "previous latest\n"
    assert list((data_dir / "runs").glob(".2026-05*.tmp")) == []
    assert list((data_dir / "runs").glob(".2026-05*.backup")) == []
    assert list((data_dir / "latest").glob(".portfolio.*.tmp")) == []
    assert list((data_dir / "latest").glob(".portfolio.csv.*.backup")) == []


def test_run_import_latest_replace_failure_restores_previous_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    first.portfolio_path.write_text("previous run\n", encoding="utf-8")
    first.latest_path.write_text("previous latest\n", encoding="utf-8")
    real_replace = Path.replace

    def fail_latest_replace(self: Path, target: Path) -> Path:
        if self.name.startswith(".portfolio.") and self.suffix == ".tmp":
            raise OSError("simulated latest replace failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_latest_replace)

    with pytest.raises(OSError, match="simulated latest replace failure"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source},
            parsers=[FakeParser()],
            data_dir=data_dir,
            fx_provider=fx_provider,
        )

    assert first.run_dir.exists()
    assert first.portfolio_path.read_text(encoding="utf-8") == "previous run\n"
    assert first.latest_path.read_text(encoding="utf-8") == "previous latest\n"
    assert list((data_dir / "runs").glob(".2026-05*.tmp")) == []
    assert list((data_dir / "runs").glob(".2026-05*.backup")) == []
    assert list((data_dir / "latest").glob(".portfolio.*.tmp")) == []
    assert list((data_dir / "latest").glob(".portfolio.csv.*.backup")) == []


def test_run_import_rollback_cleanup_failure_preserves_original_error_and_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    first.portfolio_path.write_text("previous run\n", encoding="utf-8")
    first.latest_path.write_text("previous latest\n", encoding="utf-8")
    real_rmtree = pipeline.rmtree

    def fail_backup_and_failed_cleanup(path: Path) -> None:
        if path.suffix == ".backup":
            raise OSError("simulated post-promotion backup cleanup failure")
        if path.suffix == ".failed":
            raise OSError("simulated failed run cleanup failure")
        real_rmtree(path)

    monkeypatch.setattr(pipeline, "rmtree", fail_backup_and_failed_cleanup)

    with pytest.raises(OSError, match="simulated post-promotion backup cleanup failure"):
        run_import(
            month="2026-05",
            statement_paths={"fake": source},
            parsers=[FakeParser()],
            data_dir=data_dir,
            fx_provider=fx_provider,
        )

    assert first.run_dir.exists()
    assert first.portfolio_path.read_text(encoding="utf-8") == "previous run\n"
    assert first.latest_path.read_text(encoding="utf-8") == "previous latest\n"
    assert list((data_dir / "runs").glob(".2026-05*.backup")) == []
    assert list((data_dir / "latest").glob(".portfolio.*.tmp")) == []
    assert list((data_dir / "latest").glob(".portfolio.csv.*.backup")) == []


def test_import_statements_help_includes_usd_hkd(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["import-statements", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--usd-hkd" in output
    assert "--phillips" in output
    assert "--futu" not in output
    assert "--tiger" not in output


@pytest.mark.parametrize("month", ["2026-5", "2026-00", "2026-13", "26-05"])
def test_import_statements_rejects_invalid_month(
    month: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "import-statements",
                "--month",
                month,
                "--phillips",
                "phillips.pdf",
                "--usd-hkd",
                "7.8",
            ]
        )

    assert exc_info.value.code == 2
    assert "invalid month" in capsys.readouterr().err


@pytest.mark.parametrize("rate", ["abc", "0", "-1", "NaN", "Infinity"])
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
                "--phillips",
                "phillips.pdf",
                "--usd-hkd",
                rate,
            ]
        )

    assert exc_info.value.code == 2
    assert "invalid" in capsys.readouterr().err


def test_import_statements_main_calls_pipeline_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_run_import(**kwargs: object) -> ImportResult:
        captured.update(kwargs)
        data_dir = kwargs["data_dir"]
        assert isinstance(data_dir, Path)
        return ImportResult(
            run_dir=data_dir / "runs" / "2026-05",
            portfolio_path=data_dir / "runs" / "2026-05" / "portfolio.csv",
            latest_path=data_dir / "latest" / "portfolio.csv",
            positions_count=3,
            cash_count=2,
            warnings_count=1,
        )

    monkeypatch.setattr(cli, "run_import", fake_run_import)

    result = cli.main(
        [
            "import-statements",
            "--month",
            "2026-05",
            "--phillips",
            "phillips.pdf",
            "--data-dir",
            str(tmp_path / "data"),
            "--usd-hkd",
            "7.8",
        ]
    )

    assert result == 0
    assert captured["month"] == "2026-05"
    assert captured["statement_paths"] == {
        "phillips": Path("phillips.pdf"),
    }
    assert captured["fx_provider"].get_rate_to_hkd("USD").rate == Decimal("7.8")
    output = capsys.readouterr().out
    assert f"portfolio: {tmp_path / 'data' / 'runs' / '2026-05' / 'portfolio.csv'}" in output
    assert f"latest: {tmp_path / 'data' / 'latest' / 'portfolio.csv'}" in output
    assert "positions: 3" in output
    assert "cash: 2" in output
    assert "warnings: 1" in output
