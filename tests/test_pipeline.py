from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import open_trader.cli as cli
import open_trader.pipeline as pipeline
from open_trader.cli import build_parser
from open_trader.fx import StaticMonthEndFxProvider
from open_trader.models import AssetClass, CashBalance, Market, Position, WarningRecord
from open_trader.parsers.base import ParseResult
from open_trader.pipeline import ImportResult, run_import
from open_trader.portfolio import PORTFOLIO_FIELDNAMES


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
    fx_provider = StaticMonthEndFxProvider(
        "2026-05", {"USD": Decimal("7.8")}, fx_date="2026-04-30"
    )

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
    assert {
        row["fx_date"] for row in csv.DictReader(result.portfolio_path.open(encoding="utf-8"))
    } == {"2026-04-30"}

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


def test_run_import_can_leave_latest_untouched(tmp_path: Path) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    latest = tmp_path / "data" / "latest" / "portfolio.csv"
    latest.parent.mkdir(parents=True)
    latest.write_text("sentinel\n", encoding="utf-8")

    result = run_import(
        month="2026-05",
        statement_paths={"fake": source},
        parsers=[FakeParser()],
        data_dir=tmp_path / "data",
        fx_provider=StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.8")}),
        update_latest=False,
    )

    assert result.portfolio_path.exists()
    assert latest.read_text(encoding="utf-8") == "sentinel\n"


def test_run_uploaded_statement_rebuilds_mixed_rows_from_broker_details(
    tmp_path: Path,
) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    data_dir = tmp_path / "data"
    monthly_run = data_dir / "runs" / "2026-07"
    daily_run = data_dir / "runs" / "2026-07-16"
    monthly_run.mkdir(parents=True)
    daily_run.mkdir(parents=True)

    def detail_position(
        broker: str, symbol: str, currency: str, market: str,
    ) -> dict[str, str]:
        return {
            "statement_id": f"2026-07-{broker}",
            "broker": broker,
            "account_alias": f"{broker}_main",
            "market": market,
            "asset_class": "stock",
            "symbol": symbol,
            "name": symbol,
            "currency": currency,
            "quantity": "1",
            "cost_price": "80",
            "last_price": "100",
            "market_value": "100",
            "cost_value": "80",
            "unrealized_pnl": "20",
            "confidence": "high",
            "notes": "",
        }

    def detail_cash(broker: str, currency: str, value: str) -> dict[str, str]:
        return {
            "statement_id": f"2026-07-{broker}",
            "broker": broker,
            "account_alias": f"{broker}_main",
            "currency": currency,
            "cash_balance": value,
            "available_balance": value,
            "confidence": "high",
            "notes": "",
        }

    def write_detail_rows(
        run_dir: Path,
        positions: list[dict[str, str]],
        cash: list[dict[str, str]],
    ) -> None:
        for filename, fieldnames, rows in (
            ("extracted_positions.csv", pipeline.POSITION_FIELDNAMES, positions),
            ("extracted_cash.csv", pipeline.CASH_FIELDNAMES, cash),
        ):
            with (run_dir / filename).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

    write_detail_rows(
        monthly_run,
        [
            detail_position("phillips", "OLD", "USD", "US"),
            detail_position("eastmoney", "600519", "CNY", "CN"),
        ],
        [detail_cash("phillips", "USD", "30"), detail_cash("eastmoney", "CNY", "40")],
    )
    write_detail_rows(
        daily_run,
        [
            detail_position("futu", "AAPL", "USD", "US"),
            detail_position("tiger", "TSLA", "USD", "US"),
            detail_position("phillips", "OLD", "USD", "US"),
            detail_position("eastmoney", "600519", "CNY", "CN"),
        ],
        [
            detail_cash("futu", "USD", "10"),
            detail_cash("tiger", "USD", "20"),
            detail_cash("phillips", "USD", "30"),
            detail_cash("eastmoney", "CNY", "40"),
        ],
    )
    (daily_run / "futu_account_snapshot.json").write_text(
        "sentinel", encoding="utf-8"
    )

    portfolio_path = tmp_path / "custom" / "portfolio.csv"
    portfolio_path.parent.mkdir(parents=True)
    existing_rows: list[dict[str, str]] = []
    for symbol, broker, currency, market in (
        ("AAPL", "futu", "USD", "US"),
        ("TSLA", "tiger", "USD", "US"),
        ("OLD", "phillips", "USD", "US"),
        ("600519", "eastmoney", "CNY", "CN"),
    ):
        row = {field: "" for field in PORTFOLIO_FIELDNAMES}
        row.update(
            {
                "sort_group": "2",
                "market": market,
                "asset_class": "stock",
                "symbol": symbol,
                "currency": currency,
                "market_value": "100",
                "cost_value": "80",
                "fx_to_hkd": "1.08" if currency == "CNY" else "7.8",
                "brokers": broker,
                "risk_flag": "normal",
            }
        )
        existing_rows.append(row)
    mixed_cash = {field: "" for field in PORTFOLIO_FIELDNAMES}
    mixed_cash.update({
        "sort_group": "7", "market": "CASH", "asset_class": "cash",
        "symbol": "USD_CASH", "currency": "USD", "market_value": "60",
        "fx_to_hkd": "7.8", "brokers": "futu;phillips;tiger",
        "accounts": "futu_main;phillips_main;tiger_main", "risk_flag": "normal",
    })
    existing_rows.append(mixed_cash)
    with portfolio_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(existing_rows)

    result = pipeline.run_uploaded_statement(
        statement_date="2026-07-10",
        statement_path=source,
        parser=FakeParser(broker="phillips"),
        data_dir=data_dir,
        portfolio_path=portfolio_path,
        fx_provider=StaticMonthEndFxProvider(
            "2026-07",
            {"USD": Decimal("7.8"), "CNY": Decimal("1.08")},
            fx_date="2026-07-10",
        ),
    )

    assert result.run_dir == monthly_run
    assert result.latest_path == portfolio_path
    assert result.positions_count == 1
    rows = list(csv.DictReader(portfolio_path.open(encoding="utf-8")))
    assert {(row["brokers"], row["symbol"]) for row in rows} >= {
        ("futu", "AAPL"), ("tiger", "TSLA"),
        ("eastmoney", "600519"), ("phillips", "NVDA"),
        ("futu;phillips;tiger", "USD_CASH"),
    }
    usd_cash = next(row for row in rows if row["symbol"] == "USD_CASH")
    assert usd_cash["market_value"] == "80"
    detail_rows = list(
        csv.DictReader(
            (result.run_dir / "extracted_positions.csv").open(encoding="utf-8")
        )
    )
    assert {(row["broker"], row["symbol"]) for row in detail_rows} == {
        ("eastmoney", "600519"), ("phillips", "NVDA"),
    }
    assert next(row for row in detail_rows if row["broker"] == "phillips")[
        "statement_id"
    ] == "2026-07-10-phillips"
    assert (daily_run / "futu_account_snapshot.json").read_text(
        encoding="utf-8"
    ) == "sentinel"


def test_eastmoney_import_counts_all_combined_non_cash_holdings(tmp_path: Path) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    latest = tmp_path / "data" / "latest" / "portfolio.csv"
    latest.parent.mkdir(parents=True)
    existing = {field: "" for field in PORTFOLIO_FIELDNAMES}
    existing.update({
        "sort_group": "2", "market": "US", "asset_class": "stock", "symbol": "AAPL",
        "currency": "USD", "market_value": "100", "cost_value": "80", "fx_to_hkd": "7.8",
        "brokers": "futu", "risk_flag": "normal",
    })
    with latest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerow(existing)

    result = run_import(
        month="2026-05",
        statement_paths={"eastmoney": source},
        parsers=[FakeParser(broker="eastmoney", position_currency="CNY")],
        data_dir=tmp_path / "data",
        fx_provider=StaticMonthEndFxProvider(
            "2026-05", {"CNY": Decimal("1.08"), "USD": Decimal("7.8")}
        ),
        update_latest=False,
    )

    assert result.positions_count == 2


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
    assert "--eastmoney" in output
    assert "--config" in output
    assert "--cny-hkd" in output
    assert "--fx-date" in output
    assert "--update-latest" in output
    assert "--futu" not in output
    assert "--tiger" not in output


def test_import_statements_requires_a_statement(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main([
            "import-statements", "--month", "2026-07",
            "--config", str(tmp_path / "missing.env"),
        ])
    assert exc_info.value.code == 2
    assert "OPEN_TRADER_EASTMONEY_STATEMENT" in capsys.readouterr().err


def test_cli_imports_phillips_and_eastmoney_together(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    eastmoney_path = tmp_path / "eastmoney.pdf"
    eastmoney_path.write_bytes(b"fake pdf contents")
    captured: dict[str, object] = {}

    def fake_run_import(**kwargs: object) -> ImportResult:
        captured.update(kwargs)
        return ImportResult(tmp_path, tmp_path / "portfolio.csv", tmp_path / "latest.csv", 1, 0, 0)

    monkeypatch.setattr(cli, "getpass", lambda _: "test-password")
    monkeypatch.setattr(cli, "run_import", fake_run_import)

    assert cli.main([
        "import-statements", "--month", "2026-07", "--phillips", "phillips.pdf",
        "--usd-hkd", "7.8", "--eastmoney", str(eastmoney_path), "--cny-hkd", "1.08",
    ]) == 0

    assert captured["statement_paths"] == {
        "phillips": Path("phillips.pdf"),
        "eastmoney": eastmoney_path,
    }
    assert [parser.broker for parser in captured["parsers"]] == ["phillips", "eastmoney"]
    assert captured["fx_provider"].get_rate_to_hkd("USD").rate == Decimal("7.8")
    assert captured["fx_provider"].get_rate_to_hkd("CNY").rate == Decimal("1.08")


@pytest.mark.parametrize(
    "arguments,missing_rate",
    [(["--eastmoney", "eastmoney.pdf"], "--cny-hkd"), (["--phillips", "phillips.pdf"], "--usd-hkd")],
)
def test_import_statements_requires_rate_for_selected_broker(
    arguments: list[str], missing_rate: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["import-statements", "--month", "2026-07", *arguments])
    assert exc_info.value.code == 2
    assert missing_rate in capsys.readouterr().err


def test_cli_imports_only_eastmoney_and_prompts_password(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    statement = tmp_path / "statement.pdf"
    statement.write_bytes(b"fake pdf contents")
    captured: dict[str, object] = {}

    def fake_run_import(**kwargs: object) -> ImportResult:
        captured.update(kwargs)
        return ImportResult(tmp_path, tmp_path / "portfolio.csv", tmp_path / "latest.csv", 1, 0, 0)

    monkeypatch.setattr(cli, "getpass", lambda _: "secret")
    monkeypatch.setattr(cli, "run_import", fake_run_import)
    assert cli.main([
        "import-statements", "--month", "2026-07", "--eastmoney", str(statement),
        "--cny-hkd", "1.08", "--fx-date", "2026-06-30",
        "--data-dir", str(tmp_path), "--update-latest",
    ]) == 0

    assert captured["statement_paths"] == {"eastmoney": statement}
    assert [parser.broker for parser in captured["parsers"]] == ["eastmoney"]
    assert captured["fx_provider"].get_rate_to_hkd("CNY").rate == Decimal("1.08")
    assert captured["fx_provider"].get_rate_to_hkd("CNY").fx_date == "2026-06-30"
    assert captured["update_latest"] is True
    assert "secret" not in capsys.readouterr().out


def test_cli_imports_eastmoney_path_and_password_from_local_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    statement = tmp_path / "statement.pdf"
    statement.write_bytes(b"fake pdf contents")
    config = tmp_path / "daily.env"
    password = "test-password"
    config.write_text(
        f"OPEN_TRADER_EASTMONEY_STATEMENT={statement}\n"
        f"OPEN_TRADER_EASTMONEY_PDF_PASSWORD={password}\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_import(**kwargs: object) -> ImportResult:
        captured.update(kwargs)
        return ImportResult(tmp_path, tmp_path / "portfolio.csv", tmp_path / "latest.csv", 1, 0, 0)

    def fake_eastmoney_parser(value: str) -> object:
        captured["password"] = value
        return type("FakeEastmoneyParser", (), {"broker": "eastmoney"})()

    monkeypatch.setattr(cli, "getpass", lambda _: pytest.fail("getpass should not be called"))
    monkeypatch.setattr(cli, "EastmoneyStatementParser", fake_eastmoney_parser)
    monkeypatch.setattr(cli, "run_import", fake_run_import)

    assert cli.main([
        "import-statements", "--month", "2026-07", "--config", str(config), "--cny-hkd", "1.08",
    ]) == 0

    assert captured["statement_paths"] == {"eastmoney": statement}
    assert captured["password"] == password


def test_cli_explicit_eastmoney_path_overrides_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    statement = tmp_path / "explicit.pdf"
    statement.write_bytes(b"fake pdf contents")
    config = tmp_path / "daily.env"
    password = "test-password"
    config.write_text(
        "OPEN_TRADER_EASTMONEY_STATEMENT=/missing/configured.pdf\n"
        f"OPEN_TRADER_EASTMONEY_PDF_PASSWORD={password}\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_import(**kwargs: object) -> ImportResult:
        captured.update(kwargs)
        return ImportResult(tmp_path, tmp_path / "portfolio.csv", tmp_path / "latest.csv", 1, 0, 0)

    def fake_eastmoney_parser(value: str) -> object:
        captured["password"] = value
        return type("FakeEastmoneyParser", (), {"broker": "eastmoney"})()

    monkeypatch.setattr(cli, "getpass", lambda _: pytest.fail("getpass should not be called"))
    monkeypatch.setattr(cli, "EastmoneyStatementParser", fake_eastmoney_parser)
    monkeypatch.setattr(cli, "run_import", fake_run_import)

    assert cli.main([
        "import-statements", "--month", "2026-07", "--config", str(config),
        "--eastmoney", str(statement), "--cny-hkd", "1.08",
    ]) == 0

    assert captured["statement_paths"] == {"eastmoney": statement}
    assert captured["password"] == password


def test_cli_prompts_when_config_password_is_blank(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    statement = tmp_path / "statement.pdf"
    statement.write_bytes(b"fake pdf contents")
    config = tmp_path / "daily.env"
    config.write_text(
        f"OPEN_TRADER_EASTMONEY_STATEMENT={statement}\n"
        "OPEN_TRADER_EASTMONEY_PDF_PASSWORD=   \n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_import(**kwargs: object) -> ImportResult:
        captured.update(kwargs)
        return ImportResult(tmp_path, tmp_path / "portfolio.csv", tmp_path / "latest.csv", 1, 0, 0)

    def fake_eastmoney_parser(value: str) -> object:
        captured["password"] = value
        return type("FakeEastmoneyParser", (), {"broker": "eastmoney"})()

    monkeypatch.setattr(cli, "getpass", lambda _: "prompted-password")
    monkeypatch.setattr(cli, "EastmoneyStatementParser", fake_eastmoney_parser)
    monkeypatch.setattr(cli, "run_import", fake_run_import)

    assert cli.main([
        "import-statements", "--month", "2026-07", "--config", str(config), "--cny-hkd", "1.08",
    ]) == 0

    assert captured["password"] == "prompted-password"


def test_cli_rejects_missing_configured_statement_without_leaking_password(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_path = tmp_path / "missing.pdf"
    config = tmp_path / "daily.env"
    password = "test-password"
    config.write_text(
        f"OPEN_TRADER_EASTMONEY_STATEMENT={missing_path}\n"
        f"OPEN_TRADER_EASTMONEY_PDF_PASSWORD={password}\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main([
            "import-statements", "--month", "2026-07", "--config", str(config), "--cny-hkd", "1.08",
        ])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert str(missing_path) in error
    assert password not in error


def test_daily_premarket_env_example_has_empty_eastmoney_placeholders() -> None:
    values = (Path(__file__).parents[1] / "config/daily_premarket.env.example").read_text(
        encoding="utf-8"
    ).splitlines()

    assert "OPEN_TRADER_EASTMONEY_STATEMENT=" in values
    assert "OPEN_TRADER_EASTMONEY_PDF_PASSWORD=" in values


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


def test_import_statements_rejects_invalid_fx_date(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([
            "import-statements", "--month", "2026-07", "--eastmoney", "statement.pdf",
            "--cny-hkd", "1.08", "--fx-date", "2026-06-31",
        ])

    assert exc_info.value.code == 2
    assert "invalid date" in capsys.readouterr().err


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


def test_retired_tiger_strategy_cli_is_rejected() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run-tiger-long-term-strategy"])
