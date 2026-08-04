from __future__ import annotations

from datetime import datetime
from decimal import Decimal
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


PDF_BYTES = b"%PDF-1.7\nfake statement"


class FakePhillipsParser:
    broker = "phillips"
    parser_version = "test-1"

    def __init__(self, statement_date: str = "2026-07-12") -> None:
        self.detected_date = statement_date
        self.quantity = Decimal("1")

    def statement_date(self, _path: Path) -> str:
        return self.detected_date

    def parse(self, _path: Path, period: str) -> ParseResult:
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
                    quantity=self.quantity,
                    cost_price=Decimal("500"),
                    last_price=Decimal("510"),
                    market_value=Decimal("510") * self.quantity,
                    cost_value=Decimal("500") * self.quantity,
                    unrealized_pnl=Decimal("10") * self.quantity,
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
            trades=[
                StatementTrade(
                    statement_id=statement_id,
                    broker="phillips",
                    account_alias="phillips_main",
                    market=Market.HK,
                    symbol="00700",
                    currency="HKD",
                    side="buy",
                    quantity=Decimal("1"),
                    price=Decimal("500"),
                    fee=Decimal("1"),
                    costs_complete=True,
                    traded_at="2026-07-10T16:00:00+08:00",
                    reference="buy-1",
                    execution_granularity="statement_trade_date",
                    statement_sequence=1,
                )
            ],
        )


class FakeEastmoneyParser:
    broker = "eastmoney"
    parser_version = "test-1"
    passwords: list[str] = []

    def __init__(self, password: str) -> None:
        self.passwords.append(password)

    def statement_date(self, _path: Path) -> str:
        return "2026-07-31"

    def parse(self, _path: Path, period: str) -> ParseResult:
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
        )


def test_stage_pdf_publishes_one_immutable_generation_without_current_or_trend_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.statement_import as statement_import

    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", FakePhillipsParser
    )
    data_dir = tmp_path / "data"
    current = data_dir / "latest/account_sync_state.json"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"accepted-account-state\n")
    service = statement_import.StatementImportService(
        data_dir=data_dir,
        eastmoney_password="secret",
    )

    first = service.stage_pdf("phillips", PDF_BYTES)
    second = service.stage_pdf("phillips", PDF_BYTES)

    assert first == second
    assert first["status"] == "staged"
    assert first["statement_generation"].startswith("sha256:")
    generation = first["statement_generation"].removeprefix("sha256:")
    root = data_dir / "account_statements/generations/phillips" / generation
    assert (root / "statement.pdf").read_bytes() == PDF_BYTES
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["statement_generation"] == first["statement_generation"]
    assert len(json.loads((root / "trade_facts.json").read_text(encoding="utf-8"))) == 1
    assert (root / "candidate/runs/2026-07/extracted_positions.csv").is_file()
    assert current.read_bytes() == b"accepted-account-state\n"
    assert not (data_dir / "latest/trend_api_stats.json").exists()
    assert [path.name for path in root.parent.iterdir()] == [generation]
    candidate, promoted_generation = (
        statement_import.load_staged_statement_candidate(data_dir, "phillips")
    )
    assert promoted_generation == first["statement_generation"]
    assert candidate.period == "2026-07-12"
    assert candidate.positions[0].symbol == "00700"


def test_stage_pdf_rejects_older_period_without_mutating_newer_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.statement_import as statement_import

    parser = FakePhillipsParser("2026-07-12")
    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", lambda: parser
    )
    data_dir = tmp_path / "data"
    service = statement_import.StatementImportService(
        data_dir=data_dir,
        eastmoney_password="secret",
    )
    accepted = service.stage_pdf("phillips", PDF_BYTES)
    parser.detected_date = "2026-07-11"

    with pytest.raises(ValueError, match="早于当前结单"):
        service.stage_pdf("phillips", b"%PDF-1.7\nolder statement")

    assert statement_import.load_staged_statement_candidate(
        data_dir, "phillips"
    )[1] == accepted["statement_generation"]
    assert len(list((data_dir / "account_statements/generations/phillips").iterdir())) == 1


def test_same_period_correction_is_a_new_generation_and_latest_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.statement_import as statement_import

    parser = FakePhillipsParser()
    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", lambda: parser
    )
    service = statement_import.StatementImportService(
        data_dir=tmp_path / "data",
        eastmoney_password="secret",
    )
    first = service.stage_pdf("phillips", PDF_BYTES)
    parser.quantity = Decimal("2")
    second = service.stage_pdf("phillips", b"%PDF-1.7\ncorrected")

    candidate, generation = statement_import.load_staged_statement_candidate(
        tmp_path / "data", "phillips"
    )

    assert first["statement_generation"] != second["statement_generation"]
    assert generation == second["statement_generation"]
    assert candidate.positions[0].quantity == Decimal("2")


@pytest.mark.parametrize(
    ("broker", "body", "message"),
    [
        ("phillips", b"not a pdf", "有效的 PDF"),
        ("unknown", PDF_BYTES, "不支持的券商"),
    ],
)
def test_stage_pdf_rejects_malformed_input_without_artifacts(
    tmp_path: Path,
    broker: str,
    body: bytes,
    message: str,
) -> None:
    from open_trader.statement_import import StatementImportService

    service = StatementImportService(
        data_dir=tmp_path / "data",
        eastmoney_password="secret",
    )

    with pytest.raises(ValueError, match=message):
        service.stage_pdf(broker, body)

    assert not (tmp_path / "data/account_statements").exists()


def test_stage_pdf_requires_eastmoney_password_and_uses_month_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.statement_import as statement_import

    FakeEastmoneyParser.passwords.clear()
    monkeypatch.setattr(
        statement_import, "EastmoneyStatementParser", FakeEastmoneyParser
    )
    data_dir = tmp_path / "data"
    with pytest.raises(ValueError, match="未配置"):
        statement_import.StatementImportService(
            data_dir=data_dir,
            eastmoney_password="",
        ).stage_pdf("eastmoney", PDF_BYTES)

    result = statement_import.StatementImportService(
        data_dir=data_dir,
        eastmoney_password="local-secret",
    ).stage_pdf("eastmoney", PDF_BYTES)

    assert result["statement_period"] == "2026-07"
    assert FakeEastmoneyParser.passwords == ["local-secret"]


def test_worker_validation_rejects_tampered_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.statement_import as statement_import

    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", FakePhillipsParser
    )
    data_dir = tmp_path / "data"
    staged = statement_import.StatementImportService(
        data_dir=data_dir,
        eastmoney_password="secret",
    ).stage_pdf("phillips", PDF_BYTES)
    generation = staged["statement_generation"].removeprefix("sha256:")
    pdf = (
        data_dir
        / "account_statements/generations/phillips"
        / generation
        / "statement.pdf"
    )
    pdf.write_bytes(b"%PDF-1.7\ntampered")

    with pytest.raises(ValueError, match="invalid statement generation"):
        statement_import.load_staged_statement_candidate(data_dir, "phillips")

    with pytest.raises(ValueError, match="invalid statement generation"):
        statement_import.StatementImportService(
            data_dir=data_dir,
            eastmoney_password="secret",
        ).stage_pdf("phillips", PDF_BYTES)


@pytest.mark.parametrize(
    "relative_path",
    [
        "trade_facts.json",
        "candidate/runs/2026-07/extracted_positions.csv",
    ],
)
def test_worker_validation_rejects_tampered_derived_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    import open_trader.statement_import as statement_import

    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", FakePhillipsParser
    )
    data_dir = tmp_path / "data"
    staged = statement_import.StatementImportService(
        data_dir=data_dir,
        eastmoney_password="secret",
    ).stage_pdf("phillips", PDF_BYTES)
    root = (
        data_dir
        / "account_statements/generations/phillips"
        / staged["statement_generation"].removeprefix("sha256:")
    )
    artifact = root / relative_path
    artifact.write_bytes(artifact.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="invalid statement generation"):
        statement_import.load_staged_statement_candidate(data_dir, "phillips")


def test_statement_generation_binds_derived_artifact_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.statement_import as statement_import

    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", FakePhillipsParser
    )
    data_dir = tmp_path / "data"
    staged = statement_import.StatementImportService(
        data_dir=data_dir,
        eastmoney_password="secret",
    ).stage_pdf("phillips", PDF_BYTES)
    root = (
        data_dir
        / "account_statements/generations/phillips"
        / staged["statement_generation"].removeprefix("sha256:")
    )
    positions = root / "candidate/runs/2026-07/extracted_positions.csv"
    positions.write_bytes(positions.read_bytes() + b"\n")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidate_sha256"] = statement_import._directory_sha256(
        root / "candidate"
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid statement generation"):
        statement_import.load_staged_statement_candidate(data_dir, "phillips")


def test_stage_pdf_normalizes_unexpected_parser_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.statement_import as statement_import

    class BrokenParser:
        def statement_date(self, _path: Path) -> str:
            raise RuntimeError("pdf internals")

    monkeypatch.setattr(statement_import, "PhillipsStatementParser", BrokenParser)

    with pytest.raises(ValueError, match="辉立结单无法解析"):
        statement_import.StatementImportService(
            data_dir=tmp_path / "data",
            eastmoney_password="secret",
        ).stage_pdf("phillips", PDF_BYTES)


def test_same_day_trade_fact_cutoff_covers_market_close_sentinel() -> None:
    from open_trader.statement_import import _statement_cutoff

    staged_at = datetime.fromisoformat("2026-08-04T13:00:00+08:00")

    assert _statement_cutoff(
        "2026-08-04",
        "phillips",
        staged_at,
        [{"filled_at": "2026-08-04T16:00:00+08:00"}],
    ) == (
        "2026-08-04T16:00:00+08:00"
    )

    with pytest.raises(ValueError, match="晚于结单日期"):
        _statement_cutoff(
            "2026-08-04",
            "phillips",
            staged_at,
            [{"filled_at": "2026-08-05T16:00:00+08:00"}],
        )
