from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from trend_v3.kelly_demo import PARTIAL_IDENTITY
from trend_v3.kelly_ledger import Fill, KellyLedger, RoundIdentity


def _demo(command: str, path: Path, *, check: bool = True):
    result = subprocess.run(
        [sys.executable, "-m", "trend_v3.kelly_demo", command, "--db", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _database_path(tmp_path: Path) -> Path:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    return private_dir / "demo.sqlite"


def test_seed_demo(tmp_path):
    path = _database_path(tmp_path)
    result = _demo("seed", path)
    assert json.loads(result.stdout)["fabricated"] is True

    assert path.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
        ).fetchall() == [("rounds",), ("trade_events",)]
        assert [row[1] for row in db.execute("PRAGMA table_info(rounds)")] == [
            "round_id",
            "account_key",
            "environment",
            "instrument_key",
            "direction",
            "source_group",
            "report_ref",
            "opened_at",
            "closed_at",
            "currency",
            "net_pnl",
            "return_base",
        ]
        assert [row[1] for row in db.execute("PRAGMA table_info(trade_events)")] == [
            "event_key",
            "round_id",
            "occurred_at",
            "event_type",
            "quantity",
            "notional",
            "cash_adjustment",
            "receipt_ref",
        ]
        assert [
            row[2:5] for row in db.execute("PRAGMA foreign_key_list(trade_events)")
        ] == [
            ("rounds", "round_id", "round_id"),
        ]
        joined = db.execute(
            """
            SELECT account_key, environment, instrument_key, direction, source_group,
                   report_ref, currency, round_id, closed_at, event_key, event_type,
                   CAST(quantity AS REAL), CAST(notional AS REAL)
            FROM rounds JOIN trade_events USING (round_id)
            WHERE event_key IN ('demo/partial/open', 'demo/partial/reduce')
            ORDER BY occurred_at
            """
        ).fetchall()
    assert len(joined) == 2
    account, env, instrument, direction, group, report, currency, round_id, closed = (
        joined[0][:9]
    )
    assert joined[1][:9] == joined[0][:9]
    assert (account, env, direction, currency, closed) == (
        "fabricated-simulate",
        "SIMULATE",
        "long",
        "USD",
        None,
    )
    assert [row[9:] for row in joined] == [
        ("demo/partial/open", "buy", 100, 1000),
        ("demo/partial/reduce", "sell", 25, 275),
    ]

    mismatched = RoundIdentity(
        account, env, instrument, direction, group, report + "-x", currency
    )
    with pytest.raises(ValueError, match="identity"):
        KellyLedger(path).record_fill(
            mismatched,
            Fill(
                "demo/changed-identity",
                "2026-11-06T00:00:00Z",
                "sell",
                "1",
                "11",
                "mock",
            ),
        )
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT report_ref FROM rounds WHERE round_id = ?",
            (round_id,),
        ).fetchone() == (report,)
        assert db.execute(
            "SELECT COUNT(*) FROM trade_events WHERE event_key = ?",
            ("demo/changed-identity",),
        ).fetchone() == (0,)


def test_round_from_fills(tmp_path):
    path = _database_path(tmp_path)
    ledger = KellyLedger.create(path)
    identity = RoundIdentity(
        "fabricated-lifecycle",
        "SIMULATE",
        "mock-stock",
        "long",
        "mock-equities",
        "mock-2026-11-v1",
        "USD",
    )
    first = Fill("open", "2026-11-01T01:00:00Z", "buy", "100", "1000", "mock-open")
    add = Fill("add", "2026-11-02T01:00:00Z", "buy", "50", "600", "mock-add")
    partial = Fill(
        "partial", "2026-11-03T01:00:00Z", "sell", "50", "650", "mock-partial"
    )
    exit_fill = Fill("exit", "2026-11-04T01:00:00Z", "sell", "100", "1100", "mock-exit")
    round_id = ledger.record_fill(identity, first)
    assert ledger.record_fill(identity, add) == round_id
    assert ledger.record_fill(identity, partial) == round_id

    def round_state(id_):
        with closing(sqlite3.connect(path)) as db:
            return db.execute(
                """
                SELECT closed_at, CAST(net_pnl AS REAL), CAST(return_base AS REAL)
                FROM rounds WHERE round_id = ?
                """,
                (id_,),
            ).fetchone()

    assert round_state(round_id) == (None, None, None)
    assert ledger.record_fill(identity, exit_fill) == round_id
    closed_at = "2026-11-04T01:00:00.000000+00:00"
    assert round_state(round_id) == (closed_at, None, 1600)

    fee = {
        "event_key": "cost",
        "occurred_at": "2026-11-05T01:00:00Z",
        "cash_adjustment": "-5",
        "receipt_ref": "mock-cost",
    }
    settlement = {
        "event_key": "settle",
        "occurred_at": "2026-11-05T01:00:00Z",
        "receipt_ref": "mock-cost-complete",
    }
    ledger.record_adjustment(round_id, **fee)
    assert round_state(round_id) == (closed_at, None, 1600)
    ledger.settle(round_id, **settlement)
    assert round_state(round_id) == (closed_at, 145, 1600)
    with closing(sqlite3.connect(path)) as db:
        net, base = db.execute(
            "SELECT net_pnl, return_base FROM rounds WHERE round_id = ?",
            (round_id,),
        ).fetchone()
    assert Decimal(net) / Decimal(base) == Decimal("0.090625")

    reopened = ledger.record_fill(
        identity,
        Fill("reopen", "2026-11-06T01:00:00Z", "buy", "1", "5", "mock-reopen"),
    )
    assert reopened != round_id
    ledger.record_fill(
        identity,
        Fill("exit-2", "2026-11-07T01:00:00Z", "sell", "1", "5", "mock-exit-2"),
    )
    zero_close = "2026-11-07T01:00:00.000000+00:00"
    assert round_state(reopened) == (zero_close, None, 5)
    ledger.settle(
        reopened,
        event_key="settle-2",
        occurred_at="2026-11-08T01:00:00Z",
        receipt_ref="mock-confirmed-zero-cost",
    )
    assert round_state(reopened) == (zero_close, 0, 5)

    restored = KellyLedger(path)
    assert restored.record_fill(identity, first) == round_id
    assert restored.record_fill(identity, exit_fill) == round_id
    restored.record_adjustment(round_id, **fee)
    restored.settle(round_id, **settlement)
    with pytest.raises(ValueError, match="event key conflict"):
        restored.record_adjustment(round_id, **(fee | {"cash_adjustment": "-6"}))
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM rounds").fetchone() == (2,)
        assert db.execute("SELECT COUNT(*) FROM trade_events").fetchone() == (9,)
    assert round_state(round_id) == (closed_at, 145, 1600)
    assert round_state(reopened) == (zero_close, 0, 5)


def test_demo_slice_raw_sql(tmp_path):
    path = _database_path(tmp_path)
    summary = json.loads(_demo("seed", path).stdout)
    assert summary == json.loads(_demo("inspect", path).stdout)
    assert summary["report_ref"] == "mock-trend-v3-2026-11-v1"
    assert summary["report_period"] == "2026-11"
    assert summary["source_version"] == "mock-classes-v1"
    assert summary["source_groups"] == [
        "mock-equities",
        "mock-rates",
        "mock-digital-assets",
        "mock-empty",
    ]
    assert summary["fabricated"] is True
    assert summary["totals"] == {"rounds": 7, "trade_events": 20}
    assert summary["selected"] == {
        "environment": "SIMULATE",
        "source_group": "mock-equities",
        "rounds": 4,
        "closed": 3,
        "cost_complete": 3,
    }
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            """
            SELECT environment, source_group, report_ref, COUNT(*)
            FROM rounds GROUP BY environment, source_group, report_ref
            ORDER BY environment, source_group, report_ref
            """
        ).fetchall() == [
            ("REAL", "mock-equities", "mock-trend-v3-2026-11-v1", 1),
            ("SIMULATE", "mock-equities", "mock-trend-v3-2026-10-v1", 1),
            ("SIMULATE", "mock-equities", "mock-trend-v3-2026-11-v1", 4),
            ("SIMULATE", "mock-rates", "mock-trend-v3-2026-11-v1", 1),
        ]
        assert db.execute(
            "SELECT DISTINCT environment, account_key FROM rounds ORDER BY environment"
        ).fetchall() == [
            ("REAL", "fabricated-real"),
            ("SIMULATE", "fabricated-simulate"),
        ]
        assert db.execute(
            """
            SELECT direction, net_pnl, return_base
            FROM rounds WHERE source_group = 'mock-rates'
            """
        ).fetchone() == ("short", "100", "1000")
        selected = db.execute(
            """
            SELECT instrument_key, closed_at, net_pnl, return_base
            FROM rounds
            WHERE report_ref = 'mock-trend-v3-2026-11-v1'
              AND environment = 'SIMULATE' AND source_group = 'mock-equities'
            ORDER BY instrument_key
            """
        ).fetchall()
    assert {
        instrument: (Decimal(net) / Decimal(base))
        for instrument, closed_at, net, base in selected
        if closed_at is not None
    } == {
        "mock-loss": Decimal("-0.05"),
        "mock-win": Decimal("0.10"),
        "mock-zero": Decimal("0"),
    }
    assert [row for row in selected if row[1] is None] == [
        ("mock-partial", None, None, None)
    ]


def test_seed_is_idempotent(tmp_path):
    path = _database_path(tmp_path)
    initial = json.loads(_demo("seed", path).stdout)
    assert json.loads(_demo("seed", path).stdout) == initial

    def snapshot():
        with closing(sqlite3.connect(path)) as db:
            return (
                db.execute("SELECT * FROM rounds ORDER BY round_id").fetchall(),
                db.execute("SELECT * FROM trade_events ORDER BY event_key").fetchall(),
            )

    failed_path = path.with_name("failed-seed.sqlite")

    def fail_during_initialization(staged):
        staged.record_fill(
            PARTIAL_IDENTITY,
            Fill("seed/open", "2026-11-04T00:00:00Z", "buy", "1", "10", "mock-open"),
        )
        staged.record_fill(
            PARTIAL_IDENTITY,
            Fill(
                "seed/invalid",
                "2026-11-05T00:00:00Z",
                "sell",
                "2",
                "20",
                "mock-invalid",
            ),
        )

    with pytest.raises(ValueError, match="exceeds open quantity"):
        KellyLedger.create(failed_path, populate=fail_during_initialization)
    assert not failed_path.exists()

    original = snapshot()
    ledger = KellyLedger(path)
    with pytest.raises(ValueError, match="exceeds open quantity"):
        ledger.record_fill(
            PARTIAL_IDENTITY,
            Fill(
                "too-large", "2026-11-06T00:00:00Z", "sell", "76", "836", "mock-invalid"
            ),
        )
    assert snapshot() == original
    (partial_id,) = next((row[0],) for row in original[0] if row[3] == "mock-partial")
    assert (
        ledger.record_fill(
            PARTIAL_IDENTITY,
            Fill(
                "demo/partial/close",
                "2026-11-06T00:00:00Z",
                "sell",
                "75",
                "825",
                "mock-close",
            ),
        )
        == partial_id
    )
    ledger.record_adjustment(
        partial_id,
        event_key="demo/partial/cost",
        occurred_at="2026-11-07T00:00:00Z",
        cash_adjustment="-10",
        receipt_ref="mock-partial-cost",
    )
    ledger.settle(
        partial_id,
        event_key="demo/partial/settled",
        occurred_at="2026-11-07T00:00:00Z",
        receipt_ref="mock-partial-complete-cost",
    )
    updated = snapshot()
    summary = json.loads(_demo("seed", path).stdout)
    assert summary["totals"] == {"rounds": 7, "trade_events": 23}
    assert summary["selected"] == initial["selected"] | {
        "closed": 4,
        "cost_complete": 4,
    }
    assert snapshot() == updated
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            """
            SELECT closed_at, CAST(net_pnl AS INTEGER), CAST(return_base AS INTEGER)
            FROM rounds WHERE round_id = ?
            """,
            (partial_id,),
        ).fetchone() == ("2026-11-06T00:00:00.000000+00:00", 90, 1000)

    other_path = path.with_name("execution.sqlite")
    other_path.touch(mode=0o600)
    with closing(sqlite3.connect(other_path)) as db, db:
        db.execute("CREATE TABLE sentinel (value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('do not change')")
    other_bytes = other_path.read_bytes()
    for command in ("seed", "inspect"):
        refused = _demo(command, other_path, check=False)
        assert refused.returncode != 0
        assert "not a Kelly ledger database" in refused.stderr
    with pytest.raises(ValueError, match="not a Kelly ledger database"):
        KellyLedger(other_path).record_fill(
            PARTIAL_IDENTITY,
            Fill("alien", "2026-11-08T00:00:00Z", "buy", "1", "10", "mock-alien"),
        )
    assert other_path.read_bytes() == other_bytes

    linked = path.with_name("linked.sqlite")
    linked.symlink_to(path)
    refused = _demo("seed", linked, check=False)
    assert refused.returncode != 0
    assert "private regular file" in refused.stderr
    assert linked.is_symlink()
    assert snapshot() == updated
