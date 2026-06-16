from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import open_trader.cli as cli
from open_trader.futu_watch import QuoteSnapshot


class FakeQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        self.closed = False

    def get_snapshots(self, futu_symbols: list[str]) -> dict[str, QuoteSnapshot]:
        assert futu_symbols == ["US.MSFT"]
        return {"US.MSFT": QuoteSnapshot(futu_symbol="US.MSFT", last_price=Decimal("399"))}

    def close(self) -> None:
        self.closed = True


def write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "run_date,symbol,market,source_status,fallback_reason,"
            "fallback_from_date,rating,entry_zone_low,entry_zone_high,add_price,"
            "stop_loss,target_1,target_2,max_weight,catalyst,time_horizon,"
            "plan_text,status,error\n"
            "2026-06-17,MSFT,US,ok,,,Overweight,380,400,,350,410,430,"
            "3%,fake,1 week,fake,active,\n"
        ),
        encoding="utf-8",
    )


def write_actions(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "run_date,symbol,market,futu_symbol,action,priority,last_price,"
            "trigger_status,suggested_quantity,suggested_notional,"
            "notional_currency,current_quantity,current_weight,target_max_weight,"
            "cash_available,limit_price,stop_price,reason,source_plan,status,error\n"
            "2026-06-17,MSFT,US,US.MSFT,BUY,high,399,entry_zone,3,1197,USD,"
            "2,1%,3%,10000,399,350,entered entry zone,plan,ready,\n"
        ),
        encoding="utf-8",
    )


def test_watch_actions_once_sends_trigger_and_records_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = tmp_path / "data/runs/2026-06-17/trading_plan.csv"
    actions = tmp_path / "data/runs/2026-06-17/trade_actions.csv"
    write_plan(plan)
    write_actions(actions)
    sent = []

    monkeypatch.setattr(cli, "FutuQuoteClient", FakeQuoteClient)
    monkeypatch.setattr(
        cli,
        "build_notifier_from_values",
        lambda values, dry_run=False: type(
            "N",
            (),
            {"notify": lambda self, title, message: sent.append((title, message))},
        )(),
    )

    result = cli.main(
        [
            "watch-actions",
            "--date",
            "2026-06-17",
            "--plan",
            str(plan),
            "--actions",
            str(actions),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--once",
        ]
    )

    assert result == 0
    assert len(sent) == 1
    assert "US.MSFT BUY triggered" in sent[0][1]
    assert (tmp_path / "data/runs/2026-06-17/notification_state.json").exists()


def test_watch_actions_does_not_send_duplicate_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = tmp_path / "data/runs/2026-06-17/trading_plan.csv"
    actions = tmp_path / "data/runs/2026-06-17/trade_actions.csv"
    write_plan(plan)
    write_actions(actions)
    sent = []

    monkeypatch.setattr(cli, "FutuQuoteClient", FakeQuoteClient)
    monkeypatch.setattr(
        cli,
        "build_notifier_from_values",
        lambda values, dry_run=False: type(
            "N",
            (),
            {"notify": lambda self, title, message: sent.append((title, message))},
        )(),
    )

    args = [
        "watch-actions",
        "--date",
        "2026-06-17",
        "--plan",
        str(plan),
        "--actions",
        str(actions),
        "--data-dir",
        str(tmp_path / "data"),
        "--reports-dir",
        str(tmp_path / "reports"),
        "--once",
    ]
    assert cli.main(args) == 0
    assert cli.main(args) == 0

    assert len(sent) == 1
