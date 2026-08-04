from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pytest

from open_trader.account_sync_state import (
    empty_account_sync_state,
    write_json_atomic,
)
from open_trader.models import (
    AssetClass,
    CashBalance,
    Market,
    Position,
    StatementTrade,
)
from open_trader.parsers.base import ParseResult
from open_trader.statement_import import StatementImportService


class StatementParser:
    broker = "phillips"
    parser_version = "test-1"

    def statement_date(self, _path: Path) -> str:
        return "2026-07-12"

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
                    quantity=Decimal("10"),
                    cost_price=Decimal("10"),
                    last_price=Decimal("12"),
                    market_value=Decimal("120"),
                    cost_value=Decimal("100"),
                    unrealized_pnl=Decimal("20"),
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
                    quantity=Decimal("10"),
                    price=Decimal("10"),
                    fee=Decimal("1"),
                    costs_complete=True,
                    traded_at="2026-07-10T16:00:00+08:00",
                    reference="buy-1",
                    execution_granularity="statement_trade_date",
                    statement_sequence=1,
                )
            ],
        )


def test_trend_consumes_only_accepted_statement_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.statement_import as statement_import
    import open_trader.trend_statement_consumer as consumer
    from open_trader.account_snapshot import SnapshotResult
    from open_trader.account_sync_state import load_account_sync_state
    from open_trader.trend_statement_consumer import (
        consume_accepted_statement_facts,
    )

    monkeypatch.setattr(
        statement_import, "PhillipsStatementParser", StatementParser
    )
    monkeypatch.setattr(
        consumer,
        "load_account_snapshot",
        lambda data_dir, **_kwargs: SnapshotResult(
            200,
            {
                "accepted_statement_generation": load_account_sync_state(
                    data_dir / "latest/account_sync_state.json"
                )["accepted_statement_generation"],
                "account_generation": "sha256:" + "c" * 64,
            },
            None,
        ),
    )
    data_dir = tmp_path / "data"
    staged = StatementImportService(
        data_dir=data_dir,
        eastmoney_password="secret",
    ).stage_pdf("phillips", b"%PDF-1.7\nstatement")
    state = empty_account_sync_state()
    state["generation"] = "account-generation-before-promotion"
    write_json_atomic(data_dir / "latest/account_sync_state.json", state)

    waiting = consume_accepted_statement_facts(
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        broker="phillips",
        generated_at="2026-08-04T12:00:00+08:00",
    )
    assert waiting["status"] == "waiting_for_promotion"
    assert not (data_dir / "latest/trend_api_stats.json").exists()

    state["generation"] = "account-generation-after-promotion"
    state["accepted_statement_generation"]["phillips"] = staged[
        "statement_generation"
    ]
    write_json_atomic(data_dir / "latest/account_sync_state.json", state)
    consumed = consume_accepted_statement_facts(
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        broker="phillips",
        generated_at="2026-08-04T12:00:00+08:00",
    )
    stats_before = (data_dir / "latest/trend_api_stats.json").read_bytes()
    repeated = consume_accepted_statement_facts(
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        broker="phillips",
        generated_at="2026-08-04T12:01:00+08:00",
    )

    assert consumed["status"] == "consumed"
    assert repeated == {**consumed, "status": "already_consumed"}
    assert (data_dir / "latest/trend_api_stats.json").read_bytes() == stats_before
    status = json.loads(
        (data_dir / "trend_statement_consumption/phillips.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["statement_generation"] == staged["statement_generation"]
    assert status["account_generation"] == "sha256:" + "c" * 64


def test_trend_waits_for_same_day_market_close_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.trend_statement_consumer as consumer
    from open_trader.account_snapshot import SnapshotResult

    generation = "sha256:" + "a" * 64
    monkeypatch.setattr(
        consumer,
        "load_account_snapshot",
        lambda *_args, **_kwargs: SnapshotResult(
            200,
            {
                "accepted_statement_generation": {
                    "phillips": generation,
                    "eastmoney": "",
                },
                "account_generation": "sha256:" + "c" * 64,
            },
            None,
        ),
    )
    monkeypatch.setattr(
        consumer,
        "load_statement_trade_facts",
        lambda *_args: (
            {
                "statement_period": "2026-08-04",
                "trade_facts_cutoff_at": "2026-08-04T16:00:00+08:00",
            },
            [],
        ),
    )

    result = consumer.consume_accepted_statement_facts(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        broker="phillips",
        generated_at="2026-08-04T13:00:00+08:00",
    )

    assert result["status"] == "waiting_for_statement_cutoff"
    assert result["retry_after"] == "2026-08-04T16:00:00+08:00"
    assert not (tmp_path / "data/latest/trend_api_stats.json").exists()
