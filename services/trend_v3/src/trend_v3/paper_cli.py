"""Plan paper entries in the original scheduled Trend V3 report tab."""

import argparse
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep as sleep_open

from .paper_authority import (
    delivery_card,
    handoff_identity,
    latest_report,
    message_text,
    plan_display,
    plan_was_delivered,
    read_json,
    release_pin,
    require_private_dir,
    scheduled_scope,
    session_records,
    transcript_replies,
    upload_receipt,
)
from .paper_entry import LATE_DISPATCH_SLACK, PaperEntry, encode
from .paper_evidence import TAIPEI, instant
from .paper_facts import late_execution_terms, produce, retained_bridge
from .paper_futu import BrokerUnavailable, FutuPaperBroker
from .paper_report import prepare_report_evidence, refresh_report
from .paper_store import handoff_plan


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
    result = {
        "id": plan["id"],
        "date": plan["date"],
        "report_version": plan["report_version"],
        "lines": [
            {field: row[field] for field in public_fields} for row in plan["lines"]
        ],
        "skipped": [
            {
                field: row[field]
                for field in ("code", "reason", "direction", "seat")
                if field in row
            }
            for row in plan["skipped"]
        ],
    }
    if "manual" in plan:
        result["manual"] = [
            {
                k: row[k]
                for k in (
                    "task_id",
                    "code",
                    "direction",
                    "currency",
                    "state",
                    "missing",
                )
            }
            for row in plan["manual"]
        ]
    result["display"] = plan_display(plan)
    if "observations" in plan:
        result["observations"] = plan["observations"]
    if not plan["lines"]:
        reasons = {row["reason"] for row in plan["skipped"]}
        if reasons <= {"verified_no_signal"}:
            execution = "verified_no_candidates"
        elif any(
            reason.endswith(("_unverified", "_unavailable", "_conflict"))
            for reason in reasons
        ):
            execution = "evidence_incomplete"
        else:
            execution = "no_actionable_candidates"
        return result | {"execution": execution}
    return result | {
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


def plan_report(
    service, artifacts, evidence_dir, now, *, clock=None, was_delivered=None
):
    report, selection, info = artifacts
    report, facts = produce(
        report,
        selection,
        info,
        evidence_dir,
        service.broker,
        now,
        allow_late=service.session["kind"] == "scheduled",
    )
    return public_plan(
        service.plan(
            report, facts, now=clock() if clock else now, was_delivered=was_delivered
        )
    )


def rehearse(args):
    root = args.root.resolve(strict=True)
    artifacts = latest_report(
        root, args.date, args.release, frozen_revision=args.revision
    )
    _, selection, info = artifacts
    now = instant(info.get("cutoff", selection.get("cutoff")))
    if (
        now is None
        or now.astimezone(TAIPEI).date().isoformat() != args.date
        or ("cutoff" in selection and instant(selection["cutoff"]) != now)
    ):
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
    plan = commands.add_parser(
        "plan", help="plan from the scheduled tab's latest daily report"
    )
    plan.add_argument("--handoff", help="pending trusted host planning notice")
    manual = commands.add_parser(
        "manual-holdings", help="record the original user crypto position snapshot"
    )
    manual.add_argument("snapshot", type=Path)
    commands.add_parser("status", help="produce the current redacted report supplement")
    approve = commands.add_parser(
        "approve", help="record an explicit reply from the original scheduled tab"
    )
    approve.add_argument("plan")
    cycle = commands.add_parser(
        "cycle", help="record the original reply and run approved openings"
    )
    cycle.add_argument("plan")
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
    prepare = commands.add_parser(
        "prepare-report-evidence",
        help="retain selection evidence before report verification",
    )
    prepare.add_argument("root", type=Path)
    prepare.add_argument("date")
    prepare.add_argument("revision")
    prepare.add_argument("--release", required=True)
    return parser, parser.parse_args(argv)


def main(argv=None):
    parser, args = parse_args(argv)
    try:
        if args.command == "rehearse":
            rehearse(args)
        elif args.command == "prepare-report-evidence":
            emit(
                prepare_report_evidence(
                    args.root, args.date, args.revision, args.release
                )
            )
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
    "report date must match the current scheduled report",
    "scheduled launch receipt does not match this day and session",
    "native scheduled tab or original launch prompt is unverified",
    "paper code is outside the pinned daily skill release",
    "matching host report upload receipt is unavailable",
    "matching host planning notice is unavailable",
    "latest daily report pointer does not match",
    "daily report revision does not match its verification",
    "daily report artifact digest does not match",
    "daily report artifact date does not match",
    "daily selection evidence already exists",
    "unsupported daily selection evidence schema",
    "frozen report cutoff is unavailable",
    "daily observation evidence unverified",
}


def run_daily(args):
    home = Path.home().resolve(strict=True)
    root = home / ".local/share/trend-animals"
    now = utc_now()
    today = now.astimezone(TAIPEI).date()
    allowed = {today.isoformat()}
    if args.command != "manual-holdings":
        allowed.add((today - timedelta(days=1)).isoformat())
    report_day = args.report_date or today.isoformat()
    if report_day not in allowed:
        raise ValueError("original scheduled report date is unavailable")
    scope = scheduled_scope(home, root, report_day, args.session)
    pin = release_pin(home)

    def check_plan(plan):
        try:
            report, selection, info = latest_report(
                root, report_day, pin, source_only=True
            )
            upload_receipt(home, scope, info)
            retained_bridge(report, selection, info, root / "reports/_evidence")
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
    state = root / "reports/_evidence/.paper-entry"
    if args.command == "plan":
        artifacts = latest_report(root, report_day, pin)
        upload = upload_receipt(home, scope, artifacts[2])
        if args.handoff:
            binding = handoff_identity(report_day, scope["launch"], artifacts[2])
            path = home / ".config/trend-animals/daily-state" / (
                f"{report_day}.paper-handoff.json"
            )
            notice = read_json(path)
            if (
                args.handoff != binding["token"]
                or any(notice.get(key) != value for key, value in binding.items())
                or notice.get("session") != scope["id"]
                or notice.get("status") not in {"requested", "displayed"}
                or upload.get("source") != "host Foundation"
                or not 0 <= now.timestamp() - upload.get("timestamp", 0) <= 300
            ):
                raise ValueError("matching host planning notice is unavailable")
            retained_bridge(*artifacts, root / "reports/_evidence")
            plan, _ = handoff_plan(state / "state.sqlite3", scope, artifacts[2])
            if plan is not None:
                emit(public_plan(plan))
                return 0
    if args.command == "status":
        database = state / "state.sqlite3"
        result = (
            PaperEntry(database, OfflineBroker(), scope, check_plan=check_plan).status(
                now=now
            )
            if database.exists()
            else None
        )
        emit(result)
        return 0
    state.mkdir(mode=0o700, exist_ok=True)
    require_private_dir(state)
    if args.command == "manual-holdings":
        snapshot = read_json(args.snapshot)
        info = latest_report(root, report_day, pin, source_only=True)[2]
        upload_receipt(home, scope, info)
        if snapshot.get("report_version") != info["revision"]:
            emit({"recorded": False})
            return 2
        proof = None
        for record in session_records(home, scope["id"], root):
            at = instant(record.get("timestamp"))
            payload = record.get("payload", {})
            text = message_text(payload, "user")
            meta = payload.get("internal_chat_message_metadata_passthrough")
            if meta and (
                not isinstance(meta, dict)
                or meta.get("content_item_kinds") != ["user.text"]
                or meta.get("content_item_metadata")
                != [{"provenance": {"type": "user"}}]
            ):
                continue
            if (
                record.get("type") == "response_item"
                and at is not None
                and at <= now
                and at.astimezone(TAIPEI).date().isoformat() == report_day
                and text == "模拟持仓 " + encode(snapshot)
                and payload.get("recipient") in (None, "all")
            ):
                proof = record
        service = PaperEntry(
            state / "state.sqlite3",
            OfflineBroker(),
            scope,
            verify_reply=lambda reply: proof is not None and reply is proof,
        )
        recorded = service.record_manual_holdings(snapshot, proof, now=now)
        if recorded:
            refresh_report(root, report_day, pin, now)
        emit({"recorded": recorded})
        return 0 if recorded else 2
    broker = FutuPaperBroker(settings(home), clock=utc_now)
    authorized = None
    refresh_needed = args.command != "plan"

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
            recheck_late=lambda line, now: late_execution_terms(broker, line, now),
        )
        if args.command == "plan":
            plan = plan_report(
                service,
                artifacts,
                root / "reports/_evidence",
                now,
                clock=utc_now,
                was_delivered=lambda plan, created: plan_was_delivered(
                    home, root, scope, plan, created, utc_now()
                ),
            )
            refresh_needed = True
            emit(plan)
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

            return run_action(
                args,
                service,
                plan,
                replies,
                record,
                refresh=lambda: refresh_report(root, report_day, pin, utc_now()),
            )
    finally:
        try:
            if refresh_needed:
                refresh_report(root, report_day, pin, utc_now())
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


def run_action(args, service, plan, replies, record, refresh=lambda: None):
    if args.command in {"approve", "cycle"} or args.retry_item:
        records = replies()
        latest = records[-1] if records else None
        if args.command in {"approve", "cycle"}:
            for reply in records[:-1]:
                if reply and reply["decision"] == "deny":
                    record(reply)
            decision = (
                record(latest) if latest and latest["decision"] != "retry" else None
            )
            if decision is not None:
                emit({"plan": plan["id"], **decision})
                if args.command == "cycle":
                    wait_for_open(
                        service,
                        plan,
                        replies,
                        record,
                        refresh,
                        initial_late=(latest, decision)
                        if decision.get("late_items")
                        else None,
                    )
                    follow_receipts(service, plan, refresh)
                else:
                    submit_late(service, plan, latest, decision)
                return 0
            if args.command == "cycle" and service.has_approval(plan["id"]):
                wait_for_open(service, plan, replies, record, refresh)
                follow_receipts(service, plan, refresh)
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
    wait_for_open(service, plan, replies, record, refresh)
    return 0


def submit_late(service, plan, reply, decision):
    items = decision.get("late_items", ())
    if items:
        emit(
            public_results(
                plan["id"],
                [
                    row
                    for item in items
                    for row in service.submit(
                        plan["id"], now=utc_now(), item=item, late_reply=reply
                    )
                ],
            )
        )


class ReceiptObserver:
    """One read-only I/O round in flight; never join it on an opening deadline."""

    def __init__(self, service, plan, refresh):
        self.service, self.plan, self.refresh = service, plan, refresh
        self.thread = None

    def poll(self):
        if self.thread is not None and self.thread.is_alive():
            return
        if not hasattr(self.service.broker, "receipt_reader"):
            return
        # Snapshot the reader configuration on the placement thread. SDK contexts
        # themselves belong only to the observer and are closed by that observer.
        reader = self.service.broker.receipt_reader()
        observer = PaperEntry(
            self.service.database, reader, self.service.session, clock=utc_now
        )

        def observe():
            try:
                observer.reconcile(self.plan["id"], now=utc_now(), pending_only=True)
                self.refresh()
            except (OSError, ValueError, sqlite3.Error):
                pass
            finally:
                reader.close()

        self.thread = Thread(target=observe, daemon=True)
        self.thread.start()


def wait_for_open(
    service, plan, replies, record, refresh=lambda: None, *, initial_late=None
):
    plan_id = plan["id"]
    started = utc_now()
    observer = ReceiptObserver(service, plan, refresh)
    seen = set()
    queued_late = [initial_late] if initial_late else []

    def dispatch_late():
        for reply, decision in queued_late:
            submit_late(service, plan, reply, decision)
        queued_late.clear()

    def observe_replies():
        observed = replies()
        latest = observed[-1] if observed else None
        for reply in observed:
            if not reply or reply["event_id"] in seen:
                continue
            if reply["decision"] != "deny" and (
                reply is not latest or reply["decision"] != "approve"
            ):
                continue
            seen.add(reply["event_id"])
            decision = record(reply)
            if decision is not None and decision["recorded"]:
                emit({"plan": plan_id, **decision})
                if decision.get("late_items"):
                    queued_late.append((reply, decision))

    openings = [opening for opening in service.openings(plan_id) if opening > started]
    if not openings:
        observe_replies()
        dispatch_late()
    for opening in openings:
        try:
            while (observed := utc_now()) < opening:
                if not service.openings(plan_id):
                    break
                observe_replies()
                # Prioritize the scheduled group when its dispatch window is
                # imminent. Recheck the fresh reply's deadline after the group.
                if opening - utc_now() > LATE_DISPATCH_SLACK:
                    dispatch_late()
                observer.poll()
                remaining = (opening - utc_now()).total_seconds()
                if remaining > 0:
                    sleep_open(min(15, remaining))
            observe_replies()
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
        dispatch_late()
        if opening != openings[-1]:
            observer.poll()
        else:
            service.reconcile(plan_id, now=utc_now(), pending_only=True)
        refresh()
    emit({"plan": plan_id, "status": service.status(now=utc_now())})


def follow_receipts(service, plan, refresh):
    """A supervised command follows existing orders only, never resubmits them."""
    closes = max((instant(line["closes_at"]) for line in plan["lines"]), default=None)
    while True:
        now = utc_now()
        outcomes = service.reconcile(plan["id"], now=now, pending_only=True)
        refresh()
        emit(public_results(plan["id"], outcomes))
        if (
            not closes
            or now >= closes
            or not any(row["state"] in {"active", "unknown"} for row in outcomes)
        ):
            return
        sleep_open(min(30, (closes - now).total_seconds()))


if __name__ == "__main__":
    raise SystemExit(main())
