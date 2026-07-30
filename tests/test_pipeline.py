from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import open_trader.cli as cli
import open_trader.pipeline as pipeline
from open_trader.cli import build_parser
from open_trader.fx import StaticMonthEndFxProvider
from open_trader.models import (
    AssetClass,
    CashBalance,
    Market,
    Position,
    TradeFill,
    WarningRecord,
)
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
        symbol: str = "NVDA",
        cash_currency: str = "USD",
        warning_page: int | None = 1,
        position_broker: str | None = None,
        cash_broker: str | None = None,
        warning_broker: str | None = None,
        fills_complete: bool = False,
        fills_coverage_start: str | None = None,
        fills_coverage_end: str | None = None,
    ) -> None:
        self.broker = broker
        self.result_broker = result_broker or broker
        self.position_currency = position_currency
        self.symbol = symbol
        self.cash_currency = cash_currency
        self.warning_page = warning_page
        self.position_broker = position_broker or self.result_broker
        self.cash_broker = cash_broker or self.result_broker
        self.warning_broker = warning_broker or self.result_broker
        self.fills_complete = fills_complete
        self.fills_coverage_start = fills_coverage_start
        self.fills_coverage_end = fills_coverage_end

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
                    symbol=self.symbol,
                    name=self.symbol,
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
                    currency=self.cash_currency,
                    cash_balance=Decimal("50"),
                    available_balance=Decimal("45"),
                    confidence="high",
                    notes="",
                )
            ],
            fills_complete=self.fills_complete,
            fills_coverage_start=self.fills_coverage_start,
            fills_coverage_end=self.fills_coverage_end,
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


class FillParser(FakeParser):
    def parse(self, path: Path, month: str) -> ParseResult:
        result = super().parse(path, month)
        return ParseResult(
            statement_id=result.statement_id,
            broker=result.broker,
            positions=result.positions,
            cash_balances=result.cash_balances,
            fills=[
                TradeFill(
                    source_id="fill-1",
                    source_order_id=None,
                    broker="fake",
                    account_alias="main",
                    market=Market.US,
                    symbol="NVDA",
                    currency="USD",
                    side="BUY",
                    quantity=Decimal("2"),
                    price=Decimal("100"),
                    fees=Decimal("1"),
                    executed_at="2026-05-10",
                    source_sequence=7,
                )
            ],
            fills_complete=True,
            warnings=result.warnings,
            page_count=result.page_count,
        )


class BrokerFillParser(FakeParser):
    def __init__(
        self,
        broker: str,
        market: Market,
        *,
        fills_coverage_start: str | None = None,
        fills_coverage_end: str | None = None,
    ) -> None:
        currency = "CNY" if market is Market.CN else "HKD"
        super().__init__(
            broker=broker,
            position_currency=currency,
            cash_currency=currency,
        )
        self.market = market
        self.fills_coverage_start = fills_coverage_start
        self.fills_coverage_end = fills_coverage_end

    def parse(self, path: Path, month: str) -> ParseResult:
        result = super().parse(path, month)
        return ParseResult(
            statement_id=result.statement_id,
            broker=result.broker,
            positions=result.positions,
            cash_balances=result.cash_balances,
            fills=[
                TradeFill(
                    source_id=f"{self.broker}-fill-1",
                    source_order_id=None,
                    broker=self.broker,
                    account_alias="main",
                    market=self.market,
                    symbol="600000" if self.market is Market.CN else "00700",
                    currency="CNY" if self.market is Market.CN else "HKD",
                    side="BUY",
                    quantity=Decimal("100"),
                    price=Decimal("10"),
                    fees=Decimal("1"),
                    executed_at="2026-05-10",
                )
            ],
            fills_complete=True,
            fills_coverage_start=self.fills_coverage_start,
            fills_coverage_end=self.fills_coverage_end,
            warnings=result.warnings,
            page_count=result.page_count,
        )


class InvalidExecutionBrokerFillParser(BrokerFillParser):
    def parse(self, path: Path, month: str) -> ParseResult:
        result = super().parse(path, month)
        return ParseResult(
            statement_id=result.statement_id,
            broker=result.broker,
            positions=result.positions,
            cash_balances=result.cash_balances,
            fills=result.fills,
            fills_complete=False,
            warnings=[
                WarningRecord(
                    statement_id=result.statement_id,
                    broker=self.broker,
                    page=1,
                    severity="warning",
                    code="invalid_execution_row",
                    message="invalid execution row",
                )
            ],
            page_count=result.page_count,
        )


def test_run_import_writes_candidate_without_replacing_latest_portfolio(tmp_path: Path) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    data_dir = tmp_path / "data"
    latest_path = data_dir / "latest" / "portfolio.csv"
    sentinel = b"accepted portfolio must stay untouched\n"
    latest_path.parent.mkdir(parents=True)
    latest_path.write_bytes(sentinel)
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
    assert result.positions_count == 1
    assert result.cash_count == 1
    assert result.warnings_count == 1

    portfolio_content = result.portfolio_path.read_text(encoding="utf-8")
    assert latest_path.read_bytes() == sentinel
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


def test_uploaded_statement_without_fill_completeness_keeps_audit_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "eastmoney.pdf"
    source.write_bytes(b"statement")
    result = pipeline.run_uploaded_statement(
        statement_date="2026-05-10",
        statement_path=source,
        parser=FakeParser(
            broker="eastmoney", position_currency="CNY", cash_currency="CNY",
            fills_complete=False,
        ),
        data_dir=tmp_path / "data",
        fx_provider=StaticMonthEndFxProvider("2026-05", {"CNY": Decimal("1.08")}),
    )

    assert result.positions_count == 1
    assert (result.run_dir / "manifest.csv").exists()
    assert not (tmp_path / "data/trend_review/facts/actual_fill_completeness").exists()


def test_uploaded_statement_persists_each_fill_once(tmp_path: Path) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    arguments = {
        "statement_date": "2026-05-10",
        "statement_path": source,
        "parser": FillParser(),
        "data_dir": tmp_path / "data",
        "fx_provider": StaticMonthEndFxProvider(
            "2026-05", {"USD": Decimal("7.8")}
        ),
    }

    result = pipeline.run_uploaded_statement(**arguments)
    rows = list(csv.DictReader((result.run_dir / "extracted_fills.csv").open()))
    assert [row["source_id"] for row in rows] == ["fill-1"]
    assert rows[0]["source_sequence"] == "7"

    repeated = pipeline.run_uploaded_statement(**arguments)
    repeated_rows = list(
        csv.DictReader((repeated.run_dir / "extracted_fills.csv").open())
    )
    assert [row["source_id"] for row in repeated_rows] == ["fill-1"]
    assert repeated_rows[0]["source_sequence"] == "7"


@pytest.mark.parametrize(
    ("broker", "market"),
    [("eastmoney", Market.CN), ("phillips", Market.HK)],
)
def test_uploaded_statement_freezes_broker_fills_in_its_market_idempotently(
    broker: str,
    market: Market,
    tmp_path: Path,
) -> None:
    source = tmp_path / f"{broker}.pdf"
    source.write_bytes(b"statement")
    data_dir = tmp_path / "data"
    arguments = {
        "statement_date": "2026-05-10",
        "statement_path": source,
        "parser": BrokerFillParser(
            broker,
            market,
            fills_coverage_start=(
                "2026-05-01" if broker == "eastmoney" else "2026-05-10"
            ),
            fills_coverage_end="2026-05-10",
        ),
        "data_dir": data_dir,
        "fx_provider": StaticMonthEndFxProvider(
            "2026-05", {"CNY": Decimal("1.08"), "HKD": Decimal("1")}
        ),
    }

    pipeline.run_uploaded_statement(**arguments)
    fill_paths = list(
        (data_dir / f"trend_review/facts/actual_fills/{market.value}").glob("*.json")
    )
    assert len(fill_paths) == 1
    first_bytes = fill_paths[0].read_bytes()
    assert not list(
        (data_dir / "trend_review/facts/actual_fills").glob(
            f"{'HK' if market is Market.CN else 'CN'}/*.json"
        )
    )

    pipeline.run_uploaded_statement(**arguments)

    assert fill_paths[0].read_bytes() == first_bytes
    completeness = list(
        (
            data_dir
            / f"trend_review/facts/actual_fill_completeness/{market.value}"
        ).glob("*.json")
    )
    assert len(completeness) == 1
    completeness_payload = json.loads(completeness[0].read_text(encoding="utf-8"))
    assert completeness_payload["source_metadata"]["market"] == market.value
    assert completeness_payload["coverage_start"] == (
        "2026-05-01" if broker == "eastmoney" else "2026-05-10"
    )
    assert completeness_payload["coverage_end"] == "2026-05-10"


def test_uploaded_statement_uses_declared_fill_coverage_start(tmp_path: Path) -> None:
    source = tmp_path / "eastmoney.pdf"
    source.write_bytes(b"statement")
    data_dir = tmp_path / "data"

    pipeline.run_uploaded_statement(
        statement_date="2026-07-16",
        statement_path=source,
        parser=BrokerFillParser(
            "eastmoney",
            Market.CN,
            fills_coverage_start="2026-06-16",
            fills_coverage_end="2026-07-15",
        ),
        data_dir=data_dir,
        fx_provider=StaticMonthEndFxProvider(
            "2026-07", {"CNY": Decimal("1.08")}
        ),
    )

    completeness = next(
        (data_dir / "trend_review/facts/actual_fill_completeness/CN").glob("*.json")
    )
    payload = json.loads(completeness.read_text(encoding="utf-8"))
    assert payload["coverage_start"] == "2026-06-16"
    assert payload["coverage_end"] == "2026-07-15"


def test_uploaded_statement_without_declared_coverage_does_not_advance_facts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "eastmoney.pdf"
    source.write_bytes(b"statement")
    data_dir = tmp_path / "data"

    pipeline.run_uploaded_statement(
        statement_date="2026-07-16",
        statement_path=source,
        parser=BrokerFillParser("eastmoney", Market.CN),
        data_dir=data_dir,
        fx_provider=StaticMonthEndFxProvider(
            "2026-07", {"CNY": Decimal("1.08")}
        ),
    )

    assert not (
        data_dir / "trend_review/facts/actual_fill_completeness"
    ).exists()


@pytest.mark.parametrize(
    ("broker", "market"),
    [("eastmoney", Market.CN), ("phillips", Market.HK)],
)
def test_uploaded_statement_with_invalid_execution_row_does_not_advance_fill_facts(
    broker: str,
    market: Market,
    tmp_path: Path,
) -> None:
    source = tmp_path / f"{broker}.pdf"
    source.write_bytes(b"statement")
    data_dir = tmp_path / "data"

    result = pipeline.run_uploaded_statement(
        statement_date="2026-05-10",
        statement_path=source,
        parser=InvalidExecutionBrokerFillParser(broker, market),
        data_dir=data_dir,
        fx_provider=StaticMonthEndFxProvider(
            "2026-05", {"CNY": Decimal("1.08"), "HKD": Decimal("1")}
        ),
    )

    assert not (data_dir / "trend_review/facts/actual_fills" / market.value).exists()
    assert not (
        data_dir / "trend_review/facts/actual_fill_completeness" / market.value
    ).exists()
    assert list(csv.DictReader((result.run_dir / "extracted_fills.csv").open()))
    warnings = list(
        csv.DictReader((result.run_dir / "parse_warnings.csv").open())
    )
    assert [warning["code"] for warning in warnings] == ["invalid_execution_row"]
    assert result.positions_count == 1


def test_uploaded_empty_statement_freezes_completeness_only_after_success(
    tmp_path: Path,
) -> None:
    source = tmp_path / "eastmoney.pdf"
    source.write_bytes(b"statement")
    data_dir = tmp_path / "data"
    arguments = {
        "statement_date": "2026-05-10",
        "statement_path": source,
        "parser": FakeParser(
            broker="eastmoney",
            position_currency="CNY",
            cash_currency="CNY",
            fills_complete=True,
            fills_coverage_start="2026-05-01",
            fills_coverage_end="2026-05-10",
        ),
        "data_dir": data_dir,
        "fx_provider": StaticMonthEndFxProvider(
            "2026-05", {"CNY": Decimal("1.08")}
        ),
    }

    pipeline.run_uploaded_statement(**arguments)

    completeness = list(
        (data_dir / "trend_review/facts/actual_fill_completeness/CN").glob("*.json")
    )
    assert len(completeness) == 1

    failed_dir = tmp_path / "failed-data"
    with pytest.raises(KeyError, match="SGD"):
        pipeline.run_uploaded_statement(
            **{
                **arguments,
                "data_dir": failed_dir,
                "parser": FakeParser(
                    broker="eastmoney",
                    position_currency="SGD",
                    cash_currency="CNY",
                ),
            }
        )
    assert not (failed_dir / "trend_review/facts").exists()


def test_monthly_import_does_not_guess_fill_completeness_date(tmp_path: Path) -> None:
    source = tmp_path / "eastmoney.pdf"
    source.write_bytes(b"statement")
    data_dir = tmp_path / "data"

    run_import(
        month="2026-05",
        statement_paths={"eastmoney": source},
        parsers=[BrokerFillParser("eastmoney", Market.CN)],
        data_dir=data_dir,
        fx_provider=StaticMonthEndFxProvider(
            "2026-05", {"CNY": Decimal("1.08")}
        ),
    )

    assert not (data_dir / "trend_review/facts").exists()


def test_uploaded_statement_fact_failure_rolls_back_promoted_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "eastmoney.pdf"
    source.write_bytes(b"first statement")
    data_dir = tmp_path / "data"
    arguments = {
        "statement_date": "2026-05-10",
        "statement_path": source,
        "parser": FakeParser(
            broker="eastmoney",
            position_currency="CNY",
            cash_currency="CNY",
            symbol="600001",
            fills_complete=True,
            fills_coverage_start="2026-05-01",
            fills_coverage_end="2026-05-10",
        ),
        "data_dir": data_dir,
        "fx_provider": StaticMonthEndFxProvider(
            "2026-05", {"CNY": Decimal("1.08")}
        ),
    }
    first = pipeline.run_uploaded_statement(**arguments)

    def tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    original_run = tree_bytes(first.run_dir)
    original_facts = tree_bytes(data_dir / "trend_review/facts")
    source.write_bytes(b"second statement")
    arguments["parser"] = FakeParser(
        broker="eastmoney",
        position_currency="CNY",
        cash_currency="CNY",
        symbol="600002",
        fills_complete=True,
        fills_coverage_start="2026-05-01",
        fills_coverage_end="2026-05-10",
    )
    monkeypatch.setattr(
        pipeline,
        "freeze_actual_fill_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("simulated fact writer failure")
        ),
    )

    with pytest.raises(OSError, match="simulated fact writer failure"):
        pipeline.run_uploaded_statement(**arguments)

    assert tree_bytes(first.run_dir) == original_run
    assert tree_bytes(data_dir / "trend_review/facts") == original_facts
    assert list((data_dir / "runs").glob(".2026-05*.backup")) == []


def test_uploaded_statement_commits_before_backup_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "eastmoney.pdf"
    source.write_bytes(b"first statement")
    data_dir = tmp_path / "data"
    arguments = {
        "statement_date": "2026-05-10",
        "statement_path": source,
        "parser": FakeParser(
            broker="eastmoney",
            position_currency="CNY",
            cash_currency="CNY",
            symbol="600001",
            fills_complete=True,
            fills_coverage_start="2026-05-01",
            fills_coverage_end="2026-05-10",
        ),
        "data_dir": data_dir,
        "fx_provider": StaticMonthEndFxProvider(
            "2026-05", {"CNY": Decimal("1.08")}
        ),
    }
    pipeline.run_uploaded_statement(**arguments)
    source.write_bytes(b"second statement")
    arguments["parser"] = FakeParser(
        broker="eastmoney",
        position_currency="CNY",
        cash_currency="CNY",
        symbol="600002",
        fills_complete=True,
        fills_coverage_start="2026-05-01",
        fills_coverage_end="2026-05-10",
    )
    real_rmtree = pipeline.rmtree

    def fail_backup_cleanup(path: Path) -> None:
        if path.suffix == ".backup":
            raise OSError("simulated backup cleanup failure")
        real_rmtree(path)

    monkeypatch.setattr(pipeline, "rmtree", fail_backup_cleanup)

    result = pipeline.run_uploaded_statement(**arguments)

    expected_sha = pipeline.sha256_file(source)
    manifest = list(
        csv.DictReader((result.run_dir / "manifest.csv").open(encoding="utf-8"))
    )
    assert manifest[0]["source_sha256"] == expected_sha
    assert "600002" in result.portfolio_path.read_text(encoding="utf-8")
    completeness = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (
            data_dir / "trend_review/facts/actual_fill_completeness/CN"
        ).glob("*.json")
    ]
    assert any(
        fact["source_metadata"]["source_sha256"] == expected_sha
        for fact in completeness
    )
    assert len(list((data_dir / "runs").glob(".2026-05*.backup"))) == 1


def test_uploaded_statements_for_two_brokers_share_monthly_run(
    tmp_path: Path,
) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    data_dir = tmp_path / "data"
    fx_provider = StaticMonthEndFxProvider(
        "2026-05", {"USD": Decimal("7.8"), "CNY": Decimal("1.08")}
    )

    for broker, currency, symbol in (
        ("eastmoney", "CNY", "600900"),
        ("phillips", "USD", "NVDA"),
    ):
        pipeline.run_uploaded_statement(
            statement_date="2026-05-10",
            statement_path=source,
            parser=FakeParser(
                broker=broker,
                position_currency=currency,
                symbol=symbol,
                cash_currency=currency,
            ),
            data_dir=data_dir,
            fx_provider=fx_provider,
        )

    rows = list(csv.DictReader((data_dir / "runs/2026-05/portfolio.csv").open(encoding="utf-8")))
    assert {row["brokers"] for row in rows} == {"eastmoney", "phillips"}


def test_uploaded_statement_accepts_old_run_without_fills_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"fake pdf contents")
    data_dir = tmp_path / "data"
    fx_provider = StaticMonthEndFxProvider(
        "2026-05", {"USD": Decimal("7.8"), "CNY": Decimal("1.08")}
    )
    first = pipeline.run_uploaded_statement(
        statement_date="2026-05-10",
        statement_path=source,
        parser=FakeParser(
            broker="eastmoney",
            position_currency="CNY",
            cash_currency="CNY",
            symbol="600900",
        ),
        data_dir=data_dir,
        fx_provider=fx_provider,
    )
    (first.run_dir / "extracted_fills.csv").unlink()

    second = pipeline.run_uploaded_statement(
        statement_date="2026-05-10",
        statement_path=source,
        parser=FillParser(),
        data_dir=data_dir,
        fx_provider=fx_provider,
    )

    fills = list(csv.DictReader((second.run_dir / "extracted_fills.csv").open()))
    positions = list(
        csv.DictReader((second.run_dir / "extracted_positions.csv").open())
    )
    assert [row["source_id"] for row in fills] == ["fill-1"]
    assert {row["broker"] for row in positions} == {"eastmoney", "fake"}


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
    )

    assert result.portfolio_path.exists()
    assert latest.read_text(encoding="utf-8") == "sentinel\n"


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
    original_portfolio = first.portfolio_path.read_text(encoding="utf-8")

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
    assert list((data_dir / "runs").glob(".2026-05*.tmp")) == []


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
    assert "--update-latest" not in output
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
        return ImportResult(tmp_path, tmp_path / "portfolio.csv", 1, 0, 0)

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
        return ImportResult(tmp_path, tmp_path / "portfolio.csv", 1, 0, 0)

    monkeypatch.setattr(cli, "getpass", lambda _: "secret")
    monkeypatch.setattr(cli, "run_import", fake_run_import)
    assert cli.main([
        "import-statements", "--month", "2026-07", "--eastmoney", str(statement),
        "--cny-hkd", "1.08", "--fx-date", "2026-06-30",
        "--data-dir", str(tmp_path),
    ]) == 0

    assert captured["statement_paths"] == {"eastmoney": statement}
    assert [parser.broker for parser in captured["parsers"]] == ["eastmoney"]
    assert captured["fx_provider"].get_rate_to_hkd("CNY").rate == Decimal("1.08")
    assert captured["fx_provider"].get_rate_to_hkd("CNY").fx_date == "2026-06-30"
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
        return ImportResult(tmp_path, tmp_path / "portfolio.csv", 1, 0, 0)

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
        return ImportResult(tmp_path, tmp_path / "portfolio.csv", 1, 0, 0)

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
        return ImportResult(tmp_path, tmp_path / "portfolio.csv", 1, 0, 0)

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
    assert "positions: 3" in output
    assert "cash: 2" in output
    assert "warnings: 1" in output


def test_retired_tiger_strategy_cli_is_rejected() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run-tiger-long-term-strategy"])
