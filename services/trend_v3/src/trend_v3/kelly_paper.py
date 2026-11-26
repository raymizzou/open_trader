"""Audit official Trend V3 paper execution and import verified private receipts."""

import argparse
import json
import sqlite3
from contextlib import suppress
from pathlib import Path

from .kelly_ledger import KellyLedger
from .kelly_paper_receipts import DemoLedgerError, require_formal_ledger, sync_receipts
from .kelly_paper_source import execution_snapshot, frozen_report, report_catalog
from .paper_authority import read_json


def execution_counts(snapshot):
    accepted = [
        row
        for row in snapshot.attempts
        if row["order_id"] and row["state"] in {"active", "filled", "cancelled"}
    ]
    return {
        "plans": len(snapshot.plans),
        "approved_lines": sum(
            row["quantity"] > 0 for row in snapshot.approvals.values()
        ),
        "attempts": len(snapshot.attempts),
        "accepted": len(accepted),
        "accepted_without_fills": sum(row["filled"] == 0 for row in accepted),
        "attempts_reporting_fills": sum(
            row["state"] != "unknown" and (row["filled"] or 0) > 0
            for row in snapshot.attempts
        ),
        "unknown_attempts": sum(row["state"] == "unknown" for row in snapshot.attempts),
    }


def ledger_counts(ledger, report):
    with ledger.read() as db:
        require_formal_ledger(db)
        rounds, open_rounds, closed_pending, samples = db.execute(
            """
            SELECT COUNT(*), COUNT(*) FILTER (WHERE closed_at IS NULL),
                   COUNT(*) FILTER (WHERE closed_at IS NOT NULL AND net_pnl IS NULL),
                   COUNT(*) FILTER (WHERE closed_at IS NOT NULL AND net_pnl IS NOT NULL)
            FROM rounds WHERE environment = 'SIMULATE' AND report_ref = ?
            """,
            (report.ref,),
        ).fetchone()
    return {
        "rounds": rounds,
        "open_rounds": open_rounds,
        "closed_pending_costs": closed_pending,
        "confirmed_samples": samples,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "inspect", "sync", "catalog"))
    parser.add_argument("--execution-db", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument(
        "--root", type=Path, required=True, help="official report data root"
    )
    parser.add_argument("--date", required=True, help="Taipei report date")
    parser.add_argument(
        "--revision", required=True, help="exact frozen report revision"
    )
    parser.add_argument("--release", required=True, help="report source commit")
    parser.add_argument(
        "--receipts", type=Path, help="private verified broker receipt package"
    )
    args = parser.parse_args(argv)
    if args.receipts and args.command != "sync":
        parser.error("--receipts is supported by sync")
    if args.command != "catalog" and (args.execution_db is None or args.ledger is None):
        parser.error("init, inspect and sync require --execution-db and --ledger")
    try:
        report = frozen_report(args.root, args.date, args.revision, args.release)
        if args.command == "catalog":
            print(
                json.dumps(report_catalog(report), ensure_ascii=False, sort_keys=True)
            )
            return
        if args.ledger.resolve() == args.execution_db.resolve():
            raise ValueError("execution and Kelly databases must be separate")
        snapshot = execution_snapshot(args.execution_db, report)
        ledger = KellyLedger(args.ledger)
        if args.command == "init" and not ledger.path.exists():
            with suppress(FileExistsError):
                ledger = KellyLedger.create(args.ledger)
        pending = 0
        if args.receipts:
            path = args.receipts.absolute()
            pending = sync_receipts(
                ledger, report, snapshot, read_json(path), path.parent
            )
        result = {
            "receipt_rounds_pending_deal_details": pending,
            "environment": "SIMULATE",
            "report_date": args.date,
            "revision": args.revision,
            "execution": execution_counts(snapshot),
            "kelly": ledger_counts(ledger, report),
        }
    except DemoLedgerError as exc:
        parser.exit(1, str(exc) + "\n")
    except (
        ArithmeticError,
        AttributeError,
        LookupError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        parser.exit(
            1, "Kelly paper audit failed. Check private paths and report binding.\n"
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
