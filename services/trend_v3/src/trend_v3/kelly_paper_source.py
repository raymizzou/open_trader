"""Read the frozen report and the canonical paper store without broker I/O."""

import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .paper_authority import latest_report, strict_json
from .paper_entry import PaperEntry, fingerprint
from .paper_evidence import (
    board_seat_verified,
    board_verified,
    day,
    decimal,
    identity,
    instant,
    paper_report_projection,
    product_for,
    target,
    text,
)
from .paper_facts import verify_membership
from .paper_store import SCHEMA

_CURRENT_SCHEMA = frozenset(
    " ".join(statement.replace(" IF NOT EXISTS", "").split()) for statement in SCHEMA
)
# The execution store before feab08de. Holdings added these tables without
# migrating any of the original execution tables.
_LEGACY_SCHEMA = frozenset(
    statement
    for statement in _CURRENT_SCHEMA
    if statement.split()[2]
    in {
        "plans",
        "current_plan",
        "replies",
        "approvals",
        "decisions",
        "attempts",
        "retries",
    }
)


@dataclass(frozen=True)
class FrozenReport:
    projection: dict
    selection: dict
    info: dict

    @property
    def digest(self):
        return sha256(
            (self.info["projection_sha256"] + self.info["selection_sha256"]).encode()
        ).hexdigest()

    @property
    def ref(self):
        return f"{self.info['report_date']}/{self.info['revision']}/{self.digest}"

    def matches(self, plan):
        return (
            plan["date"] == self.info["report_date"]
            and plan["report_version"] == self.info["revision"]
            and plan["revision"] == int(self.info["revision"][1:])
            and plan["report_hash"] == self.digest
        )


def frozen_report(root, date, revision, release):
    return FrozenReport(
        *latest_report(
            root.resolve(strict=True),
            date,
            release,
            frozen_revision=revision,
            source_only=True,
        )
    )


def _private_database(path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise ValueError("expected a private execution database")


@dataclass(frozen=True)
class ExecutionSnapshot:
    plans: dict
    attempts: list
    approvals: dict
    retries: dict
    replies: list
    orders: dict


def execution_snapshot(path: Path, report: FrozenReport):
    path = path.absolute()
    _private_database(path)
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            _private_database(path.with_name(path.name + suffix))
        except FileNotFoundError:
            pass
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        schemas = {
            " ".join(row[0].split())
            for row in db.execute("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL")
        }
        if schemas not in (_CURRENT_SCHEMA, _LEGACY_SCHEMA):
            raise ValueError("expected the canonical paper execution schema")
        plans = {}
        for row in db.execute(
            "SELECT * FROM plans WHERE report_day = ?", (report.info["report_date"],)
        ):
            body = strict_json(row["body"])
            if (
                body["id"] != row["id"]
                or body["date"] != row["report_day"]
                or (
                    fingerprint(
                        {
                            k: v
                            for k, v in body.items()
                            if k not in {"id", "content_hash"}
                        }
                    )
                    != body["content_hash"]
                )
            ):
                raise ValueError("invalid paper plan identity")
            if report.matches(body):
                plans[row["id"]] = body | {"created": row["created"]}
        attempts = []
        orders = {}
        order_plans = {}
        for row in db.execute("SELECT * FROM attempts"):
            if row["plan"] in plans:
                attempts.append(dict(row))
            if row["order_id"]:
                if row["plan"] not in order_plans:
                    (body,) = db.execute(
                        "SELECT body FROM plans WHERE id=?", (row["plan"],)
                    ).fetchone()
                    order_plans[row["plan"]] = {
                        line["item"]: line["account_identity"]
                        for line in strict_json(body)["lines"]
                    }
                key = (order_plans[row["plan"]][row["item"]], row["order_id"])
                orders.setdefault(key, []).append(row["token"])
        approvals = {
            (row["plan"], row["item"]): dict(row)
            for row in db.execute("SELECT * FROM approvals")
            if row["plan"] in plans
        }
        retries = {
            row["token"]: dict(row) for row in db.execute("SELECT * FROM retries")
        }
        replies = [
            (row["plan"], row["event_id"], strict_json(row["body"]))
            for row in db.execute("SELECT * FROM replies")
            if row["plan"] in plans
        ]
    return ExecutionSnapshot(plans, attempts, approvals, retries, replies, orders)


def opening_group(report, line):
    projection = paper_report_projection(
        report.projection, report.selection, report.info
    )
    collection = projection["collection"]
    evidence = collection["evidence"]
    report_day = day(projection["date"])
    if not board_verified(projection, collection, evidence, report_day):
        raise ValueError("board attribution is unverified")
    market = line["market"]
    expected = target(collection, market, report_day)
    if expected is None or expected.isoformat() != line["signal_date"]:
        raise ValueError("opening source date is unverified")
    direction = line["direction"]
    matches = []
    for index, group in enumerate(projection.get(direction, [])):
        for asset in group.get("direct", []):
            if identity(asset.get("instrument_id")) != line["instrument_id"]:
                continue
            prefix = f"/{direction}/{index}"
            kind = asset["instrument_type"]
            if (
                not board_seat_verified(projection, evidence, direction, index)
                or asset.get("market") != market
                or product_for(kind, market) != line["product"]
                or asset.get("currency") not in (None, line["currency"])
                or not all(
                    asset.get(flag) is True
                    for flag in (
                        "direct_verified",
                        "platform_match_verified",
                        "watch_eligible",
                    )
                )
                or asset.get("lookup")
            ):
                raise ValueError("opening membership is unverified")
            verify_membership(projection, asset, group, prefix)
            code = asset["code"]
            if line["product"] == "future":
                matches_code = line["economic_instrument"] == code.upper()
            else:
                prefix = (
                    market
                    if market != "CN"
                    else ("SH" if code.startswith("6") else "SZ")
                )
                matches_code = line["code"] == f"{prefix}.{code}"
            if not matches_code:
                raise ValueError("opening instrument does not match")
            selection = [
                row
                for row in report.selection["instruments"]
                if identity(row.get("id")) == line["instrument_id"]
                and row.get("code") == code
                and row.get("group") == group["name"]
                and row.get("relationship") == "直属"
                and row.get("selected_new_position") is True
                and row.get("state") == "已触发"
                and row.get("actual_data_days", [row.get("data_day")])
                == [expected.isoformat()]
            ]
            if len(selection) != 1:
                raise ValueError("opening selection is unverified")
            matches.append(group["name"])
    if len(matches) != 1:
        raise ValueError("opening group is missing or ambiguous")
    return matches[0]


def authorized_attempt(token, snapshot):
    matching = [row for row in snapshot.attempts if row["token"] == token]
    if len(matching) != 1:
        raise ValueError("order lacks a matching official attempt")
    (attempt,) = matching
    plan = snapshot.plans[attempt["plan"]]
    (line,) = [row for row in plan["lines"] if row["item"] == attempt["item"]]
    economic_key = PaperEntry.economic_key(line)
    number = attempt["number"]
    if (
        type(number) is not int
        or number <= 0
        or attempt["token"] != "TV3" + fingerprint([economic_key, number])[:25]
        or economic_key != attempt["economic_key"]
        or attempt["state"] not in {"active", "filled", "cancelled"}
    ):
        raise ValueError("official attempt is unverified or declined")
    opens, closes = instant(line["opens_at"]), instant(line["closes_at"])
    approval = snapshot.approvals[(plan["id"], line["item"])]
    if number == 1:
        proof, lower, upper = approval, instant(plan["created"]), opens
    else:
        (previous,) = [
            row
            for row in snapshot.attempts
            if row["plan"] == plan["id"]
            and row["economic_key"] == economic_key
            and row["number"] == number - 1
            and row["item"] == attempt["item"]
        ]
        proof, lower, upper = snapshot.retries[previous["token"]], opens, closes
        if (
            previous["state"] not in {"cancelled", "rejected"}
            or not previous["reconciled"]
            or instant(previous["reconciled"]) >= instant(proof["at"])
            or previous["quantity"] - previous["filled"] != proof["quantity"]
        ):
            raise ValueError("retry proof does not match the earlier attempt")
    decision = "approve" if number == 1 else "retry"
    at = instant(proof["at"])
    if not (
        at is not None
        and lower <= at < upper
        and 0 < proof["quantity"] == attempt["quantity"] <= line["quantity"]
        and any(
            key == plan["id"]
            and event_id == reply.get("event_id")
            and reply.get("plan") == key
            and reply.get("owner") == plan["owner"]
            and reply.get("session") == plan["session"]
            and reply.get("report_version") == plan["report_version"]
            and reply.get("source") == "user_reply"
            and text(reply.get("author"))
            and reply.get("decision") == decision
            and instant(reply.get("at")) == at
            and instant(plan["created"]) <= instant(reply.get("delivered_at")) <= at
            and (number == 1 or event_id == proof["event_id"])
            and any(
                selected.get("item") == line["item"]
                and selected.get("quantity") == proof["quantity"]
                and selected.get("open") == line["opens_at"]
                and (number == 1 or selected.get("after_attempt") == previous["token"])
                for selected in reply["items"]
            )
            for key, event_id, reply in snapshot.replies
        )
    ):
        raise ValueError("official authorization is unverified")
    scope = plan["account_scope"][line["account"]]
    if (
        scope["identity"] != line["account_identity"]
        or not attempt["order_id"]
        or snapshot.orders[(scope["identity"], attempt["order_id"])] != [token]
        or (
            line["product"] == "future"
            and decimal(scope["multipliers"].get(line["code"]))
            != decimal(line["multiplier"])
        )
    ):
        raise ValueError("official account is unverified")
    return attempt, line, scope, opens if number == 1 else at


def report_catalog(report):
    projection = report.projection
    collection = projection["collection"]
    parent = collection["parent_board"]
    rows = parent["groups"]
    observed = instant(parent.get("observed_at"))
    cutoff = instant(projection.get("cutoff"))
    report_day = day(projection["date"])
    board_day = day(projection.get("board_date"))
    if not (
        report_day is not None
        and board_day is not None
        and board_day <= report_day
        and text(projection.get("board_version"))
        and parent.get("complete") is True
        and parent.get("state") == "verified"
        and text(parent.get("source"))
        and isinstance(rows, list)
        and rows
        and type(parent.get("row_count")) is int
        and parent["row_count"] == len(rows)
        and parent.get("data_date") == projection["board_date"]
        and parent.get("version") == projection["board_version"]
        and observed is not None
        and cutoff is not None
        and observed <= cutoff
    ):
        raise ValueError("complete frozen class list is unverified")
    groups = [row["name"] for row in rows]
    if (
        len(set(groups)) != len(groups)
        or not all(text(name) for name in groups)
        or any(
            (strength := decimal(row.get("strength"))) is None
            or not 0 <= strength <= 100
            for row in rows
        )
    ):
        raise ValueError("invalid frozen class list")
    day_ = projection["date"]
    return {
        "fabricated": False,
        "context": "模拟盘",
        "date": day_,
        "version": report.info["revision"],
        "board_date": projection["board_date"],
        "board_version": projection["board_version"],
        "source_sha256": report.info["report_sha256"],
        "groups": groups,
        "group_labels": {name: name for name in groups},
        "reports": {report.ref: day_[:7]},
        "fixed_scope": {
            "environment": "SIMULATE",
            "rule": report.ref,
            "period": day_[:7],
        },
    }
