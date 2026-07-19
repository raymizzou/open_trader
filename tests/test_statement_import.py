from __future__ import annotations

import csv
from dataclasses import replace
from decimal import Decimal
import importlib
import json
from pathlib import Path

import pytest

from open_trader.models import (
    AssetClass,
    CashBalance,
    Market,
    Position,
    StatementTrade,
)
from open_trader.parsers.base import ParseResult
from open_trader.portfolio import PORTFOLIO_FIELDNAMES
from open_trader.trend_api_stats import (
    build_statement_actual_stats_payload,
    load_trend_api_stats,
    write_trend_api_stats,
)


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


class FakeTradePhillipsParser(FakePhillipsParser):
    def __init__(self, statement_date: str = "2026-07-12") -> None:
        super().__init__(statement_date)
        self.sell_price = Decimal("12")
        self.include_trades = True

    def parse(self, path: Path, period: str) -> ParseResult:
        parsed = super().parse(path, period)
        if not self.include_trades:
            return parsed
        return replace(
            parsed,
            trades=[
                StatementTrade(
                    statement_id=parsed.statement_id,
                    broker="phillips",
                    account_alias="phillips_main",
                    market=Market.HK,
                    symbol="00700",
                    currency="HKD",
                    side="buy",
                    quantity=Decimal("10"),
                    price=Decimal("10"),
                    fee=Decimal("1"),
                    costs_complete=True,
                    traded_at="2026-07-10T16:00:00+08:00",
                    reference="buy-1",
                    execution_granularity="statement_trade_date",
                    statement_sequence=1,
                ),
                StatementTrade(
                    statement_id=parsed.statement_id,
                    broker="phillips",
                    account_alias="phillips_main",
                    market=Market.HK,
                    symbol="00700",
                    currency="HKD",
                    side="sell",
                    quantity=Decimal("10"),
                    price=self.sell_price,
                    fee=Decimal("1"),
                    costs_complete=True,
                    traded_at="2026-07-11T16:00:00+08:00",
                    reference="sell-1",
                    execution_granularity="statement_trade_date",
                    statement_sequence=2,
                ),
            ],
        )


def write_hk_strategy_reports(reports_dir: Path) -> None:
    directory = reports_dir / "trend_hk_phillips"
    directory.mkdir(parents=True)
    for execution_date, action in (
        ("2026-07-10", "BUY"),
        ("2026-07-11", "SELL_ALL"),
    ):
        (directory / f"{execution_date}.json").write_text(
            json.dumps(
                {
                    "execution_date": execution_date,
                    "metadata": {"market": "HK"},
                    "strategy_snapshot": {
                        "strategy_id": "trend_animals_warm_to_hot/HK/v4",
                        "strategy_version": "v4",
                        "parameters": {},
                    },
                    "strategy_judgments": {
                        "formal_actions": [
                            {"action": action, "symbol": "00700"}
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )


def tree_bytes(path: Path) -> dict[str, bytes]:
    return {
        str(file.relative_to(path)): file.read_bytes()
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


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
        "trades": 0,
        "actual_rounds": 0,
        "statistics_cutoff_at": "2026-07-10T23:59:59+08:00",
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


def test_latest_statement_period_uses_newest_statement_id_across_runs(
    tmp_path: Path,
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    data_dir = tmp_path / "data"
    for run_name, statement_id in (
        ("2026-07-16", "2026-07-10-phillips"),
        ("2026-07", "2026-07-15-phillips"),
    ):
        path = data_dir / "runs" / run_name / "extracted_positions.csv"
        path.parent.mkdir(parents=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["statement_id", "broker"])
            writer.writeheader()
            writer.writerow({"statement_id": statement_id, "broker": "phillips"})
    service = statement_import.StatementImportService(
        data_dir=data_dir,
        portfolio_path=tmp_path / "portfolio.csv",
        eastmoney_password="secret",
    )

    assert service._latest_statement_period("phillips") == "2026-07-15"


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


def test_same_month_eastmoney_then_phillips_upload_keeps_both_brokers(
    tmp_path: Path, monkeypatch
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    monkeypatch.setattr(
        statement_import, "EastmoneyStatementParser", FakeEastmoneyParser
    )
    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", FakePhillipsParser
    )
    portfolio_path = tmp_path / "portfolio.csv"
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )

    service.import_pdf("eastmoney", PDF_BYTES)
    service.import_pdf("phillips", PDF_BYTES)

    rows = list(csv.DictReader(portfolio_path.open(encoding="utf-8")))
    assert {row["brokers"] for row in rows} == {"eastmoney", "phillips"}


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


def test_statement_upload_immediately_rebuilds_actual_stats_and_is_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    parser = FakeTradePhillipsParser()
    monkeypatch.setattr(statement_import, "PhillipsStatementParser", lambda: parser)
    reports_dir = tmp_path / "reports"
    write_hk_strategy_reports(reports_dir)
    portfolio_path = tmp_path / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        reports_dir=reports_dir,
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )

    first = service.import_pdf("phillips", PDF_BYTES)
    first_payload = load_trend_api_stats(tmp_path / "data")
    second = service.import_pdf("phillips", PDF_BYTES)
    second_payload = load_trend_api_stats(tmp_path / "data")

    assert first["trades"] == 2
    assert first["actual_rounds"] == 1
    assert first["statistics_cutoff_at"] == "2026-07-12T23:59:59+08:00"
    assert second["actual_rounds"] == 1
    assert len(second_payload["fills"]) == 2
    assert len(second_payload["rounds"]) == 1
    assert second_payload["rounds"] == first_payload["rounds"]
    actual = next(
        stat
        for stat in second_payload["stats"]
        if stat["source"] == "actual"
        and stat["market"] == "HK"
        and stat["opening_strategy_version"] == "v4"
    )
    assert actual["eligible_sample_count"] == 1
    assert actual["win_rate"] == "1"
    assert actual["payoff_ratio_status"] == "no_losses"


def test_corrected_statement_replaces_period_facts_instead_of_appending(
    tmp_path: Path, monkeypatch
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    parser = FakeTradePhillipsParser()
    monkeypatch.setattr(statement_import, "PhillipsStatementParser", lambda: parser)
    reports_dir = tmp_path / "reports"
    write_hk_strategy_reports(reports_dir)
    portfolio_path = tmp_path / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        reports_dir=reports_dir,
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )
    service.import_pdf("phillips", PDF_BYTES)
    parser.sell_price = Decimal("9")

    service.import_pdf("phillips", b"%PDF-1.7\ncorrected")
    payload = load_trend_api_stats(tmp_path / "data")

    assert len(payload["fills"]) == 2
    assert len(payload["rounds"]) == 1
    assert payload["rounds"][0]["sell_notional"] == "90"
    actual = next(
        stat
        for stat in payload["stats"]
        if stat["source"] == "actual" and stat["market"] == "HK"
    )
    assert actual["eligible_sample_count"] == 1
    assert actual["win_rate"] == "0"
    assert actual["payoff_ratio_status"] == "no_wins"


def test_new_statement_without_trades_keeps_samples_and_advances_source_cutoff(
    tmp_path: Path, monkeypatch
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    parser = FakeTradePhillipsParser()
    monkeypatch.setattr(statement_import, "PhillipsStatementParser", lambda: parser)
    reports_dir = tmp_path / "reports"
    write_hk_strategy_reports(reports_dir)
    portfolio_path = tmp_path / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        reports_dir=reports_dir,
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )
    service.import_pdf("phillips", PDF_BYTES)
    parser.detected_date = "2026-07-13"
    parser.include_trades = False

    result = service.import_pdf("phillips", b"%PDF-1.7\nno trades")
    payload = load_trend_api_stats(tmp_path / "data")

    assert result["trades"] == 0
    assert result["statistics_cutoff_at"] == "2026-07-13T23:59:59+08:00"
    assert len(payload["fills"]) == 2
    assert len(payload["rounds"]) == 1
    source = next(
        source for source in payload["sources"] if source["broker"] == "phillips"
    )
    assert source["statistics_cutoff_at"] == "2026-07-13T23:59:59+08:00"


def test_stats_write_failure_rolls_back_archive_portfolio_run_and_stats(
    tmp_path: Path, monkeypatch
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    parser = FakeTradePhillipsParser()
    monkeypatch.setattr(statement_import, "PhillipsStatementParser", lambda: parser)
    reports_dir = tmp_path / "reports"
    write_hk_strategy_reports(reports_dir)
    portfolio_path = tmp_path / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    service = statement_import.StatementImportService(
        data_dir=data_dir,
        reports_dir=reports_dir,
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )
    service.import_pdf("phillips", PDF_BYTES)
    before_portfolio = portfolio_path.read_bytes()
    before_data = tree_bytes(data_dir)
    parser.sell_price = Decimal("9")
    monkeypatch.setattr(
        statement_import,
        "write_trend_api_stats",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stats failed")),
    )

    with pytest.raises(RuntimeError, match="stats failed"):
        service.import_pdf("phillips", b"%PDF-1.7\ncorrected")

    assert portfolio_path.read_bytes() == before_portfolio
    assert tree_bytes(data_dir) == before_data


def test_same_day_statement_buy_and_sell_are_excluded_when_time_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")

    class SameDayParser(FakeTradePhillipsParser):
        def parse(self, path: Path, period: str) -> ParseResult:
            parsed = super().parse(path, period)
            return replace(
                parsed,
                trades=[
                    parsed.trades[0],
                    replace(
                        parsed.trades[1],
                        traded_at="2026-07-10T16:00:00+08:00",
                    ),
                ],
            )

    parser = SameDayParser()
    monkeypatch.setattr(statement_import, "PhillipsStatementParser", lambda: parser)
    reports_dir = tmp_path / "reports"
    report_dir = reports_dir / "trend_hk_phillips"
    report_dir.mkdir(parents=True)
    (report_dir / "2026-07-10.json").write_text(
        json.dumps({
            "execution_date": "2026-07-10",
            "metadata": {"market": "HK"},
            "strategy_snapshot": {
                "strategy_id": "trend_animals_warm_to_hot/HK/v4",
                "strategy_version": "v4",
            },
            "strategy_judgments": {"formal_actions": [
                {"action": "BUY", "symbol": "00700"},
                {"action": "SELL_ALL", "symbol": "00700"},
            ]},
        }),
        encoding="utf-8",
    )
    portfolio_path = tmp_path / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        reports_dir=reports_dir,
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )

    service.import_pdf("phillips", PDF_BYTES)
    payload = load_trend_api_stats(tmp_path / "data")

    assert {fill["attribution_status"] for fill in payload["fills"]} == {"ambiguous"}
    assert {fill["exclusion_reason"] for fill in payload["fills"]} == {
        "statement_trade_time_unavailable"
    }
    actual = next(
        stat
        for stat in payload["stats"]
        if stat["source"] == "actual" and stat["market"] == "HK"
    )
    assert actual["eligible_sample_count"] == 0


def test_statement_parse_failure_preserves_previous_stats_and_cutoff(
    tmp_path: Path, monkeypatch
) -> None:
    statement_import = importlib.import_module("open_trader.statement_import")
    parser = FakeTradePhillipsParser()
    monkeypatch.setattr(statement_import, "PhillipsStatementParser", lambda: parser)
    reports_dir = tmp_path / "reports"
    write_hk_strategy_reports(reports_dir)
    portfolio_path = tmp_path / "portfolio.csv"
    write_existing_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    service = statement_import.StatementImportService(
        data_dir=data_dir,
        reports_dir=reports_dir,
        portfolio_path=portfolio_path,
        eastmoney_password="secret",
    )
    service.import_pdf("phillips", PDF_BYTES)
    before = (data_dir / "latest/trend_api_stats.json").read_bytes()
    parser.parse = lambda path, period: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ValueError("辉立成交表格式无法识别")
    )

    with pytest.raises(ValueError, match="成交表格式无法识别"):
        service.import_pdf("phillips", b"%PDF-1.7\nbroken")

    assert (data_dir / "latest/trend_api_stats.json").read_bytes() == before
    payload = load_trend_api_stats(data_dir)
    source = next(
        source for source in payload["sources"] if source["broker"] == "phillips"
    )
    assert source["statistics_cutoff_at"] == "2026-07-12T23:59:59+08:00"


def test_overlapping_statement_period_moves_identical_source_fact_without_duplicate(
    tmp_path: Path,
) -> None:
    def source_fill(period: str) -> dict[str, object]:
        return {
            "fill_id": "statement:eastmoney:stable-trade",
            "order_id": "statement:eastmoney:stable-trade",
            "source": "actual",
            "source_id": "actual:eastmoney:eastmoney_main",
            "broker": "eastmoney",
            "account_id": "eastmoney_main",
            "market": "CN",
            "symbol": "600000",
            "currency": "CNY",
            "side": "buy",
            "quantity": "100",
            "price": "10",
            "fee": "5",
            "costs_complete": True,
            "filled_at": "2026-07-10T15:00:00+08:00",
            "execution_granularity": "statement_trade_date",
            "timestamp_semantics": "market_close_ordering_sentinel",
            "statement_sequence": 1,
            "statement_period": period,
            "strategy_id": "",
            "strategy_version": "",
            "report_sha256": "",
            "attribution_status": "outside_strategy",
            "exclusion_reason": "no_matching_opening_strategy_action",
        }

    july = build_statement_actual_stats_payload(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        broker="eastmoney",
        statement_period="2026-07",
        fills=[source_fill("2026-07")],
        generated_at="2026-07-31T23:59:59+08:00",
        statistics_cutoff_at="2026-07-31T23:59:59+08:00",
    )
    write_trend_api_stats(tmp_path / "data", july)

    august = build_statement_actual_stats_payload(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        broker="eastmoney",
        statement_period="2026-08",
        fills=[source_fill("2026-08")],
        generated_at="2026-08-31T23:59:59+08:00",
        statistics_cutoff_at="2026-08-31T23:59:59+08:00",
    )

    assert len(august["fills"]) == 1
    assert august["fills"][0]["statement_period"] == "2026-08"
