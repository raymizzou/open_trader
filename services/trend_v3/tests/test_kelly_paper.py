"""Replay synthetic broker documents against the normal paper execution store."""

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from copy import deepcopy
from hashlib import sha256

from test_paper_entry import (
    DATA_DAY,
    DAY,
    OPEN,
    PREOPEN,
    RELEASE,
    Broker,
    approval,
    daily_files,
    offline_reply,
    report,
    retry_reply,
    session,
)

from trend_v3.paper_entry import PaperEntry
from trend_v3.paper_futu import FutuPaperBroker

ACCOUNT = FutuPaperBroker.account_identity({"id": 1234, "trade_market": "US"})


class ReceiptBroker(Broker):
    def __init__(self):
        super().__init__()
        self.accounts["paper-us"]["identity"] = ACCOUNT
        self.accounts["paper-us"]["net_value"] = "200000"

    def account_scope(self):
        return {
            "paper-us": {
                "identity": ACCOUNT,
                "category": "securities",
                "market": "US",
                "currency": "USD",
                "multipliers": {},
            }
        }


class Replay:
    def __init__(
        self,
        tmp_path,
        filled=0,
        *,
        submit=True,
        holdings="active",
        ambiguous=False,
        hidden=False,
    ):
        self.folder = tmp_path.resolve() / "private"
        self.folder.mkdir(mode=0o700, parents=True)
        self.execution = self.folder / "state.sqlite3"
        self.ledger = self.folder / "kelly.sqlite"
        self.package = self.folder / "receipt.json"
        self.document = self.folder / "fixture-broker-export.csv"
        self.document.write_bytes(
            b"synthetic offline fixture, see normalized test values\n"
        )
        self.document.chmod(0o600)
        self.proof = {
            "file": self.document.name,
            "sha256": sha256(self.document.read_bytes()).hexdigest(),
        }
        self.broker = ReceiptBroker()
        self.service = PaperEntry(
            self.execution, self.broker, session(), verify_reply=offline_reply
        )
        projection, facts = report("FIXTURE" if submit else "UNROUTED")
        if not submit:
            facts["instruments"].clear()
        if ambiguous:
            group = deepcopy(projection["long"][0])
            group.update(name="Second verified group", seat=2)
            projection["long"].append(group)
            evidence = projection["collection"]["evidence"]
            for field in ("coverage", "board_strength"):
                evidence[f"/long/1/{field}"] = evidence[f"/long/0/{field}"] | {
                    "identity": group["name"]
                }
        projection["cutoff"] = PREOPEN.isoformat()
        projection["collection"]["parent_board"] = {
            "state": "verified",
            "complete": True,
            "source": "synthetic independent board",
            "data_date": DATA_DAY,
            "version": "v1",
            "row_count": 2,
            "observed_at": PREOPEN.isoformat(),
            "groups": [
                {"name": "Verified board group", "strength": 85},
                {"name": "Empty class", "strength": 60},
            ],
        }
        root, _, _ = daily_files(self.folder, projection)
        self.root = root
        self.revision = root / "reports/_evidence" / DAY / "v02"
        info = json.loads((self.revision / "report-verification.json").read_bytes())
        if hidden:
            public = deepcopy(projection)
            public["selection_mode"] = "threshold-v1"
            public["long"] = []
            evidence = public["collection"]["evidence"]
            for key in list(evidence):
                if key.startswith("/long/"):
                    del evidence[key]
            selection = json.loads(
                (self.revision / "selection-evidence.json").read_bytes()
            )
            public_raw = json.dumps(public).encode()
            projection_digest = sha256(public_raw).hexdigest()
            selection.update(
                schema="trend-v3-paper-evidence/v1",
                revision="v02",
                source={"commit": RELEASE},
                cutoff=projection["cutoff"],
                projection_sha256=projection_digest,
                paper_projection=projection | {"revision": "v02"},
            )
            selection_raw = json.dumps(selection).encode()
            (self.revision / "reader-projection.json").write_bytes(public_raw)
            (self.revision / "selection-evidence.json").write_bytes(selection_raw)
            info.update(
                projection_sha256=projection_digest,
                selection_sha256=sha256(selection_raw).hexdigest(),
            )
            (self.revision / "report-verification.json").write_text(json.dumps(info))
        self.digest = sha256(
            (info["projection_sha256"] + info["selection_sha256"]).encode()
        ).hexdigest()
        self.plan = self.service.plan(
            projection,
            facts
            | {
                "daily_evidence_hash": self.digest,
                "report_version": "v02",
                "report_revision": 2,
            },
            now=PREOPEN,
        )
        if submit:
            assert self.service.approve(
                self.plan["id"], approval(self.plan), now=PREOPEN
            )
            self.broker.reply = {
                "state": holdings,
                "id": "fixture-open-order",
                "filled": filled,
                "filled_notional": str(filled * 10),
            }
            self.service.submit(self.plan["id"], now=OPEN)
            with closing(sqlite3.connect(self.execution)) as db:
                (self.token,) = db.execute("SELECT token FROM attempts").fetchone()

    def cli(self, command, *extra, success=True, ledger=None):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "trend_v3.kelly_paper",
                command,
                "--execution-db",
                str(self.execution),
                "--ledger",
                str(ledger or self.ledger),
                "--root",
                str(self.root),
                "--date",
                DAY,
                "--revision",
                "v02",
                "--release",
                RELEASE,
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if success:
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)
        assert result.returncode == 1, result
        assert not result.stdout
        return result.stderr

    def sync(self, orders, *, settlements=(), success=True, **options):
        def document(row, key, **facts):
            name = self.folder / (
                "proof-" + sha256((key + row[key]).encode()).hexdigest() + ".json"
            )
            record = {
                k: v
                for k, v in row.items()
                if k
                not in {
                    "proof",
                    "source",
                    "attempt",
                    "opening_order_id",
                }
            }
            record.update(facts)
            group = "orders" if key == "order_id" else "settlements"
            record[key] = row[key]
            name.write_text(
                json.dumps(
                    {
                        "schema": "trend-v3-kelly-document-v1",
                        "source": row.get("source", "broker_export"),
                        "original": row.get("proof", self.proof),
                        group: [record],
                    }
                )
            )
            name.chmod(0o600)
            binding = {
                key: row[key],
                "proof": {
                    "file": name.name,
                    "sha256": sha256(name.read_bytes()).hexdigest(),
                },
            }
            if group == "orders":
                binding.update(
                    {k: row[k] for k in ("attempt", "opening_order_id") if k in row}
                )
            return binding

        self.package.write_text(
            json.dumps(
                {
                    "schema": "trend-v3-kelly-receipts-v1",
                    "orders": [document(row, "order_id") for row in orders],
                    "settlements": [
                        document(
                            row,
                            "opening_order_id",
                            broker="Futu",
                            environment="SIMULATE",
                            account_id="1234",
                            account_market="US",
                        )
                        for row in settlements
                    ],
                }
            )
        )
        self.package.chmod(0o600)
        return self.cli(
            "sync", "--receipts", str(self.package), success=success, **options
        )

    def sql(self, query):
        with closing(sqlite3.connect(self.ledger)) as db:
            return db.execute(query).fetchall()


def test_acknowledged_order_without_fills(tmp_path):
    case = Replay(tmp_path)
    before = case.execution.read_bytes()
    case.cli("init")
    audit = case.cli("inspect")
    assert audit["execution"]["accepted"] == 1
    assert audit["execution"]["accepted_without_fills"] == 1
    assert audit["kelly"]["confirmed_samples"] == 0
    assert case.cli("sync")["kelly"]["rounds"] == 0
    assert case.execution.read_bytes() == before
    assert case.sql("SELECT COUNT(*) FROM trade_events") == [(0,)]

    empty = Replay(tmp_path / "empty", submit=False)
    empty.cli("init")
    result = empty.cli("sync")
    assert result["execution"]["plans"] == 1
    assert result["execution"]["accepted"] == 0
    assert result["kelly"]["confirmed_samples"] == 0
    catalog = empty.cli("catalog")
    assert catalog["fabricated"] is False
    assert catalog["groups"] == ["Verified board group", "Empty class"]
    assert catalog["fixed_scope"] == {
        "environment": "SIMULATE",
        "rule": f"{DAY}/v02/{empty.digest}",
        "period": DAY[:7],
    }


def deal(id_, at, qty, price):
    return {"deal_id": id_, "executed_at": at, "quantity": qty, "price": str(price)}


def order(id_, side, qty, filled, notional, deals, **binding):
    return {
        "broker": "Futu",
        "environment": "SIMULATE",
        "account_id": "1234",
        "account_market": "US",
        "order_id": id_,
        "code": "US.FIXTURE",
        "product": "stock",
        "currency": "USD",
        "side": side,
        "order_quantity": qty,
        "state": "filled" if qty == filled else "active",
        "filled_quantity": filled,
        "filled_notional": str(notional),
        "deals": deals,
        **binding,
    }


OPEN_ID = "fixture-open-order"
OPEN_TIME = OPEN.isoformat()
ADD_TIME = OPEN.replace(minute=31).isoformat()
REDUCE_TIME = OPEN.replace(minute=32).isoformat()
CLOSE_TIME = OPEN.replace(minute=33).isoformat()
FEE_TIME = OPEN.replace(minute=34).isoformat()


def opening(case, filled=100, deals=None, **overrides):
    return (
        order(
            OPEN_ID,
            "buy",
            case.plan["lines"][0]["quantity"],
            filled,
            10 * filled,
            [deal("open-deal", OPEN_TIME, 100, 10)] if deals is None else deals,
            attempt=case.token,
        )
        | overrides
    )


def position_action(id_, side, qty, price, at, before, after):
    return order(
        id_,
        side,
        qty,
        qty,
        qty * price,
        [deal(id_ + "-deal", at, qty, price)],
        opening_order_id=OPEN_ID,
        position_before=before,
        position_after=after,
    )


def test_confirmed_fill_binds_opening_report(tmp_path):
    case = Replay(tmp_path, 100, hidden=True)
    case.cli("init")
    before = case.execution.read_bytes()
    opened = opening(case)
    add = position_action("fixture-add", "buy", 50, 12, ADD_TIME, 100, 150)
    reduce = position_action("fixture-reduce", "sell", 50, 13, REDUCE_TIME, 150, 100)
    close = position_action("fixture-close", "sell", 100, 11, CLOSE_TIME, 100, 0)
    assert case.sync([opened])["kelly"]["open_rounds"] == 1
    assert case.sync([opened, add, reduce])["kelly"]["open_rounds"] == 1
    orders = [opened | {"state": "cancelled"}, add, reduce, close]
    case.broker.reconciled = {
        "state": "cancelled",
        "id": OPEN_ID,
        "filled": 100,
        "filled_notional": "1000",
    }
    case.service.reconcile(case.plan["id"], now=OPEN.replace(minute=34))
    before_after_reconcile = case.execution.read_bytes()
    assert before_after_reconcile != before
    assert case.sync(orders)["kelly"]["closed_pending_costs"] == 1
    assert case.cli("inspect")["kelly"]["confirmed_samples"] == 0
    settlement = {
        "opening_order_id": OPEN_ID,
        "currency": "USD",
        "confirmed_at": FEE_TIME,
        "fees": {
            OPEN_ID: "2",
            "fixture-add": "1",
            "fixture-reduce": "1",
            "fixture-close": "1",
        },
    }
    assert (
        case.sync(orders, settlements=[settlement])["kelly"]["confirmed_samples"] == 1
    )
    assert case.cli("inspect")["kelly"]["confirmed_samples"] == 1
    assert case.sql("""
        SELECT environment, direction, source_group, report_ref, net_pnl, return_base
        FROM rounds
    """) == [
        (
            "SIMULATE",
            "long",
            "Verified board group",
            f"{DAY}/v02/{case.digest}",
            "145",
            "1600",
        )
    ]
    assert case.sql(
        "SELECT COUNT(*) FROM trade_events WHERE event_type IN ('buy','sell')"
    ) == [(4,)]
    assert case.execution.read_bytes() == before_after_reconcile


def test_cumulative_and_manual_replay(tmp_path):
    case = Replay(tmp_path, 60)
    case.cli("init")
    first = deal("first", OPEN_TIME, 60, 10)
    first_receipt = opening(case, filled=60, deals=[first])
    assert case.sync([first_receipt])["kelly"]["rounds"] == 1
    case.broker.reconciled = {
        "state": "filled",
        "id": OPEN_ID,
        "filled": 200,
        "filled_notional": "2280",
    }
    case.service.reconcile(case.plan["id"], now=OPEN.replace(minute=32))
    second = deal("second", ADD_TIME, 140, 12)
    checkpoints = [
        {"filled_quantity": 60, "filled_notional": "600"},
        {"filled_quantity": 200, "filled_notional": "2280"},
        {"filled_quantity": 200, "filled_notional": "2280"},
    ]
    final = opening(
        case,
        filled=200,
        deals=[first, second],
        state="filled",
        filled_notional="2280",
        checkpoints=checkpoints,
    )
    bad = final | {
        "checkpoints": [checkpoints[0] | {"filled_notional": "599"}, checkpoints[1]]
    }
    case.sync([bad], success=False)
    assert case.sql("SELECT quantity, notional FROM trade_events") == [("60", "600")]

    assert case.sync([final])["kelly"]["rounds"] == 1
    assert case.sync([final | {"source": "manual_document"}])["kelly"]["rounds"] == 1
    assert case.sync([final])["kelly"]["rounds"] == 1
    assert case.sql(
        "SELECT quantity, notional, occurred_at FROM trade_events ORDER BY occurred_at"
    ) == [
        ("60", "600", "2026-11-24T14:30:00.000000+00:00"),
        ("140", "1680", "2026-11-24T14:31:00.000000+00:00"),
    ]

    empty = Replay(tmp_path / "different-source", 200, holdings="filled")
    empty.cli("init")
    aggregate = opening(empty, filled=200, deals=[])
    result = empty.sync([aggregate])
    assert result["receipt_rounds_pending_deal_details"] == 1
    assert result["kelly"]["rounds"] == 0
    assert empty.sql("SELECT COUNT(*) FROM trade_events") == [(0,)]

    retry = Replay(tmp_path / "retry", holdings="rejected")
    retry.cli("init")
    plan = retry.plan["id"]
    when = OPEN.replace(minute=31)
    retry.broker.reconciled = retry.broker.reply
    (prior,) = retry.service.reconcile(plan, now=when)
    when = when.replace(second=1)
    assert retry.service.approve(
        plan,
        retry_reply(retry.plan, prior, when, 200),
        now=when,
    )
    retry.broker.reply = {
        "state": "active",
        "id": "fixture-retry",
        "filled": 20,
        "filled_notional": "200",
    }
    retry.service.submit(plan, now=when, retry=True)
    with closing(sqlite3.connect(retry.execution)) as db:
        (token,) = db.execute("SELECT token FROM attempts WHERE number=2").fetchone()
    confirmed = order(
        "fixture-retry",
        "buy",
        200,
        20,
        200,
        [deal("retry-deal", when.isoformat(), 20, 10)],
        attempt=token,
    )
    assert retry.sync([confirmed])["kelly"]["open_rounds"] == 1
    assert retry.sql("SELECT quantity FROM trade_events") == [("20",)]


def test_rejects_unverified_attribution(tmp_path):
    case = Replay(tmp_path, 100)
    case.cli("init")
    valid = opening(case)
    source = case.execution.read_bytes()
    no_mutation = [
        valid
        | {"opening_order_id": OPEN_ID, "proof": case.proof | {"sha256": "0" * 64}},
        valid | {"environment": "REAL"},
        valid | {"account_id": "55555"},
        valid | {"account_market": "HK"},
        valid | {"order_id": "foreign-sensitive-id"},
        valid | {"attempt": "another-sensitive-id"},
        valid | {"order_quantity": 201},
        valid | {"currency": "HKD"},
        valid | {"filled_notional": "999"},
        opening(case, deals=[deal("preopen", PREOPEN.isoformat(), 100, 10)]),
        opening(case, deals=[deal("no-time", "", 100, 10)]),
    ]
    for receipt in no_mutation:
        error = case.sync([receipt], success=False)
        assert not any(
            secret in error
            for secret in ("55555", "foreign-sensitive-id", "another-sensitive-id")
        )
        assert case.sql("SELECT COUNT(*) FROM rounds") == [(0,)]
    assert case.execution.read_bytes() == source

    bad_close = position_action("bad-close", "sell", 100, 11, CLOSE_TIME, 120, 20)
    case.sync([valid, bad_close], success=False)
    assert case.sql("SELECT COUNT(*) FROM trade_events") == [(0,)]
    bad_close = position_action("bad-close", "sell", 100, 11, CLOSE_TIME, 100, 0)
    settlement = {
        "opening_order_id": OPEN_ID,
        "currency": "USD",
        "confirmed_at": FEE_TIME,
        "fees": {OPEN_ID: "2"},
    }
    case.sync([valid, bad_close], settlements=[settlement], success=False)
    assert case.sql("SELECT COUNT(*) FROM rounds") == [(0,)]

    case.sync([valid], success=False, ledger=case.execution)
    assert case.execution.read_bytes() == source
    foreign = case.folder / "unrelated.sqlite"
    with closing(sqlite3.connect(foreign)) as db:
        db.execute("CREATE TABLE note (body TEXT)")
    foreign.chmod(0o600)
    unrelated = foreign.read_bytes()
    case.sync([valid], success=False, ledger=foreign)
    assert foreign.read_bytes() == unrelated

    with closing(sqlite3.connect(case.execution)) as db, db:
        db.execute("DELETE FROM approvals")
    case.sync([valid], success=False)
    assert case.sql("SELECT COUNT(*) FROM rounds") == [(0,)]

    ambiguous = Replay(tmp_path / "ambiguous", 100, ambiguous=True)
    ambiguous.cli("init")
    ambiguous.sync([opening(ambiguous)], success=False)
    assert ambiguous.sql("SELECT COUNT(*) FROM rounds") == [(0,)]

    # A new projection with unchanged report metadata breaks the digest binding.
    projection = case.revision / "reader-projection.json"
    projection.write_bytes(projection.read_bytes() + b"\n")
    case.sync([valid], success=False)
    assert case.sql("SELECT COUNT(*) FROM rounds") == [(0,)]


def test_legacy_execution_schema_read_only(tmp_path):
    case = Replay(tmp_path, 100)
    # feab08de introduced only these two tables. The other seven retain their
    # original SQL definitions and the real PaperEntry fixture rows.
    with closing(sqlite3.connect(case.execution)) as db, db:
        db.execute("DROP TABLE position_history")
        db.execute("DROP TABLE positions")
    before = case.execution.read_bytes()
    assert case.cli("init")["execution"]["approved_lines"] == 1
    assert case.cli("inspect")["execution"]["accepted"] == 1
    assert case.sync([opening(case)])["kelly"]["open_rounds"] == 1
    assert case.sql("SELECT quantity, notional FROM trade_events") == [("100", "1000")]
    assert case.execution.read_bytes() == before

    for statements in (
        ("CREATE TABLE alien (unexpected TEXT)",),
        ("DROP TABLE alien", "DROP TABLE decisions"),
    ):
        with closing(sqlite3.connect(case.execution)) as db, db:
            for statement in statements:
                db.execute(statement)
        malformed = case.execution.read_bytes()
        case.cli("inspect", success=False)
        assert case.execution.read_bytes() == malformed
    assert case.sql("SELECT quantity FROM trade_events") == [("100",)]


def test_verified_display_board_without_entry_eligibility(tmp_path):
    case = Replay(tmp_path, submit=False, hidden=True)
    projection_path = case.revision / "reader-projection.json"
    selection_path = case.revision / "selection-evidence.json"
    verification_path = case.revision / "report-verification.json"
    projection = json.loads(projection_path.read_bytes())
    selection = json.loads(selection_path.read_bytes())
    verification = json.loads(verification_path.read_bytes())
    selected_before = projection["collection"]["parent_board"]
    # The visible board has its own edition even when member data is older.
    projection.update(board_date=DAY, board_version=DAY)
    projection["collection"]["targets"].pop("BOARD")
    projection["collection"]["evidence"].pop("/board_date")
    projection["collection"]["evidence"].pop("/board_version")
    parent = selected_before | {"data_date": DAY, "version": DAY}
    projection["collection"]["parent_board"] = parent
    assert selection["instruments"][0]["data_day"] == DATA_DAY

    def publish_fixture():
        projection_path.write_text(json.dumps(projection))
        selection["projection_sha256"] = sha256(
            projection_path.read_bytes()
        ).hexdigest()
        selection_path.write_text(json.dumps(selection))
        verification.update(
            projection_sha256=selection["projection_sha256"],
            selection_sha256=sha256(selection_path.read_bytes()).hexdigest(),
        )
        verification_path.write_text(json.dumps(verification))

    publish_fixture()
    catalog = case.cli("catalog")
    assert catalog["date"] == catalog["board_date"] == catalog["board_version"] == DAY
    assert catalog["groups"] == ["Verified board group", "Empty class"]
    assert catalog["group_labels"]["Empty class"] == "Empty class"
    case.cli("init")
    assert case.cli("sync")["kelly"] == {
        "rounds": 0,
        "open_rounds": 0,
        "closed_pending_costs": 0,
        "confirmed_samples": 0,
    }
    assert case.sql("SELECT COUNT(*) FROM trade_events") == [(0,)]

    projection_path.write_bytes(projection_path.read_bytes() + b"\n")
    case.cli("catalog", success=False)
    for override in (
        {"complete": False},
        {"row_count": 3},
        {"version": "another edition"},
        {"data_date": DATA_DAY},
        {"observed_at": OPEN.isoformat()},
        {
            "groups": [{"name": "Unverified class", "strength": "Infinity"}],
            "row_count": 1,
        },
    ):
        projection["collection"]["parent_board"] = parent | override
        publish_fixture()
        case.cli("catalog", success=False)


def test_documented_fee_conversion(tmp_path):
    case = Replay(tmp_path, 100, holdings="cancelled")
    case.cli("init")
    exit_order = position_action("fx-exit", "sell", 100, 11, CLOSE_TIME, 100, 0)
    orders = [opening(case, state="cancelled"), exit_order]
    assert case.sync(orders)["kelly"]["closed_pending_costs"] == 1
    order_rate = {
        "opening_order_id": OPEN_ID,
        "broker": "Futu",
        "environment": "SIMULATE",
        "account_id": "1234",
        "account_market": "US",
        "confirmed_at": FEE_TIME,
        "from_currency": "HKD",
        "to_currency": "USD",
        "rate": "0.125",
    }

    def conversion(**override):
        file = case.folder / "fixture-fx.json"
        file.write_text(
            json.dumps(
                {
                    "schema": "trend-v3-kelly-document-v1",
                    "source": "broker_export",
                    "original": case.proof,
                    "fee_conversions": [
                        order_rate
                        | override
                        | {"order_id": OPEN_ID, "fee_amount": "20"},
                        order_rate
                        | override
                        | {"order_id": "fx-exit", "fee_amount": "-4"},
                    ],
                }
            )
        )
        file.chmod(0o600)
        return {"file": file.name, "sha256": sha256(file.read_bytes()).hexdigest()}

    def settlement(proof):
        charge = {"currency": "HKD", "amount": "20"}
        rebate = {"currency": "HKD", "amount": "-4"}
        if proof is not None:
            charge["fx"] = rebate["fx"] = proof
        return {
            "opening_order_id": OPEN_ID,
            "currency": "USD",
            "confirmed_at": FEE_TIME,
            "fees": {OPEN_ID: charge, "fx-exit": rebate},
        }

    case.sync(orders, settlements=[settlement(None)], success=False)
    for mismatch in (
        {"account_id": "99"},
        {"opening_order_id": "different-round"},
        {"confirmed_at": CLOSE_TIME},
        {"from_currency": "USD", "to_currency": "HKD"},
        {"rate": "0"},
    ):
        case.sync(
            orders, settlements=[settlement(conversion(**mismatch))], success=False
        )
    assert case.sql("SELECT net_pnl FROM rounds") == [(None,)]
    assert case.sql("SELECT COUNT(*) FROM trade_events") == [(2,)]

    verified = settlement(conversion())
    assert case.sync(orders, settlements=[verified])["kelly"]["confirmed_samples"] == 1
    assert case.sync(orders, settlements=[verified])["kelly"]["confirmed_samples"] == 1
    assert case.sql("SELECT net_pnl, return_base, currency FROM rounds") == [
        ("98", "1000", "USD")
    ]
    assert case.sql(
        "SELECT event_type, cash_adjustment FROM trade_events "
        "WHERE cash_adjustment IS NOT NULL"
    ) == [("adjustment", "-2")]
    changed = settlement(conversion(rate="0.25"))
    case.sync(orders, settlements=[changed], success=False)
    assert case.sql("SELECT net_pnl FROM rounds") == [("98",)]


def test_formal_import_refuses_demo(tmp_path):
    case = Replay(tmp_path, 100)
    demo = case.folder / "demo.sqlite"
    command = [sys.executable, "-m", "trend_v3.kelly_demo", "seed", "--db", str(demo)]
    seeded = subprocess.run(
        command, capture_output=True, text=True, timeout=15, check=True
    )
    assert json.loads(seeded.stdout)["fabricated"] is True
    before = demo.read_bytes()

    case.cli("init", success=False, ledger=demo)
    case.cli("inspect", success=False, ledger=demo)
    case.cli("sync", success=False, ledger=demo)
    case.sync([opening(case)], success=False, ledger=demo)
    assert demo.read_bytes() == before
    inspector = command[:3] + ["inspect", "--db", str(demo)]
    after = subprocess.run(
        inspector, capture_output=True, text=True, timeout=15, check=True
    )
    assert json.loads(after.stdout)["totals"] == {"rounds": 7, "trade_events": 20}
