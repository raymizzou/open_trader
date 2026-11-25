"""Observe the paper-entry service through its operator commands."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from trend_v3 import paper_authority, paper_cli, paper_entry
from trend_v3.paper_futu import BrokerUnavailable, FutuPaperBroker

DAY = "2026-11-24"
DATA_DAY = "2026-11-23"
PREOPEN = datetime.fromisoformat(f"{DAY}T09:20:00-05:00")
OPEN = datetime.fromisoformat(f"{DAY}T09:30:00-05:00")


def receipt(identity, value, *, market="US", kind="stock", day=DATA_DAY, version="v1"):
    return {
        "identity": identity,
        "value": value,
        "state": "verified",
        "market": market,
        "instrument_type": kind,
        "data_date": day,
        "version": version,
        "source": "fixed-offline-provider-record",
    }


def report(*codes, short=False, product="stock", order_type="MARKET"):
    direction = "short" if short else "long"
    prev, curr, event = ("凉", "寒", "凉转寒") if short else ("温", "热", "温转热")
    members = list(codes)
    group = dict(
        name="Verified board group",
        seat=1,
        direct=[],
        board_strength=85,
        coverage={"state": "complete", "instrument_ids": members},
    )
    evidence = {}
    data = {
        "date": DAY,
        "board_date": DATA_DAY,
        "board_version": "v1",
        direction: [group],
        "collection": {
            "targets": {"BOARD": DATA_DAY, "US": DATA_DAY},
            "evidence": evidence,
            "target_evidence": {
                "US": {
                    "state": "verified",
                    "kind": "equity",
                    "report_date": DAY,
                    "basis_date": DATA_DAY,
                    "sessions": [DATA_DAY],
                    "covered_through": DATA_DAY,
                    "source": "exchange calendar",
                },
            },
        },
    }
    facts = {"sessions": {}, "instruments": {}, "report_version": "v1"}
    facts["sessions"]["US"] = {
        "state": "verified",
        "source": "exchange calendar",
        "market": "US",
        "report_date": DAY,
        "session_date": DAY,
        "data_date": DATA_DAY,
        "opens_at": OPEN.isoformat(),
        "closes_at": f"{DAY}T16:00:00-05:00",
        "timezone": "America/New_York",
        "session": "regular",
    }
    for field in ("board_date", "board_version"):
        evidence[f"/{field}"] = receipt("board", data[field], market="BOARD", kind=None)
    for field in ("coverage", "board_strength"):
        evidence[f"/{direction}/0/{field}"] = receipt(
            group["name"],
            group[field],
            market="BOARD",
            kind=None,
        )
    for index, code in enumerate(codes):
        asset = {
            "instrument_id": code,
            "code": code,
            "name": code,
            "currency": "USD",
            "market": "US",
            "instrument_type": product,
            "data_date": DATA_DAY,
            "direct_verified": True,
            "platform_match_verified": True,
            "watch_eligible": True,
            "strength": 85 - index,
            "trend": curr,
            "signal_events": [event],
        }
        group["direct"].append(asset)
        for field in ("trend", "signal_events", "strength"):
            evidence[f"/{direction}/0/direct/{index}/{field}"] = receipt(
                code,
                asset[field],
                kind=product,
            )
        facts["instruments"][code] = {
            "signal": receipt(
                code,
                {
                    "previous": prev,
                    "current": curr,
                    "labels": [event],
                    "action": "entry",
                },
                kind=product,
            ),
            "membership": receipt(
                code,
                {
                    "group": group["name"],
                    "seat": 1,
                    "direction": direction,
                    "direct": True,
                },
                kind=product,
            ),
            "route": receipt(
                code,
                {
                    "code": f"US.{code}",
                    "account": "paper-us",
                    "currency": "USD",
                    "lot": 1,
                    "multiplier": "1",
                    "reference_price": "20",
                    "order_type": order_type,
                    "environment": "SIMULATE",
                },
                kind=product,
            ),
        }
    return data, facts


class Broker:
    environment = "SIMULATE"

    def __init__(self):
        self.accounts = {
            "paper-us": {
                "currency": "USD",
                "net_value": "100000",
                "available": "100000",
                "identity": "fixture-us-account",
                "environment": "SIMULATE",
                "markets": ["US"],
                "products": ["stock", "etf"],
                "positions": [],
                "orders": [],
            }
        }
        self.calls = []
        self.orders = []
        self.borrow = 1000
        self.open = True
        self.reply = {"state": "active", "id": "order-1", "filled": 0}
        self.reconciled = None

    def snapshot(self):
        self.calls.append("snapshot")
        return deepcopy(self.accounts)

    def capacity(self, line):
        self.calls.append("capacity")
        return self.borrow if line["direction"] == "short" else 100000

    def market_open(self, line):
        self.calls.append("market_open")
        return self.open

    def place_market(self, line, quantity, token, *, deadline=None):
        self.orders.append((line["code"], line["direction"], quantity, token))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def find_order(self, line, token, order_id, quantity):
        self.calls.append("find_order")
        return self.reconciled


def session():
    return {
        "owner": "scheduled-tab-7",
        "date": DAY,
        "id": "daily-run-7",
        "kind": "scheduled",
    }


def offline_reply(event):
    return event.get("proof") == "synthetic-test-authority"


def make_service(tmp_path):
    return paper_entry.PaperEntry(
        tmp_path / "paper.sqlite",
        Broker(),
        session(),
        verify_reply=offline_reply,
    )


def prepare(service, *codes, **options):
    data, facts = report(*codes, **options)
    return service.plan(data, facts, now=PREOPEN)


def test_confirmed_long_opens_once(tmp_path):
    service = make_service(tmp_path)

    plan = prepare(service, "ABC", "ABC")
    displayed_again = prepare(service, "ABC", "ABC")

    assert plan == displayed_again
    assert len(plan["lines"]) == 1
    (line,) = plan["lines"]
    assert (line["code"], line["direction"], line["quantity"], line["currency"]) == (
        "US.ABC",
        "long",
        100,
        "USD",
    )
    assert line["opens_at"] == OPEN.isoformat()
    assert not service.broker.orders


def test_short_requires_simulated_borrow(tmp_path):
    service = make_service(tmp_path)
    allowed = prepare(service, "ABC", short=True)
    assert [(line["direction"], line["quantity"]) for line in allowed["lines"]] == [
        ("short", 100),
    ]

    for capacity in (None, 0):
        service.broker.borrow = capacity
        denied = prepare(service, "ABC", short=True)
        assert denied["lines"] == []
        assert denied["skipped"] == [
            {"code": "US.ABC", "reason": "short_capacity_unverified"}
        ]
    assert service.broker.orders == []


def approval(plan, at=PREOPEN, *, selection=None, **overrides):
    items = plan["lines"] if selection is None else selection
    return {
        "event_id": "reply-1",
        "author": "Ray",
        "owner": session()["owner"],
        "session": session()["id"],
        "decision": "approve",
        "at": at.isoformat(),
        "source": "user_reply",
        "proof": "synthetic-test-authority",
        "delivered_at": PREOPEN.isoformat(),
        "plan": plan["id"],
        "report_version": plan["report_version"],
        "items": [
            {
                "item": line["item"],
                "quantity": line["quantity"],
                "open": line["opens_at"],
            }
            for line in items
        ],
        **overrides,
    }


def test_preopen_approval_submits_market_once_at_open(tmp_path):
    service = make_service(tmp_path)
    plan = prepare(service, "ABC")
    assert service.approve(plan["id"], approval(plan), now=PREOPEN) is True

    service.submit(plan["id"], now=OPEN - timedelta(seconds=1))
    assert service.broker.orders == []
    outcome = service.submit(plan["id"], now=OPEN)
    service.submit(plan["id"], now=OPEN)

    assert outcome[0]["state"] == "active"
    assert [row[:3] for row in service.broker.orders] == [("US.ABC", "long", 100)]


def test_no_open_time_signal_refresh(tmp_path):
    service = make_service(tmp_path)
    data, facts = report("ABC")
    plan = service.plan(data, facts, now=PREOPEN)
    assert service.approve(plan["id"], approval(plan), now=PREOPEN)

    # Reconstruct from only the saved plan, session identity, and execution broker.
    resumed = paper_entry.PaperEntry(
        service.database, service.broker, session(), verify_reply=offline_reply
    )
    data.clear()
    facts.clear()
    assert resumed.submit(plan["id"], now=OPEN)[0]["state"] == "active"
    assert len(service.broker.orders) == 1


def test_late_plan_or_approval_does_not_submit(tmp_path):
    service = make_service(tmp_path)
    data, facts = report("ABC")
    at = OPEN + timedelta(minutes=1)
    late = service.plan(data, facts, now=at)
    assert late["lines"] == []
    assert late["skipped"] == [{"code": "US.ABC", "reason": "missed_open"}]
    assert service.broker.calls == []

    other_path = tmp_path / "other"
    other_path.mkdir()
    other = make_service(other_path)
    early = prepare(other, "ABC")
    assert not other.approve(early["id"], approval(early, at), now=at)
    assert other.approve(early["id"], approval(early), now=PREOPEN)
    assert other.submit(early["id"], now=at)[0]["reason"] == "missed_open"
    assert (
        other.submit(early["id"], now=OPEN + timedelta(days=1))[0]["state"] == "skipped"
    )
    assert not other.broker.orders


def test_limit_only_route_reported_preopen(tmp_path):
    service = make_service(tmp_path)
    plan = prepare(service, "ABC", order_type="NORMAL")

    assert plan["lines"] == []
    assert plan["skipped"] == [{"code": "ABC", "reason": "market_order_unsupported"}]
    assert service.submit(plan["id"], now=OPEN) == []
    assert service.broker.calls == []
    assert service.broker.orders == []


def retry_reply(plan, status, when, quantity):
    (line,) = plan["lines"]
    return approval(
        plan,
        when,
        decision="retry",
        event_id="retry-1",
        items=[
            {
                "item": line["item"],
                "quantity": quantity,
                "open": line["opens_at"],
                "after_attempt": status["attempt"],
            }
        ],
    )


def test_reject_requires_explicit_retry(tmp_path):
    service = make_service(tmp_path)
    broker = service.broker
    plan = prepare(service, "ABC")
    assert service.approve(plan["id"], approval(plan), now=PREOPEN)
    broker.reply = {
        "state": "rejected",
        "id": "rejected-1",
        "filled": 0,
        "reason": "broker_rejected",
    }
    (rejected,) = service.submit(plan["id"], now=OPEN)
    assert rejected["state"] == "rejected"
    assert rejected["remaining"] == 100

    now = OPEN + timedelta(minutes=1)
    assert service.submit(plan["id"], now=now) == [rejected]
    reply = retry_reply(plan, rejected, now, 100)
    assert not service.approve(plan["id"], reply, now=now)
    broker.reconciled = broker.reply
    assert service.reconcile(plan["id"], now=now)[0]["state"] == "rejected"
    now += timedelta(seconds=1)
    assert service.approve(plan["id"], retry_reply(plan, rejected, now, 100), now=now)
    broker.reply = {"state": "active", "id": "new-1", "filled": 0}

    assert service.submit(plan["id"], now=now, retry=True)[0]["state"] == "active"
    service.submit(plan["id"], now=now, retry=True)
    assert [row[2] for row in broker.orders] == [100, 100]
    assert broker.orders[0][3] != broker.orders[1][3]


def test_partial_or_unknown_requires_reconciliation(tmp_path):
    partial_path = tmp_path / "partial"
    partial_path.mkdir()
    partial = make_service(partial_path)
    plan = prepare(partial, "ABC")
    assert partial.approve(plan["id"], approval(plan), now=PREOPEN)
    partial.broker.reply = {"state": "active", "id": "partial-1", "filled": 40}
    (observed,) = partial.submit(plan["id"], now=OPEN)
    assert (observed["filled"], observed["remaining"], observed["active"]) == (
        40,
        60,
        True,
    )
    now = OPEN + timedelta(seconds=4)
    assert not partial.approve(
        plan["id"], retry_reply(plan, observed, now, 60), now=now
    )
    partial.broker.reconciled = {"state": "cancelled", "id": "partial-1", "filled": 40}
    (terminal,) = partial.reconcile(plan["id"], now=now)
    assert (terminal["filled"], terminal["remaining"], terminal["active"]) == (
        40,
        60,
        False,
    )
    now += timedelta(seconds=1)
    assert not partial.approve(
        plan["id"], retry_reply(plan, terminal, now, 100), now=now
    )
    assert partial.approve(plan["id"], retry_reply(plan, terminal, now, 60), now=now)
    partial.broker.reply = {"state": "active", "id": "partial-2", "filled": 0}
    assert partial.submit(plan["id"], now=now, retry=True)[0]["filled"] == 40
    assert [row[2] for row in partial.broker.orders] == [100, 60]

    unknown = make_service(tmp_path)
    plan = prepare(unknown, "XYZ")
    assert unknown.approve(plan["id"], approval(plan), now=PREOPEN)
    unknown.broker.reply = TimeoutError()
    (observed,) = unknown.submit(plan["id"], now=OPEN)
    assert (observed["state"], observed["reason"], observed["active"]) == (
        "unknown",
        "broker_timeout",
        None,
    )
    assert unknown.reconcile(plan["id"], now=now) == [observed]
    assert not unknown.approve(
        plan["id"], retry_reply(plan, observed, now, 100), now=now
    )
    unknown.broker.reconciled = {"state": "filled", "id": "lost-1", "filled": 100}
    (terminal,) = unknown.reconcile(plan["id"], now=now)
    assert (terminal["state"], terminal["filled"], terminal["remaining"]) == (
        "filled",
        100,
        0,
    )
    now += timedelta(seconds=1)
    assert not unknown.approve(
        plan["id"], retry_reply(plan, terminal, now, 100), now=now
    )
    unknown.submit(plan["id"], now=now, retry=True)
    assert len(unknown.broker.orders) == 1


def test_repeat_and_unknown_do_not_resubmit(tmp_path):
    entered, release, retry_entered = Event(), Event(), Event()

    class InterruptedBroker(Broker):
        def place_market(self, line, quantity, token, *, deadline=None):
            self.orders.append((line["code"], line["direction"], quantity, token))
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test watchdog interrupted the broker")
            raise SystemExit("simulated death after write and before durable receipt")

    broker = InterruptedBroker()
    service = paper_entry.PaperEntry(
        tmp_path / "paper.sqlite", broker, session(), verify_reply=offline_reply
    )
    plan = prepare(service, "ABC")
    assert service.approve(plan["id"], approval(plan), now=PREOPEN)

    def repeat():
        restarted = paper_entry.PaperEntry(
            service.database, broker, session(), verify_reply=offline_reply
        )
        retry_entered.set()
        return restarted.submit(plan["id"], now=OPEN)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.submit, plan["id"], now=OPEN)
        try:
            assert entered.wait(timeout=5)
            second = pool.submit(repeat)
            assert retry_entered.wait(timeout=5)
        finally:
            release.set()
        with pytest.raises(SystemExit, match="simulated death"):
            first.result(timeout=5)
        (status,) = second.result(timeout=5)

    assert status["state"] == "unknown"
    assert status["active"] is None
    assert len(broker.orders) == 1

    revised_time = OPEN + timedelta(seconds=20)
    data, facts = report("ABC")
    facts["report_version"] = "v2"
    facts["sessions"]["US"]["opens_at"] = revised_time.isoformat()
    revised_at = OPEN + timedelta(seconds=5)
    revised = service.plan(data, facts, now=revised_at)
    assert revised["id"] != plan["id"]
    assert revised["lines"] == []
    assert revised["skipped"] == [{"code": "US.ABC", "reason": "instrument_busy"}]
    assert service.submit(revised["id"], now=revised_time) == []
    assert len(broker.orders) == 1


def footprint(code, value, *, currency="USD", quantity=1):
    return {
        "code": f"US.{code}",
        "currency": currency,
        "notional": str(value),
        "quantity": quantity,
    }


def test_notional_lot_and_portfolio_limits(tmp_path):
    service = make_service(tmp_path)
    broker = service.broker
    data, facts = report("ABC")
    route = facts["instruments"]["ABC"]["route"]["value"]
    route.update(lot=10, reference_price="30")
    broker.accounts["paper-us"]["available"] = "1250"
    plan = service.plan(data, facts, now=PREOPEN)
    assert [(line["quantity"], line["notional"]) for line in plan["lines"]] == [
        (40, "1200")
    ]

    account = broker.accounts["paper-us"]
    account["positions"] = [footprint(f"POS{i}", 100) for i in range(9)]
    account["orders"] = [footprint("OPEN", 100)]
    assert service.plan(data, facts, now=PREOPEN)["skipped"] == [
        {"code": "US.ABC", "reason": "portfolio_slots_full"},
    ]

    account["positions"] = [footprint("POS", 99500)]
    account["orders"] = []
    plan = service.plan(data, facts, now=PREOPEN)
    assert [(line["quantity"], line["notional"]) for line in plan["lines"]] == [
        (10, "300")
    ]

    broker.accounts["paper-hk"] = {
        "currency": "HKD",
        "net_value": "80000",
        "available": "80000",
        "identity": "fixture-hk-account",
        "environment": "SIMULATE",
        "markets": ["HK"],
        "products": ["stock"],
        "positions": [],
        "orders": [],
    }
    assert service.plan(data, facts, now=PREOPEN)["lines"][0]["quantity"] == 10
    account["positions"] = [footprint("POS", 99500, currency="HKD")]
    assert service.plan(data, facts, now=PREOPEN)["skipped"] == [
        {"code": "US.ABC", "reason": "portfolio_currency_unverified"},
    ]

    del broker.accounts["paper-hk"]
    account["positions"] = [footprint(f"POS{i}", 100) for i in range(9)]
    data, facts = report("ABC", "XYZ")
    plan = service.plan(data, facts, now=PREOPEN)
    assert [line["code"] for line in plan["lines"]] == ["US.ABC"]
    assert plan["skipped"] == [{"code": "US.XYZ", "reason": "portfolio_slots_full"}]
    assert service.approve(plan["id"], approval(plan), now=PREOPEN)
    broker.reply = TimeoutError()
    assert service.submit(plan["id"], now=OPEN)[0]["state"] == "unknown"
    later = OPEN + timedelta(seconds=4)
    facts["report_version"] = "v2"
    facts["sessions"]["US"]["opens_at"] = (OPEN + timedelta(seconds=20)).isoformat()
    plan = service.plan(data, facts, now=later)
    assert plan["lines"] == []
    assert plan["skipped"][-1] == {"code": "US.XYZ", "reason": "portfolio_slots_full"}
    assert len(broker.orders) == 1


def test_only_entry_actions_submit(tmp_path):
    service = make_service(tmp_path)
    for action in ("watch", "exit", "add", "UNKNOWN"):
        data, facts = report("ABC")
        facts["instruments"]["ABC"]["signal"]["value"]["action"] = action
        assert not service.plan(data, facts, now=PREOPEN)["lines"]
    data, facts = report("ABC")
    asset = data["long"][0]["direct"][0]
    asset["data_date"] = "2026-11-22"
    assert not service.plan(data, facts, now=PREOPEN)["lines"]
    data, facts = report("ABC")
    data["collection"]["evidence"]["/long/0/direct/0/signal_events"]["identity"] = (
        "OTHER"
    )
    assert not service.plan(data, facts, now=PREOPEN)["lines"]

    data, facts = report("ABC")
    member = facts["instruments"]["ABC"]["membership"]["value"]
    member["direct"] = (
        1  # A numerically equal input is not an affirmative membership receipt.
    )
    assert not service.plan(data, facts, now=PREOPEN)["lines"]

    data, facts = report("ABC", product="option")
    assert not service.plan(data, facts, now=PREOPEN)["lines"]
    data, facts = report("ABC", product="etf")
    assert not service.plan(data, facts, now=PREOPEN)["lines"]
    facts["instruments"]["ABC"]["route"]["value"]["leverage"] = 1
    assert service.plan(data, facts, now=PREOPEN)["lines"][0]["code"] == "US.ABC"
    assert service.broker.orders == []


def test_submit_requires_current_plan_approval(tmp_path):
    service = make_service(tmp_path)
    plan = prepare(service, "ABC")
    service.broker.calls.clear()
    assert service.submit(plan["id"], now=OPEN)[0]["reason"] == "not_approved"
    for invalid in (
        {"owner": "another-tab"},
        {"session": "older-run"},
        {"author": "report-agent"},
        {"source": "report"},
        {"source": "smoke"},
        {"report_version": "previous-version"},
        {"decision": "yes"},
        {"proof": None},
    ):
        assert not service.approve(plan["id"], approval(plan, **invalid), now=PREOPEN)
    assert not service.approve(plan["id"], "yes", now=PREOPEN)
    forged = approval(plan)
    forged["items"][0]["quantity"] = 101
    assert not service.approve(plan["id"], forged, now=PREOPEN)
    assert service.submit(plan["id"], now=OPEN)[0]["reason"] == "not_approved"
    assert service.broker.calls == []
    assert service.broker.orders == []


def test_subset_approval_only_submits_selected(tmp_path):
    service = make_service(tmp_path)
    plan = prepare(service, "ABC", "XYZ")
    assert service.approve(
        plan["id"],
        approval(plan, selection=plan["lines"][:1]),
        now=PREOPEN,
    )
    outcomes = service.submit(plan["id"], now=OPEN)

    assert [(row["code"], row["state"], row["reason"]) for row in outcomes] == [
        ("US.ABC", "active", None),
        ("US.XYZ", "skipped", "not_approved"),
    ]
    assert [row[:3] for row in service.broker.orders] == [("US.ABC", "long", 100)]


def test_expired_or_superseded_approval_skips(tmp_path):
    service = make_service(tmp_path)
    original_data, original_facts = report("ABC")
    original_facts["report_revision"] = 1
    original = service.plan(original_data, original_facts, now=PREOPEN)
    assert service.approve(original["id"], approval(original), now=PREOPEN)
    changed_data, changed_facts = report("ABC")
    changed_facts.update(report_version="v2", report_revision=2)
    changed_facts["instruments"]["ABC"]["signal"]["state"] = "withdrawn"
    changed_at = PREOPEN + timedelta(minutes=1)
    withdrawn = service.plan(changed_data, changed_facts, now=changed_at)
    assert withdrawn["lines"] == []

    replay_at = PREOPEN + timedelta(minutes=2)
    with pytest.raises(ValueError, match="older report"):
        service.plan(original_data, original_facts, now=replay_at)
    (old_outcome,) = service.submit(original["id"], now=OPEN)
    assert old_outcome["reason"] == "plan_superseded"
    assert service.submit(withdrawn["id"], now=OPEN) == []
    assert service.broker.orders == []


def test_future_requires_verified_month(tmp_path):
    service = make_service(tmp_path)
    service.broker.accounts["paper-us"]["products"] = ["future"]
    data, facts = report("MESmain", product="future")
    route = facts["instruments"]["MESmain"]["route"]["value"]
    route.update(multiplier="5", reference_price="195")
    assert not service.plan(data, facts, now=PREOPEN)["lines"]
    assert service.broker.calls == []

    route.update(
        code="US.MES2612",
        contract_root="MES",
        delivery_month="2026-12",
        underlying_id="MESmain",
        tradable_contract_verified=True,
    )
    plan = service.plan(data, facts, now=PREOPEN)
    assert [(x["code"], x["quantity"], x["notional"]) for x in plan["lines"]] == [
        ("US.MES2612", 2, "1950"),
    ]
    assert service.approve(plan["id"], approval(plan), now=PREOPEN)
    assert service.submit(plan["id"], now=OPEN)[0]["state"] == "active"
    assert [item[:3] for item in service.broker.orders] == [("US.MES2612", "long", 2)]


def test_crypto_requires_simulated_route(tmp_path):
    service = make_service(tmp_path)
    data, facts = report("BTCUSD", product="crypto", order_type="NORMAL")
    assert service.plan(data, facts, now=PREOPEN)["skipped"] == [
        {"code": "BTCUSD", "reason": "market_order_unsupported"},
    ]
    assert service.broker.calls == []
    route = facts["instruments"]["BTCUSD"]["route"]["value"]
    route.update(order_type="MARKET", spot_verified=True)
    plan = service.plan(data, facts, now=PREOPEN)
    assert plan["lines"] == []
    assert plan["skipped"] == [{"code": "BTCUSD", "reason": "crypto_route_unverified"}]
    assert service.submit(plan["id"], now=OPEN) == []
    assert not service.broker.orders


def native_selection(data):
    records, instruments = {}, []
    for group in data["long"]:
        for asset in group["direct"]:
            code = asset["code"]
            fields = ("trendTemperaturePrev", "trendTemperatureCurr", "tickerLabels")
            records[code] = {
                "identity": {
                    "tmId": asset["instrument_id"],
                    "asset": "美国股票",
                    "tickerSymbol": code,
                    "asOfDate": DATA_DAY,
                },
                "fields": list(fields),
                "source": "fixed-offline-acquisition.json",
                "location": "records[0]",
                "sha256": "1" * 64,
            }
            instruments.append(
                {
                    "id": asset["instrument_id"],
                    "code": code,
                    "group": group["name"],
                    "market": "美国股票",
                    "selected_new_position": True,
                    "state": "已触发",
                    "relationship": "直属",
                    "current": "热",
                    "previous": "温",
                    "labels": "温转热",
                    "data_day": DATA_DAY,
                    "expected_day": DATA_DAY,
                    "field_refs": dict.fromkeys(fields, code),
                }
            )
    return {"report_date": DAY, "instruments": instruments, "field_records": records}


def test_native_selection_reference_must_match(tmp_path):
    data, facts = report("ABC")
    facts["selection"] = native_selection(data)
    service = make_service(tmp_path)
    assert service.plan(data, facts, now=PREOPEN)["lines"][0]["quantity"] == 100

    reference = facts["selection"]["field_records"]["ABC"]
    reference["identity"]["tickerSymbol"] = "WRONG"
    plan = service.plan(data, facts, now=PREOPEN)
    assert plan["lines"] == []
    assert plan["skipped"] == [{"code": "ABC", "reason": "signal_unverified"}]
    assert service.broker.orders == []


def test_open_revalidates_account_identity_and_does_not_catch_up(tmp_path):
    service = make_service(tmp_path)
    plan = prepare(service, "ABC")
    assert service.approve(plan["id"], approval(plan), now=PREOPEN)
    account = service.broker.accounts["paper-us"]
    account["identity"] = "another-sim-account"
    (outcome,) = service.submit(plan["id"], now=OPEN)
    assert outcome["state"] == "skipped"
    account["identity"] = "fixture-us-account"
    assert service.submit(plan["id"], now=OPEN + timedelta(seconds=1)) == [outcome]
    assert service.broker.orders == []


class OpenD:
    def __init__(self):
        self.calls = []
        self.writes = []
        self.history = []
        self.environment = "SIMULATE"
        self.status = "ACTIVE"
        self.markets = ["US"]

    def OpenSecTradeContext(self, *, filter_trdmarket, **address):
        assert filter_trdmarket in {"US", "HK", "CN", "N/A"}
        return self

    def OpenQuoteContext(self, **address):
        return self

    def get_acc_list(self):
        return 0, [
            {
                "acc_id": 42,
                "trd_env": self.environment,
                "acc_status": self.status,
                "trdmarket_auth": self.markets,
                "sim_acc_type": "STOCK",
                "acc_type": "MARGIN",
            }
        ]

    def query(self, kind, params, rows):
        assert params.get("trd_env") == "SIMULATE"
        assert params.get("acc_id") == 42
        self.calls.append((kind, params))
        return 0, rows

    def accinfo_query(self, **params):
        return self.query(
            "funds",
            params,
            [
                {
                    "currency": "USD",
                    "total_assets": "100000",
                    "available_funds": "100000",
                }
            ],
        )

    def position_list_query(self, **params):
        return self.query("positions", params, [])

    def order_list_query(self, **params):
        return self.query("orders", params, [])

    def history_order_list_query(self, **params):
        return self.query("history", params, self.history)

    def acctradinginfo_query(self, **params):
        return self.query(
            "capacity", params, [{"max_cash_buy": 1000, "max_sell_short": 100}]
        )

    def get_market_state(self, *, code_list):
        return 0, [{"code": code_list[0], "market_state": "MORNING"}]

    def place_order(self, **params):
        self.writes.append(params)
        return self.query(
            "write",
            params,
            [
                {
                    **params,
                    "order_id": "paper-a",
                    "order_status": "SUBMITTED",
                    "dealt_qty": 0,
                }
            ],
        )

    def close(self):
        pass


def private_input(directory, name, data):
    path = directory / name
    path.write_text(paper_entry.encode(data), encoding="utf-8")
    path.chmod(0o600)
    return path


def paper_settings():
    return {
        "environment": "SIMULATE",
        "release": "3" * 40,
        "port": 11111,
        "accounts": {
            "paper-us": {
                "id": 42,
                "category": "securities",
                "trade_market": "US",
                "currency": "USD",
            }
        },
    }


def test_futu_lost_ack_reconciles_only_matching_simulate_order(tmp_path):
    class LostAck(OpenD):
        def place_order(self, **params):
            super().place_order(**params)
            return -1, "timeout"

    opend = LostAck()
    broker = FutuPaperBroker(paper_settings(), sdk=opend)
    service = paper_entry.PaperEntry(
        tmp_path / "state.sqlite3",
        broker,
        session(),
        verify_reply=offline_reply,
    )
    opend.environment = "REAL"
    assert prepare(service, "ABC")["lines"] == []
    assert opend.calls == []
    opend.environment = "SIMULATE"
    opend.markets = ["HK"]
    assert prepare(service, "ABC")["lines"] == []
    assert opend.calls == []

    opend.markets = ["US"]
    plan = prepare(service, "ABC")
    assert service.approve(plan["id"], approval(plan), now=PREOPEN)
    (result,) = service.submit(plan["id"], now=OPEN)
    assert result["state"] == "unknown"
    (write,) = opend.writes
    opend.history = [
        {
            **write,
            "order_id": "unrelated",
            "order_status": "FILLED_ALL",
            "remark": "different-attempt",
            "dealt_qty": 100,
        }
    ]
    later = OPEN + timedelta(seconds=4)
    assert service.reconcile(plan["id"], now=later)[0]["state"] == "unknown"
    matched = opend.history[0] | {"remark": write["remark"], "order_id": "matched"}
    opend.history.append(matched)
    assert service.reconcile(plan["id"], now=later)[0]["state"] == "filled"
    service.submit(plan["id"], now=later, retry=True)
    assert len(opend.writes) == 1


def test_daily_cli_execution_requires_original_scheduled_session(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(paper_cli.Path, "home", staticmethod(lambda: tmp_path))

    def no_broker(*args, **kwargs):
        pytest.fail("broker opened without an original scheduled session")

    monkeypatch.setattr(paper_cli, "FutuPaperBroker", no_broker)
    with pytest.raises(SystemExit) as stopped:
        paper_cli.main(["--session", "other-session", "submit", "plan-id"])
    assert stopped.value.code == 2
    assert "original scheduled Codex session is unavailable" in capsys.readouterr().err


NATIVE_SESSION = "00000000-0000-0000-0000-000000000001"
NATIVE_LAUNCH = "2" * 32
RELEASE = "3" * 40


class PlanningOpenD(OpenD):
    def __init__(self, *, market="US", day_type="WHOLE", lot=1, snapshot_offset=0):
        super().__init__()
        self.markets = [market]
        self.denomination = "HKD" if market == "HK" else "USD"
        self.day_type = day_type
        self.lot = lot
        self.snapshot_offset = snapshot_offset
        self.quote_calls = []
        self.basic_suspension = False
        self.snapshot_suspension = False
        self.security_status = "NORMAL"

    def request_trading_days(self, **params):
        self.quote_calls.append(("calendar", params))
        rows = [{"time": params["start"], "trade_date_type": self.day_type}]
        return 0, rows if self.day_type else []

    def get_stock_basicinfo(self, **params):
        self.quote_calls.append(("basic", params))
        return 0, [
            {
                "code": params["code_list"][0],
                "stock_type": params["stock_type"],
                "lot_size": self.lot,
                "suspension": self.basic_suspension,
                "delisting": False,
            }
        ]

    def get_market_snapshot(self, **params):
        self.quote_calls.append(("quote", params))
        return 0, [
            {
                "code": params["code_list"][0],
                "update_time": f"{DATA_DAY} 16:00:00",
                "last_price": "20",
                "lot_size": self.lot + self.snapshot_offset,
                "suspension": self.snapshot_suspension,
                "sec_status": self.security_status,
            }
        ]

    def accinfo_query(self, **params):
        return self.query(
            "funds",
            params,
            [
                {
                    "currency": self.denomination,
                    "total_assets": "100000",
                    "available_funds": "100000",
                }
            ],
        )


def replace_market(data, market, denomination):
    (group,) = data["long"]
    for asset in group["direct"]:
        asset["market"] = market
        asset["currency"] = denomination
    collection = data["collection"]
    for kind in ("targets", "target_evidence"):
        collection[kind][market] = collection[kind].pop("US")
    for proof in collection["evidence"].values():
        if proof["market"] == "US":
            proof["market"] = market


def daily_files(home, data, *, supplemental=None):
    root = home / ".local/share/trend-animals"
    index = root / "reports/_evidence" / DAY
    revision = index / "v02"
    revision.mkdir(parents=True)
    selected = native_selection(data)
    fields = ["trendTemperaturePrev", "trendTemperatureCurr", "tickerLabels"]
    source = f"{DAY}/snapshot.json"
    raw = []
    for i, row in enumerate(selected["instruments"]):
        asset = next(
            asset for asset in data["long"][0]["direct"] if asset["code"] == row["code"]
        )
        if asset["market"] == "HK":
            row["market"] = "香港股票"
        if asset["instrument_type"] == "future":
            row["market"] = "香港股指期货" if asset["market"] == "HK" else "美国期货"
        if asset["market"] == "CRYPTO":
            row["market"] = "加密现货"
        additional = (supplemental or {}).get(row["code"], {})
        row["field_refs"].update(dict.fromkeys(additional, row["code"]))
        identity = {
            "tmId": row["id"],
            "asset": row["market"],
            "tickerSymbol": row["code"],
            "asOfDate": row["data_day"],
        }
        raw.append(
            {
                **identity,
                "trendTemperaturePrev": row["previous"],
                "trendTemperatureCurr": row["current"],
                "tickerLabels": row["labels"],
                **additional,
            }
        )
        selected["field_records"][row["code"]].update(
            identity=identity,
            source=source,
            location=f"$.data[{i}]",
            fields=fields + list(additional),
        )
    upstream = private_input(index, "snapshot.json", {"data": raw})
    raw_hash = sha256(upstream.read_bytes()).hexdigest()
    for record in selected["field_records"].values():
        record["sha256"] = raw_hash
    html = root / "reports" / f"{DAY}.html"
    html.write_text(
        "<html><title>Fixed current daily fixture</title></html>", encoding="utf-8"
    )
    html.chmod(0o600)
    verification = {
        "report_date": DAY,
        "revision": "v02",
        "status": "reader_verified",
        "source": {"commit": RELEASE},
        "cutoff": PREOPEN.isoformat(),
        "report_sha256": sha256(html.read_bytes()).hexdigest(),
        "checks": dict.fromkeys(
            ("template", "privacy", "allowlist", "evidence_consistency"), True
        ),
    }
    for field, name, body in (
        ("projection_sha256", "reader-projection.json", data),
        ("selection_sha256", "selection-evidence.json", selected),
        ("fees_sha256", "fees.json", {}),
    ):
        path = private_input(revision, name, body)
        verification[field] = sha256(path.read_bytes()).hexdigest()
    private_input(revision, "report-verification.json", verification)
    private_input(
        index, "latest-revision.json", {"report_date": DAY, "revision": "v02"}
    )

    state = home / ".config/trend-animals/daily-state"
    state.mkdir(parents=True)
    prompt = (
        f"Initialize the scheduled daily session for {DAY} (Asia/Taipei). "
        f"Original immutable host launch ID: {NATIVE_LAUNCH}."
    )
    command = ["codex", "--no-daemon", "--cd", str(root), prompt]
    launch = {
        "day": DAY,
        "name": f"trend507-{DAY}",
        "tab": "official-tab",
        "terminal": "t1",
        "space": "official-space",
        "home": str(home),
        "root": str(root),
        "launch": NATIVE_LAUNCH,
        "created_ms": 123,
        "session": None,
        "argv": command,
    }
    private_input(state, f"{DAY}.launch.json", launch)
    private_input(
        state,
        f"{DAY}.upload.json",
        {
            "day": DAY,
            "launch": NATIVE_LAUNCH,
            "revision": "v02",
            "uploaded": True,
            "sha256": verification["report_sha256"],
        },
    )
    transcripts = home / ".codex/sessions"
    transcripts.mkdir(parents=True)
    transcript = transcripts / f"rollout-{NATIVE_SESSION}.jsonl"
    transcript.write_text(
        "\n".join(
            map(
                paper_entry.encode,
                (
                    {
                        "type": "session_meta",
                        "payload": {"id": NATIVE_SESSION, "cwd": str(root)},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "role": "user",
                            "content": [{"text": "# AGENTS.md instructions"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "role": "user",
                            "content": [{"text": prompt}],
                        },
                    },
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    transcript.chmod(0o600)
    live = {
        "state": "live",
        "cwd": str(root),
        "created_ms": 123,
        "argv": command,
        "labels": {"herdr.tab": "official-tab", "herdr.space": "official-space"},
        "status": {"app": "codex", "state": "working"},
        "foreground": {"pid": os.getpid(), "name": "codex", "cwd": str(root)},
    }
    return root, live, upstream


def run_daily_plan(home, broker, now, live, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    release_dir = home / ".config/trend-animals/skill-releases" / RELEASE
    monkeypatch.setattr(
        paper_authority,
        "__file__",
        str(release_dir / "services/trend_v3/src/trend_v3/paper_authority.py"),
    )
    monkeypatch.setattr(
        paper_authority, "native_terminal", lambda _home, _terminal: live
    )
    monkeypatch.setattr(paper_cli, "utc_now", lambda: now)
    monkeypatch.setattr(
        paper_cli,
        "FutuPaperBroker",
        lambda config, **options: FutuPaperBroker(config, sdk=broker, **options),
    )
    return paper_cli.main(["--session", NATIVE_SESSION, "plan"])


def test_daily_cli_plans_from_host_receipt_and_retained_sources(
    tmp_path, monkeypatch, capsys
):
    data, _ = report("ABC")
    root, live, upstream = daily_files(tmp_path, data)
    opend = PlanningOpenD()
    assert run_daily_plan(tmp_path, opend, PREOPEN, live, monkeypatch) == 0
    planned = json.loads(capsys.readouterr().out)
    (line,) = planned["lines"]
    actual = (line["code"], line["quantity"], line["order_type"], line["opens_at"])
    assert actual == ("US.ABC", 100, "MARKET", OPEN.isoformat())
    assert planned["report_version"] == "v02"
    assert planned["execution"] == "awaiting_scheduled_tab_approval"
    assert "account" not in line
    assert opend.writes == []
    assert (root / "reports/_evidence/.paper-entry/state.sqlite3").is_file()

    opend.quote_calls.clear()
    opend.calls.clear()
    upstream.write_bytes(upstream.read_bytes().replace(b"ABC", b"OTHER"))
    assert run_daily_plan(tmp_path, opend, PREOPEN, live, monkeypatch) == 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["lines"] == []
    assert rejected["skipped"] == [{"code": "ABC", "reason": "raw_signal_unverified"}]
    assert opend.quote_calls == opend.calls == opend.writes == []

    live["labels"]["herdr.tab"] = "other-tab"
    with pytest.raises(SystemExit) as stopped:
        run_daily_plan(tmp_path, opend, PREOPEN, live, monkeypatch)
    assert stopped.value.code == 2
    assert "original launch prompt is unverified" in capsys.readouterr().err
    assert opend.quote_calls == []


@pytest.mark.parametrize(
    ("day_type", "code", "reason"),
    [
        ("MORNING", "HK.00005", None),
        (None, None, "market_no_session"),
    ],
)
def test_daily_cli_hk_calendar_and_round_lot(
    tmp_path,
    monkeypatch,
    capsys,
    day_type,
    code,
    reason,
):
    data, _ = report("00005")
    replace_market(data, "HK", "HKD")
    _, live, _ = daily_files(tmp_path, data)
    opend = PlanningOpenD(market="HK", day_type=day_type, lot=100)
    at = datetime.fromisoformat(f"{DAY}T09:20:00+08:00")
    assert run_daily_plan(tmp_path, opend, at, live, monkeypatch) == 0
    plan = json.loads(capsys.readouterr().out)
    assert opend.writes == []
    if code is None:
        assert plan["lines"] == []
        assert plan["skipped"] == [{"code": "00005", "reason": reason}]
        assert [method for method, _ in opend.quote_calls] == ["calendar"]
    else:
        (line,) = plan["lines"]
        assert (line["code"], line["quantity"], line["currency"], line["opens_at"]) == (
            code,
            100,
            "HKD",
            f"{DAY}T09:30:00+08:00",
        )


def test_daily_cli_derives_verified_futures_multiplier_from_opend(
    tmp_path, monkeypatch, capsys
):
    data, _ = report("MESmain", product="future")
    metadata = {
        "code": "US.MES2612",
        "root": "MES",
        "delivery_month": "2026-12",
        "underlying": "MESmain",
        "as_of": DAY,
    }
    _, live, _ = daily_files(
        tmp_path,
        data,
        supplemental={
            "MESmain": {"brokerContract": metadata},
        },
    )

    class FutureAccount(PlanningOpenD):
        def get_acc_list(self):
            return 0, [
                {
                    "acc_id": 42,
                    "trd_env": "SIMULATE",
                    "acc_status": "ACTIVE",
                    "trdmarket_auth": ["FUTURES_SIMULATE_US"],
                    "sim_acc_type": "FUTURES",
                }
            ]

        def acctradinginfo_query(self, **params):
            return self.query("capacity", params, [{"max_cash_and_margin_buy": 6}])

    class FutureOpenD(PlanningOpenD):
        futures = FutureAccount()

        def get_acc_list(self):
            return 0, []

        def OpenFutureTradeContext(self, **address):
            return self.futures

        def get_stock_basicinfo(self, **params):
            status, rows = super().get_stock_basicinfo(**params)
            rows[0]["main_contract"] = False
            return status, rows

        def get_future_info(self, **params):
            return 0, [
                {
                    "code": params["code_list"][0],
                    "owner": "MESmain",
                    "size": "5",
                    "price_currency": "USD",
                    "trade_time": "09:30-16:00",
                    "time_zone": "America/New_York",
                    "last_trade_time": "2026-12-18 09:30:00",
                }
            ]

    broker = FutureOpenD()
    assert run_daily_plan(tmp_path, broker, PREOPEN, live, monkeypatch) == 0
    plan = json.loads(capsys.readouterr().out)
    (line,) = plan["lines"]
    assert (line["code"], line["quantity"], line["notional"]) == (
        "US.MES2612",
        6,
        "600",
    )
    assert broker.writes == broker.futures.writes == []


def test_daily_cli_flags_unverified_crypto_route_without_broker_access(
    tmp_path,
    monkeypatch,
    capsys,
):
    data, _ = report("BTC", product="spot")
    replace_market(data, "CRYPTO", "USD")
    _, live, _ = daily_files(tmp_path, data)
    broker = PlanningOpenD()
    assert run_daily_plan(tmp_path, broker, PREOPEN, live, monkeypatch) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["lines"] == []
    assert plan["skipped"] == [{"code": "BTC", "reason": "crypto_route_unverified"}]
    assert broker.quote_calls == broker.calls == broker.writes == []


def test_daily_cli_plan_rehearses_subset_and_lost_ack_offline(
    tmp_path, monkeypatch, capsys
):
    data, _ = report("ABC", "XYZ")
    _, live, _ = daily_files(tmp_path, data)

    class LostAck(PlanningOpenD):
        def place_order(self, **params):
            super().place_order(**params)
            return -1, "lost synthetic acknowledgement"

    sdk = LostAck()
    clock, invoke = tab_cli(tmp_path, monkeypatch, sdk, live)
    plan = json.loads(capsys.readouterr().out)
    assert [line["code"] for line in plan["lines"]] == ["US.ABC", "US.XYZ"]
    item = plan["lines"][0]["item"]
    clock.at += timedelta(seconds=1)
    show_plan(tmp_path, plan, clock.at)
    clock.at += timedelta(seconds=1)
    tab_record(tmp_path, "user", f"模拟批准 {plan['id']} {item}", clock.at)
    assert invoke("approve", plan["id"]) == 0
    capsys.readouterr()
    assert sdk.writes == []
    assert invoke("submit", plan["id"]) == 0
    submitted, _ = map(json.loads, capsys.readouterr().out.splitlines())
    assert [(row["code"], row["state"]) for row in submitted["results"]] == [
        ("US.ABC", "unknown"),
        ("US.XYZ", "skipped"),
    ]
    assert invoke("submit", plan["id"]) == 0
    capsys.readouterr()
    (placed,) = sdk.writes
    assert (placed["code"], placed["qty"], placed["trd_env"]) == (
        "US.ABC",
        100,
        "SIMULATE",
    )
    sdk.history = [
        placed
        | {
            "order_id": "synthetic-receipt",
            "order_status": "FILLED_ALL",
            "dealt_qty": 100,
        }
    ]
    clock.at += timedelta(seconds=10)
    assert invoke("reconcile", plan["id"]) == 0
    (confirmed,) = json.loads(capsys.readouterr().out)["results"]
    assert (confirmed["state"], confirmed["filled"], confirmed["remaining"]) == (
        "filled",
        100,
        0,
    )
    assert invoke("submit", plan["id"], "--retry-item", item) == 2
    assert len(sdk.writes) == 1


@pytest.mark.parametrize(
    "variant",
    [
        "host-modes",
        "group-writable",
        "other-writable",
        "writable-directory",
        "symlink",
        "wrong-launch",
        "duplicate",
        "readable-receipt",
    ],
)
def test_daily_cli_accepts_owner_controlled_native_transcript_only(
    tmp_path,
    monkeypatch,
    capsys,
    variant,
):
    data, _ = report("ABC")
    _, live, _ = daily_files(tmp_path, data)
    sessions = tmp_path / ".codex/sessions"
    (transcript,) = sessions.glob("*.jsonl")
    dated = sessions / "2026/11/24"
    dated.mkdir(parents=True)
    transcript = transcript.rename(dated / transcript.name)
    transcript.chmod(0o644)
    for directory in (sessions.parent, sessions, *reversed(dated.parents[:3]), dated):
        directory.chmod(0o755)

    if variant == "group-writable":
        transcript.chmod(0o664)
    elif variant == "other-writable":
        transcript.chmod(0o646)
    elif variant == "writable-directory":
        dated.parent.chmod(0o775)
    elif variant == "symlink":
        original = transcript.rename(dated / "unmatched.jsonl")
        transcript.symlink_to(original)
    elif variant == "wrong-launch":
        transcript.write_bytes(
            transcript.read_bytes().replace(
                NATIVE_LAUNCH.encode(),
                ("6" * 32).encode(),
            )
        )
    elif variant == "duplicate":
        duplicate = dated / f"other-{NATIVE_SESSION}.jsonl"
        duplicate.write_bytes(transcript.read_bytes())
        duplicate.chmod(0o644)
    elif variant == "readable-receipt":
        receipt = tmp_path / f".config/trend-animals/daily-state/{DAY}.launch.json"
        receipt.chmod(0o644)

    opend = PlanningOpenD()
    if variant == "host-modes":
        assert run_daily_plan(tmp_path, opend, PREOPEN, live, monkeypatch) == 0
        planned = json.loads(capsys.readouterr().out)
        (line,) = planned["lines"]
        assert (line["code"], line["quantity"]) == ("US.ABC", 100)
        assert not opend.writes
    else:
        with pytest.raises(SystemExit) as stopped:
            run_daily_plan(tmp_path, opend, PREOPEN, live, monkeypatch)
        assert stopped.value.code == 2
        assert not opend.quote_calls
        assert not opend.calls
        assert not opend.writes


@pytest.mark.parametrize(
    ("basic_status", "snapshot_status", "security_status", "eligible"),
    [
        ("N/A", False, "NORMAL", True),
        ("N/A", False, "N/A", False),
        ("N/A", True, "NORMAL", False),
        (True, False, "NORMAL", False),
    ],
)
def test_daily_cli_requires_active_native_security_snapshot(
    tmp_path,
    monkeypatch,
    capsys,
    basic_status,
    snapshot_status,
    security_status,
    eligible,
):
    data, _ = report("AAPL")
    _, live, _ = daily_files(tmp_path, data)
    opend = PlanningOpenD()
    opend.basic_suspension = basic_status
    opend.snapshot_suspension = snapshot_status
    opend.security_status = security_status
    assert run_daily_plan(tmp_path, opend, PREOPEN, live, monkeypatch) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [line["code"] for line in plan["lines"]] == (["US.AAPL"] if eligible else [])
    assert opend.writes == []
    if not eligible:
        assert opend.calls == []


class NativeHKFutureOpenD(PlanningOpenD):
    def __init__(self):
        super().__init__(market="HK", lot=50)
        self.basic_suspension = "N/A"
        self.contract_owner = "HK.800000"
        self.contract_currency = "港币"
        self.trade_hours = "(09:15 - 12:00), (13:00 - 16:30), (17:00 - 03:00)"
        self.last_trade_time = "2026-11-27 16:00:00"

    def get_acc_list(self):
        return 0, []

    def OpenFutureTradeContext(self, **address):
        class FutureAccount(PlanningOpenD):
            def get_acc_list(self):
                return 0, [
                    {
                        "acc_id": 42,
                        "trd_env": "SIMULATE",
                        "acc_status": "ACTIVE",
                        "trdmarket_auth": ["FUTURES_SIMULATE_HK"],
                        "sim_acc_type": "FUTURES",
                    }
                ]

            def accinfo_query(self, **params):
                return self.query(
                    "funds",
                    params,
                    [
                        {
                            "currency": "HKD",
                            "total_assets": "100000000",
                            "available_funds": "3000000",
                        }
                    ],
                )

            def acctradinginfo_query(self, **params):
                return self.query("capacity", params, [{"max_cash_and_margin_buy": 10}])

        self.future_account = FutureAccount(market="HK")
        return self.future_account

    def get_stock_basicinfo(self, **params):
        status, rows = super().get_stock_basicinfo(**params)
        rows[0].update(main_contract=False, last_trade_time=self.last_trade_time)
        return status, rows

    def get_market_snapshot(self, **params):
        status, rows = super().get_market_snapshot(**params)
        rows[0].update(last_price="20000")
        return status, rows

    def get_future_info(self, **params):
        return 0, [
            {
                "code": params["code_list"][0],
                "owner": self.contract_owner,
                "type": "股指期货",
                "size": 50.0,
                "price_currency": self.contract_currency,
                "trade_time": self.trade_hours,
                "time_zone": "CCT",
            }
        ]


@pytest.mark.parametrize(
    ("variant", "reason"),
    [
        ("verified", None),
        ("afternoon-only", None),
        ("wrong-underlying", "future_underlying_unverified"),
        ("unknown-currency", "future_currency_unverified"),
        ("unknown-session", "future_session_unverified"),
        ("wrong-lot", "future_quantity_unit_unverified"),
        ("expired", "future_expiry_unverified"),
    ],
)
def test_daily_cli_native_hk_futures_prices_one_multiplier_per_contract(
    tmp_path,
    monkeypatch,
    capsys,
    variant,
    reason,
):
    data, _ = report("HSImain", product="future")
    replace_market(data, "HK", "HKD")
    _, live, _ = daily_files(
        tmp_path,
        data,
        supplemental={
            "HSImain": {
                "brokerContract": {
                    "code": "HK.HSI2611",
                    "root": "HSI",
                    "delivery_month": "2026-11",
                    "underlying": "HSImain",
                    "broker_owner": "HK.800000",
                    "as_of": DAY,
                }
            }
        },
    )
    sdk = NativeHKFutureOpenD()
    if variant == "wrong-underlying":
        sdk.contract_owner = "HK.800001"
    elif variant == "unknown-currency":
        sdk.contract_currency = "N/A"
    elif variant == "unknown-session":
        sdk.trade_hours = "(17:00 - 03:00)"
    elif variant == "wrong-lot":
        sdk.snapshot_offset = 1
    elif variant == "expired":
        sdk.last_trade_time = f"{DATA_DAY} 16:00:00"
    elif variant == "afternoon-only":
        sdk.day_type = "AFTERNOON"
    at = datetime.fromisoformat(f"{DAY}T09:08:00+08:00")
    assert run_daily_plan(tmp_path, sdk, at, live, monkeypatch) == 0
    plan = json.loads(capsys.readouterr().out)
    assert sdk.writes == []
    if reason:
        assert plan["lines"] == []
        assert plan["skipped"] == [{"code": "HSImain", "reason": reason}]
        return
    (line,) = plan["lines"]
    assert (line["code"], line["quantity"], line["currency"]) == (
        "HK.HSI2611",
        2,
        "HKD",
    )
    expected_open = "13:00" if variant == "afternoon-only" else "09:15"
    assert line["opens_at"] == f"{DAY}T{expected_open}:00+08:00"
    assert line["notional"] == "2000000.0"
    assert sdk.future_account.writes == []


@pytest.mark.parametrize(
    ("market", "currency", "api_currency", "symbol"),
    [
        ("US", "USD", "USD", "US.AAPL"),
        ("HK", "HKD", "HKD", "HK.00700"),
        ("CN", "CNY", "CNH", "SH.600000"),
    ],
)
def test_futu_native_stock_account_funds_are_cash_limited(
    market,
    currency,
    api_currency,
    symbol,
):
    class NativeStockFunds(PlanningOpenD):
        reported_currency = "N/A"

        def accinfo_query(self, **params):
            assert params["currency"] == api_currency
            return self.query(
                "funds",
                params,
                [
                    {
                        "currency": self.reported_currency,
                        "available_funds": "N/A",
                        "net_cash_power": "N/A",
                        "total_assets": 100000,
                        "cash": 1100,
                        "frozen_cash": 100,
                        "power": 750,
                    }
                ],
            )

        def position_list_query(self, **params):
            assert params["currency"] == api_currency
            return self.query(
                "positions",
                params,
                [
                    {
                        "acc_id": 42,
                        "code": symbol,
                        "currency": api_currency,
                        "market_val": 200,
                        "qty": 2,
                    }
                ],
            )

    opend = NativeStockFunds(market=market)
    config = paper_settings()
    config["accounts"]["paper-us"].update(trade_market=market, currency=currency)
    broker = FutuPaperBroker(config, sdk=opend)
    funds = broker.snapshot()["paper-us"]
    assert (funds["currency"], funds["net_value"], funds["available"]) == (
        currency,
        100000,
        750,
    )
    assert funds["positions"][0]["currency"] == currency
    opend.reported_currency = "JPY"
    with pytest.raises(BrokerUnavailable, match="simulation account balance currency"):
        broker.snapshot()
    assert opend.writes == []


@pytest.mark.parametrize("mixed_currency", [False, True])
def test_daily_cli_uses_native_cash_and_never_guesses_fx(
    tmp_path,
    monkeypatch,
    capsys,
    mixed_currency,
):
    data, _ = report("AAPL")
    _, live, _ = daily_files(tmp_path, data)

    class NativeOpenD(PlanningOpenD):
        def get_acc_list(self):
            markets = (("US", 42), ("HK", 43)) if mixed_currency else (("US", 42),)
            return 0, [
                {
                    "acc_id": account,
                    "trd_env": "SIMULATE",
                    "acc_status": "ACTIVE",
                    "trdmarket_auth": [market],
                    "sim_acc_type": "STOCK",
                    "acc_type": "CASH",
                }
                for market, account in markets
            ]

        def query(self, kind, params, rows):
            assert params["trd_env"] == "SIMULATE"
            assert params["acc_id"] in {42, 43}
            self.calls.append((kind, params))
            return 0, rows

        def accinfo_query(self, **params):
            return self.query(
                "funds",
                params,
                [
                    {
                        "currency": "N/A",
                        "available_funds": "N/A",
                        "net_cash_power": "N/A",
                        "total_assets": 100000,
                        "cash": 1100,
                        "frozen_cash": 100,
                        "power": 750,
                    }
                ],
            )

        def acctradinginfo_query(self, **params):
            return self.query("capacity", params, [{"max_cash_buy": 30}])

    sdk = NativeOpenD()
    assert run_daily_plan(tmp_path, sdk, PREOPEN, live, monkeypatch) == 0
    plan = json.loads(capsys.readouterr().out)
    assert sdk.writes == []
    (line,) = plan["lines"]
    assert (line["code"], line["quantity"], line["notional"]) == (
        "US.AAPL",
        30,
        "600",
    )
    assert [kind for kind, _ in sdk.calls].count("funds") == (
        2 if mixed_currency else 1
    )
    assert any(kind == "capacity" for kind, _ in sdk.calls)
    assert not any(
        code.startswith("FX.")
        for _, args in sdk.quote_calls
        for code in args.get("code_list", [])
    )


def tab_record(
    home,
    role,
    content,
    at,
    *,
    phase=None,
    kind="response_item",
    recipient=None,
    include_id=True,
):
    (transcript,) = (home / ".codex/sessions").rglob(f"*{NATIVE_SESSION}.jsonl")
    identifier = f"fixture-message-{len(transcript.read_bytes().splitlines())}"
    item = {
        "type": "message",
        "role": role,
        "content": [
            {"type": "input_text" if role == "user" else "output_text", "text": content}
        ],
    }
    if include_id:
        item["id"] = identifier
    if phase:
        item["phase"] = phase
    if recipient:
        item["recipient"] = recipient
    record = {"type": kind, "timestamp": at.isoformat(), "payload": item}
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(paper_entry.encode(record) + "\n")


def tab_cli(home, monkeypatch, sdk, live, *, start=PREOPEN):
    clock = SimpleNamespace(at=start)

    def invoke(*args):
        return paper_cli.main(["--session", NATIVE_SESSION, *args])

    def wait(seconds):
        clock.at += timedelta(seconds=seconds)

    monkeypatch.setattr(paper_cli, "sleep_open", wait, raising=False)
    run_daily_plan(home, sdk, start, live, monkeypatch)
    monkeypatch.setattr(paper_cli, "utc_now", lambda: clock.at)
    return clock, invoke


def show_plan(home, plan, when):
    tab_record(home, "assistant", plan["delivery"], when, phase="final_answer")


def test_daily_tab_approval_places_selected_market_order_at_open(
    tmp_path, monkeypatch, capsys
):
    data, _ = report("ABC", "XYZ")
    _, live, _ = daily_files(tmp_path, data)
    sdk = PlanningOpenD()
    clock, invoke = tab_cli(tmp_path, monkeypatch, sdk, live)
    plan = json.loads(capsys.readouterr().out)
    selected = plan["lines"][0]
    assert (selected["code"], selected["quantity"]) == ("US.ABC", 100)
    clock.at += timedelta(seconds=1)
    show_plan(tmp_path, plan, clock.at)
    clock.at += timedelta(seconds=1)
    tab_record(tmp_path, "user", f"模拟批准 {plan['id']} {selected['item']}", clock.at)
    assert invoke("approve", plan["id"]) == 0
    assert sdk.writes == []

    assert invoke("submit", plan["id"]) == 0
    (submitted,) = sdk.writes
    assert (submitted["code"], submitted["qty"], submitted["order_type"]) == (
        "US.ABC",
        100,
        "MARKET",
    )
    assert submitted["trd_env"] == "SIMULATE"
    assert clock.at == OPEN
    assert invoke("submit", plan["id"]) == 0
    assert len(sdk.writes) == 1


@pytest.mark.parametrize(
    "case",
    [
        "wrong-tab",
        "no-message",
        "early",
        "late",
        "unrelated",
        "quoted",
        "tool-recipient",
        "withdrawn",
        "new-revision",
    ],
)
def test_daily_tab_approval_excludes_other_or_stale_replies(
    tmp_path, monkeypatch, capsys, case
):
    data, _ = report("ABC")
    root, live, _ = daily_files(tmp_path, data)
    sdk = PlanningOpenD()
    clock, invoke = tab_cli(tmp_path, monkeypatch, sdk, live)
    plan = json.loads(capsys.readouterr().out)
    approve = "确认全部"
    clock.at += timedelta(seconds=1)
    if case == "early":
        tab_record(tmp_path, "user", approve, clock.at)
        clock.at += timedelta(seconds=1)
    show_plan(tmp_path, plan, clock.at)
    clock.at += timedelta(seconds=1)

    if case == "wrong-tab":
        live["labels"]["herdr.tab"] = "other-tab"
        with pytest.raises(SystemExit):
            invoke("approve", plan["id"])
        assert sdk.writes == []
        return
    if case == "late":
        clock.at = OPEN + timedelta(seconds=1)
        tab_record(tmp_path, "user", approve, clock.at)
    elif case == "unrelated":
        tab_record(tmp_path, "user", "看一下计划", clock.at)
    elif case == "quoted":
        tab_record(tmp_path, "user", f"> {approve}", clock.at)
    elif case == "tool-recipient":
        tab_record(tmp_path, "user", approve, clock.at, recipient="functions.exec")
    elif case in {"withdrawn", "new-revision"}:
        tab_record(tmp_path, "user", approve, clock.at)
        assert invoke("approve", plan["id"]) == 0
        clock.at += timedelta(seconds=1)
        if case == "withdrawn":
            tab_record(tmp_path, "user", f"模拟撤回 {plan['id']} 全部", clock.at)
        else:
            index = root / "reports/_evidence" / DAY
            latest = index / "v03"
            latest.mkdir()
            for path in (index / "v02").iterdir():
                (latest / path.name).write_bytes(path.read_bytes())
                (latest / path.name).chmod(0o600)
            verification = latest / "report-verification.json"
            info = json.loads(verification.read_bytes()) | {"revision": "v03"}
            verification.write_text(paper_entry.encode(info), encoding="utf-8")
            (index / "latest-revision.json").write_text(
                paper_entry.encode({"report_date": DAY, "revision": "v03"}),
                encoding="utf-8",
            )
            upload = tmp_path / f".config/trend-animals/daily-state/{DAY}.upload.json"
            upload.write_text(
                paper_entry.encode(
                    json.loads(upload.read_bytes()) | {"revision": "v03"}
                ),
                encoding="utf-8",
            )
            tab_record(tmp_path, "user", approve, clock.at)
            capsys.readouterr()
            assert invoke("approve", plan["id"]) == 2
            assert json.loads(capsys.readouterr().out)["reason"] == (
                "report_revised_or_unverified"
            )

    if case not in {"withdrawn", "new-revision"}:
        assert invoke("approve", plan["id"]) == 2
    assert invoke("submit", plan["id"]) == (2 if case == "new-revision" else 0)
    assert sdk.writes == []


@pytest.mark.parametrize(
    ("initial_status", "overnight"),
    [("FILLED_PART", False), ("FAILED", False), ("FILLED_PART", True)],
)
def test_daily_retry_requires_fresh_reply_for_reconciled_remainder(
    tmp_path,
    monkeypatch,
    capsys,
    initial_status,
    overnight,
):
    data, _ = report("ABC")
    _, live, _ = daily_files(tmp_path, data)
    filled = 40 if initial_status == "FILLED_PART" else 0

    class RetryOpenD(PlanningOpenD):
        def place_order(self, **params):
            status, rows = super().place_order(**params)
            if len(self.writes) == 1:
                rows[0].update(order_status=initial_status, dealt_qty=filled)
            else:
                rows[0].update(order_id="paper-retry")
            return status, rows

    sdk = RetryOpenD()
    clock, invoke = tab_cli(tmp_path, monkeypatch, sdk, live)
    plan = json.loads(capsys.readouterr().out)
    plan_id = plan["id"]
    item = plan["lines"][0]["item"]
    clock.at += timedelta(seconds=1)
    show_plan(tmp_path, plan, clock.at)
    clock.at += timedelta(seconds=1)
    tab_record(tmp_path, "user", f"模拟批准 {plan_id} 全部", clock.at)
    assert invoke("approve", plan_id) == 0
    capsys.readouterr()
    assert invoke("submit", plan_id) == 0
    initial, _ = map(json.loads, capsys.readouterr().out.splitlines())
    (result,) = initial["results"]
    assert result["state"] == ("active" if filled else "rejected")
    assert result["filled"] == filled
    assert "retry_reply" not in result
    (first,) = sdk.writes
    clock.at += timedelta(seconds=1)
    if overnight:
        clock.at = OPEN + timedelta(hours=2)
        same_day_invoke = invoke

        def invoke(*args):
            return same_day_invoke("--report-date", DAY, *args)

    assert invoke("submit", plan_id, "--retry-item", item) == 2
    capsys.readouterr()

    explicit = f"模拟重试 {plan_id} {item} {100 - filled} {first['remark']}"
    tab_record(tmp_path, "user", explicit, clock.at)
    assert invoke("submit", plan_id, "--retry-item", item) == 2
    assert len(sdk.writes) == 1
    capsys.readouterr()

    terminal = "CANCELLED_PART" if filled else "FAILED"
    sdk.history = [
        first
        | {
            "order_id": "paper-a",
            "order_status": terminal,
            "dealt_qty": filled,
        }
    ]
    clock.at += timedelta(seconds=1)
    assert invoke("reconcile", plan_id) == 0
    (reconciled,) = json.loads(capsys.readouterr().out)["results"]
    assert reconciled["remaining"] == 100 - filled
    assert reconciled["retry_reply"] == explicit
    assert invoke("submit", plan_id, "--retry-item", item) == 2
    clock.at += timedelta(seconds=1)
    tab_record(tmp_path, "user", explicit, clock.at)
    assert invoke("submit", plan_id, "--retry-item", item) == 0
    assert [order["qty"] for order in sdk.writes] == [100, 100 - filled]
    assert len({order["remark"] for order in sdk.writes}) == 2
    assert invoke("submit", plan_id, "--retry-item", item) == 2
    assert len(sdk.writes) == 2


def two_market_inputs(*extra):
    data, facts = report("ABC", "00005", *extra)
    hk_asset = data["long"][0]["direct"][1]
    hk_asset.update(market="HK", currency="HKD")
    targets = data["collection"]
    targets["targets"]["HK"] = DATA_DAY
    targets["target_evidence"]["HK"] = deepcopy(targets["target_evidence"]["US"])
    for path, item in targets["evidence"].items():
        if path.startswith("/long/0/direct/1/"):
            item["market"] = "HK"
    for item in facts["instruments"]["00005"].values():
        item["market"] = "HK"
    facts["instruments"]["00005"]["route"]["value"].update(
        code="HK.00005",
        account="paper-hk",
        currency="HKD",
        reference_price="25",
        lot=100,
    )
    facts["sessions"]["HK"] = facts["sessions"]["US"] | {
        "market": "HK",
        "opens_at": f"{DAY}T09:30:00+08:00",
        "closes_at": f"{DAY}T16:00:00+08:00",
        "timezone": "Asia/Hong_Kong",
    }
    return data, facts


def test_native_account_budget_without_portfolio_fx(tmp_path):
    service = make_service(tmp_path)
    data, facts = two_market_inputs()
    broker = service.broker
    broker.accounts["paper-hk"] = {
        "identity": "fixture-hk",
        "currency": "HKD",
        "net_value": "500000",
        "available": "500000",
        "environment": "SIMULATE",
        "markets": ["HK"],
        "products": ["stock"],
        "positions": [],
        "orders": [],
    }
    morning = datetime.fromisoformat(f"{DAY}T08:30:00+08:00")
    plan = service.plan(data, facts, now=morning)
    assert {line["code"]: line["quantity"] for line in plan["lines"]} == {
        "US.ABC": 100,
        "HK.00005": 400,
    }
    broker.accounts["paper-us"]["positions"] = [footprint("EXP", 99500)]
    tighter = service.plan(data, facts, now=morning)
    assert {line["code"]: line["quantity"] for line in tighter["lines"]} == {
        "US.ABC": 25,
        "HK.00005": 400,
    }
    broker.accounts["paper-us"]["positions"] = []
    broker.accounts["paper-hk"]["available"] = "2499"
    low = service.plan(data, facts, now=morning)
    assert [line["code"] for line in low["lines"]] == ["US.ABC"]
    broker.accounts["paper-hk"]["available"] = "500000"
    plan = service.plan(data, facts, now=morning)
    reply = approval(plan, morning, delivered_at=morning.isoformat())
    assert service.approve(plan["id"], reply, now=morning)

    hk_open = datetime.fromisoformat(f"{DAY}T09:30:00+08:00")
    service.submit(plan["id"], now=hk_open)
    broker.accounts["paper-us"]["net_value"] = "50000"
    outcomes = service.submit(plan["id"], now=OPEN)
    assert (
        next(result for result in outcomes if result["code"] == "US.ABC")["state"]
        == "skipped"
    )
    service.submit(plan["id"], now=OPEN)
    assert [(code, quantity) for code, _, quantity, _ in broker.orders] == [
        ("HK.00005", 400)
    ]


@pytest.mark.parametrize(
    ("miss_us_wake", "capacity_latency"), [(False, 2), (True, 2), (False, 10)]
)
def test_scheduled_cli_executes_multi_item_groups_without_catchup(
    tmp_path,
    monkeypatch,
    capsys,
    miss_us_wake,
    capacity_latency,
):
    data, _ = two_market_inputs("XYZ", "QRS")
    _, live, _ = daily_files(tmp_path, data)

    class LatentOpenD(PlanningOpenD):
        controlled = None
        sends = []

        def get_acc_list(self):
            return 0, [
                {
                    "acc_id": account,
                    "trd_env": "SIMULATE",
                    "acc_status": "ACTIVE",
                    "trdmarket_auth": [market],
                    "sim_acc_type": "STOCK",
                    "acc_type": "CASH",
                }
                for market, account in (("US", 42), ("HK", 43))
            ]

        def query(self, kind, params, rows):
            assert params["trd_env"] == "SIMULATE"
            assert params["acc_id"] in {42, 43}
            self.calls.append((kind, params))
            if self.controlled is not None:
                latency = {"funds": 3, "capacity": capacity_latency, "write": 1}.get(
                    kind, 0
                )
                self.controlled.at += timedelta(seconds=latency)
            return 0, rows

        def accinfo_query(self, **params):
            return self.query(
                "funds",
                params,
                [
                    {
                        "currency": "USD" if params["acc_id"] == 42 else "HKD",
                        "total_assets": "100000"
                        if params["acc_id"] == 42
                        else "500000",
                        "available_funds": "100000",
                    }
                ],
            )

        def get_stock_basicinfo(self, **params):
            status, rows = super().get_stock_basicinfo(**params)
            if rows[0]["code"].startswith("HK."):
                rows[0]["lot_size"] = 100
            return status, rows

        def get_market_snapshot(self, **params):
            status, rows = super().get_market_snapshot(**params)
            if rows[0]["code"].startswith("HK."):
                rows[0].update(lot_size=100, last_price="25")
            return status, rows

        def place_order(self, **params):
            self.sends.append((params["code"], self.controlled.at))
            return super().place_order(**params)

    sdk = LatentOpenD()
    hk_open = datetime.fromisoformat(f"{DAY}T09:30:00+08:00")
    clock, invoke = tab_cli(
        tmp_path, monkeypatch, sdk, live, start=hk_open - timedelta(minutes=10)
    )
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["lines"]) == 4
    clock.at += timedelta(seconds=1)
    show_plan(tmp_path, plan, clock.at)
    clock.at += timedelta(seconds=1)
    tab_record(tmp_path, "user", f"模拟批准 {plan['id']} 全部", clock.at)
    assert invoke("approve", plan["id"]) == 0
    sdk.calls.clear()
    sdk.controlled = clock
    if miss_us_wake:

        def wake_late(seconds):
            clock.at += timedelta(seconds=seconds)
            if clock.at == OPEN:
                clock.at += timedelta(seconds=3)

        monkeypatch.setattr(paper_cli, "sleep_open", wake_late)
    capsys.readouterr()
    assert invoke("submit", plan["id"]) == 0
    hk, us, _ = map(json.loads, capsys.readouterr().out.splitlines())
    assert [row["code"] for row in hk["results"]] == ["HK.00005"]
    assert sdk.sends[0] == (
        "HK.00005",
        hk_open + timedelta(seconds=6 + capacity_latency),
    )
    if miss_us_wake:
        assert len(sdk.sends) == 1
        assert all(result["reason"] == "missed_open" for result in us["results"])
    elif capacity_latency == 10:
        assert [write[0] for write in sdk.sends] == ["HK.00005", "US.ABC", "US.XYZ"]
        assert [result["state"] for result in us["results"]] == [
            "active",
            "active",
            "skipped",
        ]
        assert us["results"][-1]["reason"] == "open_dispatch_deadline"
    else:
        assert [write[0] for write in sdk.sends] == [
            "HK.00005",
            "US.ABC",
            "US.XYZ",
            "US.QRS",
        ]
        assert all(result["state"] == "active" for result in us["results"])
        assert sdk.sends[-1][1] == OPEN + timedelta(seconds=14)
    expected_reads = 2 if miss_us_wake else 4
    assert len([kind for kind, _ in sdk.calls if kind == "funds"]) == expected_reads
    sent = len(sdk.writes)
    assert invoke("submit", plan["id"]) == 0
    assert len(sdk.writes) == sent


@pytest.mark.parametrize(
    ("reply", "competing_plan", "items"),
    [
        ("确认全部", False, [1, 2, 3]),
        ("只确认第1项", False, [1]),
        ("确认第1、3项", False, [1, 3]),
        ("确认第4项", False, []),
        ("确认第1、1项", False, []),
        ("我看到有人说确认全部", False, []),
        ("确认全部", True, []),
    ],
)
def test_daily_cli_accepts_unambiguous_numbered_chinese_approval(
    tmp_path,
    monkeypatch,
    capsys,
    reply,
    competing_plan,
    items,
):
    data, _ = report("ABC", "QRS", "XYZ")
    _, live, _ = daily_files(tmp_path, data)
    sdk = PlanningOpenD()
    clock, invoke = tab_cli(tmp_path, monkeypatch, sdk, live)
    plan = json.loads(capsys.readouterr().out)
    clock.at += timedelta(seconds=1)
    if competing_plan:
        other = plan["delivery"].replace(plan["id"], "a" * 64)
        tab_record(tmp_path, "assistant", other, clock.at, phase="final_answer")
        clock.at += timedelta(seconds=1)
    show_plan(tmp_path, plan, clock.at)
    clock.at += timedelta(seconds=1)
    tab_record(tmp_path, "user", reply, clock.at, include_id=False)
    assert invoke("approve", plan["id"]) == (0 if items else 2)
    confirmation = json.loads(capsys.readouterr().out)
    if not items:
        assert confirmation["reason"] == "approval_clarification_required"
    assert invoke("submit", plan["id"]) == 0
    assert [order["code"] for order in sdk.writes] == [
        plan["lines"][index - 1]["code"] for index in items
    ]
    assert all(
        row.startswith(f"第{index}项 | {plan['lines'][index - 1]['item']} | ")
        for index, row in enumerate(plan["delivery"].splitlines()[1:], 1)
    )


@pytest.mark.parametrize("process_withdrawal", ["interactive", "waiter"])
def test_mixed_openings_withdraw_all_preserves_past_and_cancels_future(
    tmp_path,
    monkeypatch,
    capsys,
    process_withdrawal,
):
    data, _ = two_market_inputs()
    _, live, _ = daily_files(tmp_path, data)

    class TwoMarketOpenD(PlanningOpenD):
        def get_acc_list(self):
            return 0, [
                {
                    "acc_id": account,
                    "trd_env": "SIMULATE",
                    "acc_status": "ACTIVE",
                    "trdmarket_auth": [market],
                    "sim_acc_type": "STOCK",
                    "acc_type": "CASH",
                }
                for market, account in (("US", 42), ("HK", 43))
            ]

        def query(self, kind, params, rows):
            assert params["trd_env"] == "SIMULATE"
            assert params["acc_id"] in {42, 43}
            self.calls.append((kind, params))
            return 0, rows

        def accinfo_query(self, **params):
            return self.query(
                "funds",
                params,
                [
                    {
                        "currency": "USD" if params["acc_id"] == 42 else "HKD",
                        "total_assets": 100000,
                        "available_funds": 100000,
                    }
                ],
            )

    sdk = TwoMarketOpenD()
    hk_open = datetime.fromisoformat(f"{DAY}T09:30:00+08:00")
    clock, invoke = tab_cli(
        tmp_path, monkeypatch, sdk, live, start=hk_open - timedelta(minutes=1)
    )
    plan = json.loads(capsys.readouterr().out)
    plan_id = plan["id"]
    items = {line["code"]: line["item"] for line in plan["lines"]}
    assert set(items) == {"HK.00005", "US.ABC"}
    clock.at += timedelta(seconds=1)
    show_plan(tmp_path, plan, clock.at)
    clock.at += timedelta(seconds=1)
    tab_record(tmp_path, "user", "确认全部", clock.at)
    assert invoke("approve", plan_id) == 0
    capsys.readouterr()
    withdrawn = False

    def wait(seconds):
        nonlocal withdrawn
        clock.at += timedelta(seconds=seconds)
        if not withdrawn and hk_open < clock.at < OPEN:
            withdrawn = True
            assert [write["code"] for write in sdk.writes] == ["HK.00005"]
            tab_record(tmp_path, "user", "确认全部", clock.at)
            assert invoke("approve", plan_id) == 2
            clock.at += timedelta(seconds=1)
            tab_record(tmp_path, "user", plan["withdraw_all"], clock.at)
            if process_withdrawal == "interactive":
                assert invoke("approve", plan_id) == 0

    monkeypatch.setattr(paper_cli, "sleep_open", wait)
    assert invoke("submit", plan_id) == 0
    messages = [json.loads(row) for row in capsys.readouterr().out.splitlines()]
    (decision,) = (row for row in messages if row.get("decision") == "deny")
    assert decision["recorded"] is True
    assert decision["items"] == [items["US.ABC"]]
    assert decision["ignored_opened_items"] == [items["HK.00005"]]
    assert [write["code"] for write in sdk.writes] == ["HK.00005"]
    assert {row["code"]: row["state"] for row in messages[-1]["status"]["results"]} == {
        "HK.00005": "active",
        "US.ABC": "denied",
    }

    before = messages[-1]["status"]
    clock.at += timedelta(seconds=1)
    tab_record(tmp_path, "user", plan["withdraw_all"], clock.at)
    assert invoke("approve", plan_id) == 0
    no_op = json.loads(capsys.readouterr().out)
    assert no_op["recorded"] is False
    assert no_op["decision"] == "deny"
    assert no_op["reason"] == "no_unopened_items"
    assert set(no_op["ignored_opened_items"]) == set(items.values())
    assert invoke("status") == 0
    assert json.loads(capsys.readouterr().out) == before
    assert [write["code"] for write in sdk.writes] == ["HK.00005"]
