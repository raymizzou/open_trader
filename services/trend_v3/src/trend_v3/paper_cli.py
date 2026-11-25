"""Plan paper entries in the original scheduled Trend V3 report tab."""

import argparse
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep as sleep_open

from .paper_authority import (
    delivery_card,
    latest_report,
    read_json,
    release_pin,
    require_private_dir,
    scheduled_scope,
    transcript_replies,
    upload_receipt,
)
from .paper_entry import PaperEntry, encode
from .paper_evidence import TAIPEI, instant
from .paper_facts import produce
from .paper_futu import BrokerUnavailable, FutuPaperBroker


def utc_now():
    return datetime.now(UTC)


def settings(home):
    path = home / ".config/trend-v3/paper-entry/settings.json"
    if not path.exists():
        return {
            "host": os.getenv("OPEN_TRADER_FUTU_HOST", "127.0.0.1"),
            "port": int(os.getenv("OPEN_TRADER_FUTU_PORT", "11111")),
        }
    config = read_json(path)
    if config.get("environment") != "SIMULATE":
        raise ValueError("paper broker configuration must be explicitly SIMULATE")
    return config


def emit(value):
    print(encode(value), flush=True)


def public_plan(plan):
    public_fields = (
        "item",
        "code",
        "direction",
        "signal",
        "quantity",
        "reference_price",
        "notional",
        "currency",
        "order_type",
        "opens_at",
        "timezone",
        "session_date",
    )
    return {
        "id": plan["id"],
        "date": plan["date"],
        "report_version": plan["report_version"],
        "lines": [
            {field: row[field] for field in public_fields} for row in plan["lines"]
        ],
        "skipped": plan["skipped"],
        "execution": "awaiting_scheduled_tab_approval",
        "delivery": delivery_card(plan),
        "approve_all": "确认全部",
        "withdraw_all": f"模拟撤回 {plan['id']} 全部",
    }


class OfflineBroker:
    """Read the same frozen artifacts without connecting to a broker."""

    environment = "SIMULATE"

    def planning_quote(self, *_args, **_kwargs):
        raise BrokerUnavailable("offline quote unavailable")


def plan_report(service, artifacts, evidence_dir, now, *, clock=None):
    report, selection, info = artifacts
    report, facts = produce(report, selection, info, evidence_dir, service.broker, now)
    return public_plan(service.plan(report, facts, now=clock() if clock else now))


def rehearse(args):
    root = args.root.resolve(strict=True)
    artifacts = latest_report(
        root, args.date, args.release, frozen_revision=args.revision
    )
    now = instant(artifacts[2].get("cutoff"))
    if now is None:
        raise ValueError("frozen report cutoff is unavailable")
    scope = {"owner": "offline", "id": "offline", "date": args.date, "kind": "offline"}
    with TemporaryDirectory(prefix="paper-rehearsal-") as folder:
        service = PaperEntry(Path(folder) / "state.sqlite3", OfflineBroker(), scope)
        plan = plan_report(service, artifacts, root / "reports/_evidence", now)
    emit(plan | {"offline": True, "broker_io": 0, "order_writes": 0})


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", help="original scheduled Codex session UUID")
    parser.add_argument("--report-date", help="original scheduled Taipei report date")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "plan", help="plan from the scheduled tab's latest daily report"
    )
    commands.add_parser("status", help="produce the current redacted report supplement")
    approve = commands.add_parser(
        "approve", help="record an explicit reply from the original scheduled tab"
    )
    approve.add_argument("plan")
    submit = commands.add_parser(
        "submit", help="wait and submit approved market entries"
    )
    submit.add_argument("plan")
    submit.add_argument("--retry-item")
    reconcile = commands.add_parser("reconcile", help="read existing broker receipts")
    reconcile.add_argument("plan")
    offline = commands.add_parser(
        "rehearse", help="read frozen ordinary artifacts offline"
    )
    offline.add_argument("root", type=Path)
    offline.add_argument("date")
    offline.add_argument("revision")
    offline.add_argument("--release", required=True)
    return parser, parser.parse_args(argv)


def main(argv=None):
    parser, args = parse_args(argv)
    try:
        if args.command == "rehearse":
            rehearse(args)
        else:
            return run_daily(args)
    except (
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        # Only messages defined on these input validation paths are public.
        if isinstance(error, ValueError) and type(error) is ValueError:
            reason = (
                str(error)
                if str(error) in PUBLIC_ERRORS
                else "daily evidence unavailable"
            )
        else:
            reason = "daily evidence or paper broker unavailable"
        parser.exit(2, f"Paper planning stopped: {reason}.\n")
    return 0


PUBLIC_ERRORS = {
    "original scheduled Codex session is unavailable",
    "original scheduled report date is unavailable",
    "scheduled launch receipt does not match this day and session",
    "native scheduled tab or original launch prompt is unverified",
    "paper code is outside the pinned daily skill release",
    "matching host report upload receipt is unavailable",
    "latest daily report pointer does not match",
    "daily report revision does not match its verification",
    "daily report artifact digest does not match",
    "daily report artifact date does not match",
    "frozen report cutoff is unavailable",
}


def run_daily(args):
    home = Path.home().resolve(strict=True)
    root = home / ".local/share/trend-animals"
    now = utc_now()
    today = now.astimezone(TAIPEI).date()
    allowed = {today.isoformat()}
    if args.command in {"reconcile", "status"} or (
        args.command == "submit" and args.retry_item
    ):
        allowed.add((today - timedelta(days=1)).isoformat())
    report_day = args.report_date or today.isoformat()
    if report_day not in allowed:
        raise ValueError("original scheduled report date is unavailable")
    scope = scheduled_scope(home, root, report_day, args.session)
    pin = release_pin(home)

    def check_plan(plan):
        try:
            info = latest_report(root, report_day, pin)[2]
            upload_receipt(home, scope, info)
            digest = sha256(
                (info["projection_sha256"] + info["selection_sha256"]).encode()
            )
            return (
                plan["report_version"] == info["revision"]
                and plan["report_hash"] == digest.hexdigest()
            )
        except (KeyError, OSError, TypeError, ValueError):
            return False

    artifacts = None
    if args.command == "plan":
        artifacts = latest_report(root, report_day, pin)
        upload_receipt(home, scope, artifacts[2])
    state = root / "reports/_evidence/.paper-entry"
    state.mkdir(mode=0o700, exist_ok=True)
    require_private_dir(state)
    broker = FutuPaperBroker(settings(home), clock=utc_now)
    authorized = None

    def record_reply(service, plan, reply):
        nonlocal authorized
        authorized = reply
        return service.record_reply(plan["id"], reply, now=utc_now())

    try:
        service = PaperEntry(
            state / "state.sqlite3",
            broker,
            scope,
            clock=utc_now,
            verify_reply=lambda reply: reply is authorized,
            check_plan=check_plan,
        )
        if args.command == "plan":
            emit(
                plan_report(
                    service, artifacts, root / "reports/_evidence", now, clock=utc_now
                )
            )
        elif args.command == "status":
            emit(service.status(now=now))
        elif args.command == "reconcile":
            emit(public_results(args.plan, service.reconcile(args.plan, now=now)))
        else:
            plan, created = service.current_plan(args.plan)
            if plan is None or not check_plan(plan):
                emit({"plan": args.plan, "reason": "report_revised_or_unverified"})
                return 2

            def replies():
                return transcript_replies(home, root, scope, plan, created, utc_now())

            def record(reply):
                return record_reply(service, plan, reply)

            return run_action(args, service, plan, replies, record)
    finally:
        broker.close()
    return 0


def public_results(plan_id, outcomes):
    results = []
    for row in outcomes:
        result = PaperEntry.public_outcome(row) | {"item": row["item"]}
        if (
            row["state"] in {"rejected", "cancelled"}
            and row.get("reconciled")
            and row.get("remaining")
        ):
            result["retry_reply"] = (
                f"模拟重试 {plan_id} {row['item']} {row['remaining']} {row['attempt']}"
            )
        results.append(result)
    return {"plan": plan_id, "results": results}


def run_action(args, service, plan, replies, record):
    if args.command == "approve" or args.retry_item:
        records = replies()
        latest = records[-1] if records else None
        if args.command == "approve":
            decision = (
                record(latest) if latest and latest["decision"] != "retry" else None
            )
            if decision is not None:
                emit({"plan": plan["id"], **decision})
                return 0
            emit(
                {
                    "plan": plan["id"],
                    "recorded": False,
                    "reason": "approval_clarification_required",
                }
            )
            return 2
        if (
            not latest
            or latest["decision"] != "retry"
            or [item["item"] for item in latest["items"]] != [args.retry_item]
            or not record(latest)
        ):
            emit({"plan": plan["id"], "reason": "retry_not_authorized"})
            return 2
        emit(
            public_results(
                plan["id"],
                service.submit(
                    plan["id"],
                    now=utc_now(),
                    retry=True,
                    item=args.retry_item,
                ),
            )
        )
        return 0
    wait_for_open(service, plan, replies, record)
    return 0


def wait_for_open(service, plan, replies, record):
    plan_id = plan["id"]
    started = utc_now()
    for opening in service.openings(plan_id):
        if opening <= started:
            continue
        while (observed := utc_now()) < opening:
            if not service.openings(plan_id):
                break
            sleep_open(min(30, (opening - observed).total_seconds()))
        try:
            for reply in replies():
                if reply and reply["decision"] == "deny":
                    decision = record(reply)
                    if decision is not None and decision["recorded"]:
                        emit({"plan": plan_id, **decision})
        except (OSError, ValueError):
            emit(
                public_results(
                    plan_id,
                    [
                        service.outcome(
                            line, "skipped", reason="approval_evidence_unavailable"
                        )
                        for line in plan["lines"]
                        if instant(line["opens_at"]) == opening
                    ],
                )
            )
            continue
        emit(
            public_results(
                plan_id, service.submit(plan_id, now=observed, opening=opening)
            )
        )
    emit({"plan": plan_id, "status": service.status(now=utc_now())})


if __name__ == "__main__":
    raise SystemExit(main())
