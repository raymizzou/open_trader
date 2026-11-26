"""Build and inspect a private Kelly ledger from entirely fabricated evidence."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .kelly_ledger import Fill, KellyLedger, RoundIdentity

REPORT_PERIOD = "2026-11"
REPORT_REF = "mock-trend-v3-2026-11-v1"
SOURCE_VERSION = "mock-classes-v1"
SOURCE_GROUPS = ("mock-equities", "mock-rates", "mock-digital-assets", "mock-empty")
SELECTED_ENVIRONMENT = "SIMULATE"
SELECTED_GROUP = "mock-equities"

PARTIAL_IDENTITY = RoundIdentity(
    "fabricated-simulate",
    SELECTED_ENVIRONMENT,
    "mock-partial",
    "long",
    SELECTED_GROUP,
    REPORT_REF,
    "USD",
)


def _complete_example(
    ledger: KellyLedger,
    label: str,
    date: str,
    exit_notional: str,
    *,
    environment: str = SELECTED_ENVIRONMENT,
    group: str = SELECTED_GROUP,
    report_ref: str = REPORT_REF,
    direction: str = "long",
) -> None:
    identity = RoundIdentity(
        f"fabricated-{environment.lower()}",
        environment,
        f"mock-{label}",
        direction,
        group,
        report_ref,
        "USD",
    )
    exit_side = "sell" if direction == "long" else "buy"
    round_id = ledger.record_fill(
        identity,
        Fill(
            f"demo/{label}/open",
            f"{date}T01:00:00Z",
            identity.entry_side,
            "100",
            "1000",
            f"mock-{label}-open",
        ),
    )
    ledger.record_fill(
        identity,
        Fill(
            f"demo/{label}/close",
            f"{date}T02:00:00Z",
            exit_side,
            "100",
            exit_notional,
            f"mock-{label}-close",
        ),
    )
    ledger.settle(
        round_id,
        event_key=f"demo/{label}/settle",
        occurred_at=f"{date}T03:00:00Z",
        receipt_ref=f"mock-{label}-confirmed-zero-cost",
    )


def _populate_demo(ledger: KellyLedger) -> None:
    _complete_example(ledger, "win", "2026-11-01", "1100")
    _complete_example(ledger, "loss", "2026-11-02", "950")
    _complete_example(ledger, "zero", "2026-11-03", "1000")
    ledger.record_fill(
        PARTIAL_IDENTITY,
        Fill(
            "demo/partial/open",
            "2026-11-04T00:00:00Z",
            "buy",
            "100",
            "1000",
            "mock-open",
        ),
    )
    ledger.record_fill(
        PARTIAL_IDENTITY,
        Fill(
            "demo/partial/reduce",
            "2026-11-05T00:00:00Z",
            "sell",
            "25",
            "275",
            "mock-reduce",
        ),
    )
    _complete_example(ledger, "real", "2026-11-06", "1300", environment="REAL")
    _complete_example(
        ledger, "rates", "2026-11-07", "900", group="mock-rates", direction="short"
    )
    _complete_example(
        ledger,
        "past",
        "2026-10-08",
        "800",
        report_ref="mock-trend-v3-2026-10-v1",
    )


def seed_demo(path: Path) -> dict:
    """Initialize a new demo, or read the current ledger without reseeding."""
    try:
        KellyLedger.create(path, populate=_populate_demo)
    except FileExistsError:
        pass
    return demo_snapshot(path)


def demo_snapshot(path: Path) -> dict:
    """Return the frozen mock class list and counts from the current ledger."""
    with KellyLedger(path).read() as db:
        (rounds,) = db.execute("SELECT COUNT(*) FROM rounds").fetchone()
        (events,) = db.execute("SELECT COUNT(*) FROM trade_events").fetchone()
        matched, closed, complete = db.execute(
            """
            SELECT COUNT(*), COUNT(closed_at), COUNT(net_pnl) FROM rounds
            WHERE environment = ? AND source_group = ? AND report_ref = ?
            """,
            (SELECTED_ENVIRONMENT, SELECTED_GROUP, REPORT_REF),
        ).fetchone()
    return {
        "fabricated": True,
        "report_ref": REPORT_REF,
        "report_period": REPORT_PERIOD,
        "source_version": SOURCE_VERSION,
        "source_groups": list(SOURCE_GROUPS),
        "totals": {"rounds": rounds, "trade_events": events},
        "selected": {
            "environment": SELECTED_ENVIRONMENT,
            "source_group": SELECTED_GROUP,
            "rounds": matched,
            "closed": closed,
            "cost_complete": complete,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["seed", "inspect"])
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    action = seed_demo if args.command == "seed" else demo_snapshot
    try:
        result = action(args.db)
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
