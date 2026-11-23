"""Offline collection assessment and run-day control. No network or fee authority."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo


def valid_date(value):
    try:
        return (
            date.fromisoformat(value).isoformat()
            if isinstance(value, str)
            and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
            else None
        )
    except ValueError:
        return None


def dated_projection(report):
    result = deepcopy(report)
    targets = report.get("collection", {}).get("targets", {})
    pending = []
    for direction in ("short", "long"):
        for gi, group in enumerate(result.get(direction, [])):
            group["_date_note"] = "大类数据日期 " + (
                valid_date(group.get("data_date")) or "待核"
            )
            for section in ("direct", "searchable", "related"):
                for ai, asset in enumerate(group.get(section, [])):
                    actual = valid_date(asset.get("data_date"))
                    target = valid_date(targets.get(asset.get("market")))
                    asset["_date_note"] = "行情日期 " + (actual or "待核")
                    if (asset.get("data_date") is not None and not actual) or (
                        actual and target and actual > target
                    ):
                        for field in ("trend", "strength", "local_strength", "signal"):
                            asset[field] = None
                        asset["signal_events"] = []
                        asset["watch_eligible"] = False
                        asset["reason"] = "行情日期无效或晚于目标日，行情待核。"
                    prefix = f"/{direction}/{gi}/{section}/{ai}"
                    proofs = report.get("collection", {}).get("evidence", {})
                    lookup_proof = proofs.get(prefix + "/lookup", {})
                    if asset.get("lookup") == "miss" and lookup_proof:
                        if (
                            lookup_proof.get("state") != "verified"
                            or lookup_proof.get("full_lookup") is not True
                            or valid_date(lookup_proof.get("data_date")) != target
                            or lookup_proof.get("identity")
                            != asset.get("instrument_id")
                            or lookup_proof.get("market") != asset.get("market")
                            or lookup_proof.get("instrument_type")
                            != asset.get("instrument_type")
                            or lookup_proof.get("value") != "miss"
                            or not lookup_proof.get("source")
                            or not lookup_proof.get("version")
                        ):
                            asset["lookup"] = "pending"
                            asset["reason"] = "完整查询证据或适用范围待核。"
                    field_dates = []
                    fields_current = True
                    for field, label in (
                        ("trend", "趋势"),
                        ("strength", "全局强度"),
                        ("local_strength", "同品种强度"),
                        ("signal_events", "特殊信号"),
                    ):
                        proof = proofs.get(prefix + "/" + field, {})
                        field_day = valid_date(proof.get("data_date"))
                        if proof:
                            if field_day and target and field_day < target:
                                field_dates.append("历史数据")
                            field_dates.append(label + "日期 " + (field_day or "待核"))
                        if (
                            proof.get("state") != "verified"
                            or not field_day
                            or field_day != target
                            or proof.get("value") != asset.get(field)
                            or not proof.get("source")
                            or not proof.get("version")
                            or not asset.get("instrument_id")
                            or proof.get("identity") != asset.get("instrument_id")
                            or proof.get("market") != asset.get("market")
                            or not asset.get("instrument_type")
                            or proof.get("instrument_type")
                            != asset.get("instrument_type")
                        ):
                            if field in ("trend", "signal_events") or proof:
                                fields_current = False
                            pending.append(
                                f"{asset['code']} {field}：目标 {target or '待核'}，"
                                f"实际 {field_day or actual or '待核'}"
                            )
                    if field_dates:
                        asset["_date_note"] += "；" + "；".join(field_dates)
                    if not fields_current:
                        asset["watch_eligible"] = False
                    if not actual or not target or actual != target:
                        asset["watch_eligible"] = False
                        asset["_date_note"] += (
                            "（历史数据）"
                            if actual and target and actual < target
                            else "（日期待核）"
                        )
                        pending.append(
                            f"{asset['code']}：目标 {target or '待核'}，"
                            f"实际 {actual or '待核'}"
                        )
    return result, pending


def required_facts(report):
    """Enumerate visible obligations; the supplied missing list is never authority."""
    yield "/board_date", "榜图", "BOARD", "board", report.get("board_date")
    yield "/board_version", "榜图版次", "BOARD", "board", report.get("board_version")
    for direction in ("short", "long"):
        for gi, group in enumerate(report.get(direction, [])):
            prefix = f"/{direction}/{gi}"
            for field in ("board_strength", "coverage"):
                yield (
                    prefix + "/" + field,
                    group["name"] + " " + field,
                    "BOARD",
                    group["name"],
                    group.get(field),
                )
            for section in ("direct", "searchable", "related"):
                for ai, asset in enumerate(group.get(section, [])):
                    fields = (
                        ("lookup",)
                        if asset.get("lookup")
                        else ("trend", "strength", "local_strength", "signal_events")
                    )
                    for field in fields:
                        yield (
                            prefix + f"/{section}/{ai}/" + field,
                            asset["code"] + " " + field,
                            asset.get("market"),
                            asset.get("instrument_id"),
                            asset.get(field),
                        )


def confirmation_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return value if parsed.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def assess(report):
    """Evidence must bind every obligation to its frozen target and actual value."""
    collection = report.get("collection") or {}
    targets = collection.get("targets") or {}
    evidence = collection.get("evidence") or {}
    worklist = collection.get("worklist") or []
    obligations = {
        item.get("path"): item for item in worklist if isinstance(item, dict)
    }
    pending = []
    facts = list(required_facts(report))
    paths = [item.get("path") for item in worklist if isinstance(item, dict)]
    if not worklist or len(paths) != len(worklist) or len(set(paths)) != len(paths):
        pending.append("工作清单缺失或重复")
    known_paths = {fact[0] for fact in facts}
    for path in paths:
        if path not in known_paths:
            pending.append("额外工作项待核")
    for path, label, market, identity, value in facts:
        parts = path.strip("/").split("/")
        subject = report
        for part in parts[:-1]:
            subject = subject[int(part)] if isinstance(subject, list) else subject[part]
        item = obligations.get(path, {})
        proof = evidence.get(path, {})
        target = valid_date(targets.get(market))
        evidence_day = valid_date(proof.get("data_date"))
        # A proof's metadata cannot replace the actual date cited in the report.
        # Board edition remains independent of the board's data day.
        actual = valid_date(value) if path == "/board_date" else evidence_day
        if (
            not target
            or not identity
            or value is None
            or item.get("target_date") != target
            or item.get("market") != market
            or proof.get("state") != "verified"
            or evidence_day != target
            or actual != target
            or proof.get("identity") != identity
            or proof.get("value") != value
            or not proof.get("source")
            or not proof.get("version")
        ):
            pending.append(f"{label}：目标 {target or '待核'}，实际 {actual or '待核'}")
        if len(parts) == 5:
            if (
                not subject.get("instrument_type")
                or proof.get("market") != market
                or proof.get("instrument_type") != subject.get("instrument_type")
                or valid_date(subject.get("data_date")) != target
            ):
                pending.append(label + "：身份范围或行情日期待核")
        if len(parts) == 3 and valid_date(subject.get("data_date")) != target:
            pending.append(label + "：大类日期待核")
        if parts[-1] in ("strength", "local_strength", "board_strength"):
            raw = str(value).removesuffix(" / 100")
            try:
                usable = (
                    re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", raw)
                    and Decimal(raw) <= 100
                )
            except InvalidOperation:
                usable = False
            if not usable:
                pending.append(label + "：数值待核")
        if path.endswith("/lookup") and (
            value != "miss"
            or proof.get("full_lookup") is not True
            or subject.get("platform_match_verified") is True
        ):
            pending.append(label + "：完整查询范围待核")
    for direction in ("short", "long"):
        groups = report.get(direction, [])
        if {g.get("seat") for g in groups} != {1, 2}:
            pending.append(direction + "：榜图席位待核")
        for group in groups:
            members = [
                a.get("instrument_id")
                for section in ("direct", "searchable", "related")
                for a in group.get(section, [])
            ]
            coverage = group.get("coverage") or {}
            if (
                coverage.get("state") != "complete"
                or coverage.get("instrument_ids") != members
                or any(not member for member in members)
            ):
                pending.append(group["name"] + "：成分及查询覆盖待核")
    verified_at = confirmation_time(collection.get("verified_at"))
    if not verified_at:
        pending.append("完备核实时间待核")
    return {"complete": not pending, "pending": pending, "verified_at": verified_at}


# Persistent controls deliberately do not send requests, sleep, or hold the shared
# billing lock. The scheduled owner uses the existing ledger around each send.
TAIPEI = ZoneInfo("Asia/Taipei")


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def save(path, value):
    """Durably replace control state; failure must propagate to the owner."""
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def state_lock(path):
    # NB: never wait holding another lock. This is not a billing/run-lock token.
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def initialize(path, report):
    path = Path(path)
    day = valid_date(report.get("date"))
    if not day:
        raise ValueError("Invalid report day")
    with state_lock(path):
        if path.exists():
            state = json.loads(path.read_text())
            check_frozen(state, report)
            return state
        state = {
            "date": day,
            "targets": report.get("collection", {}).get("targets", {}),
            "worklist": report.get("collection", {}).get("worklist", []),
            "last_slot": -1,
            "rounds": [],
            "evidence": {},
            "published": None,
        }
        save(path, state)
        return state


def check_frozen(state, report):
    if (
        report.get("date") != state["date"]
        or report.get("collection", {}).get("targets", {}) != state["targets"]
    ):
        raise ValueError("Report day and targets are frozen; reconcile original run")


def claim_round(path, report, now):
    path = Path(path)
    instant = datetime.fromisoformat(now)
    if instant.utcoffset() is None:
        raise ValueError("Clock must have a timezone")
    with state_lock(path):
        state = json.loads(path.read_text())
        check_frozen(state, report)
        start = datetime.fromisoformat(state["date"] + "T08:30:00").replace(
            tzinfo=TAIPEI
        )
        slot = int((instant - start).total_seconds() // 3600)
        if slot < 0 or slot <= state["last_slot"]:
            return None
        if state.get("published") == fingerprint(report) and assess(report)["complete"]:
            return None
        evidence = report.get("collection", {}).get("evidence", {})
        hashes = {key: fingerprint(value) for key, value in evidence.items()}
        changed = sorted(
            key
            for key in set(hashes) | set(state["evidence"])
            if hashes.get(key) != state["evidence"].get(key)
        )
        assessment = assess(report)
        # Re-assess cheap local facts, but request/recompute only affected items.
        # Release observation alone does not satisfy any instrument obligation.
        pending_paths = []
        markets = set()
        for fact in required_facts(report):
            path_, label, market, identity, value = fact
            if any(text.startswith(label + "：") for text in assessment["pending"]):
                pending_paths.append(path_)
                if market:
                    markets.add(market)
        if not assessment["complete"] and not pending_paths:
            markets.add("BOARD")
        result = {
            "slot": slot,
            "observed_at": now,
            "next_check": (start + timedelta(hours=slot + 1)).isoformat(),
            "markets": sorted(markets),
            "pending_paths": pending_paths,
            "changed": changed,
            "affected_groups": sorted(
                {
                    "/".join(p.split("/")[:3])
                    for p in changed
                    if p.startswith(("/short/", "/long/"))
                }
            ),
        }
        state.update(
            last_slot=slot,
            evidence=hashes,
            worklist=report.get("collection", {}).get("worklist", []),
            assessment=assessment,
            projection=report,
        )
        state["rounds"].append(result)
        state["published"] = None
        save(path, state)  # claim before any observation; unknown is never replayed
        return result


def render_html(report):
    """Use the official public renderer, including its exact pinned template."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source, output = root / "input.json", root / "report.html"
        source.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("render_report.py")),
                str(source),
                str(output),
            ],
            check=True,
            capture_output=True,
        )
        return output.read_text(encoding="utf-8")


def confirm_publication(path, report, local, delivered):
    """Stop only after current complete HTML is published and read back exactly."""
    path = Path(path)
    if not assess(report)["complete"]:
        raise ValueError("Report evidence is incomplete")
    expected = render_html(report).encode("utf-8")
    if Path(local).read_bytes() != expected or Path(delivered).read_bytes() != expected:
        raise ValueError("Current HTML publication readback mismatch")
    with state_lock(path):
        state = json.loads(path.read_text())
        check_frozen(state, report)
        state.update(
            published=fingerprint(report),
            projection=report,
            assessment=assess(report),
            publication={
                "local": str(local),
                "delivered": str(delivered),
                "html_sha256": hashlib.sha256(expected).hexdigest(),
            },
        )
        save(path, state)


def target_dates(report_day, calendars):
    """Freeze D-1, or the last verified local closing session on/before D-1."""
    previous = date.fromisoformat(report_day) - timedelta(days=1)
    result = {}
    for market, calendar in calendars.items():
        target = None
        if calendar.get("source"):
            if calendar.get("kind") == "continuous":
                target = previous.isoformat()
            elif calendar.get("kind") == "equity":
                through = valid_date(calendar.get("covered_through"))
                sessions = calendar.get("sessions", [])
                if (
                    through
                    and through >= previous.isoformat()
                    and sessions
                    and all(valid_date(value) for value in sessions)
                ):
                    eligible = [
                        value for value in sessions if value <= previous.isoformat()
                    ]
                    target = max(eligible) if eligible else None
        result[market] = target
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("targets", "init", "claim", "assess", "confirm")
    )
    parser.add_argument(
        "input", type=Path, help="Verified projection, or calendar JSON for targets"
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument(
        "--now", help="Timezone-aware observation time; no waiting or requests"
    )
    parser.add_argument("--local", type=Path)
    parser.add_argument("--delivered", type=Path)
    args = parser.parse_args()
    try:
        report = json.loads(args.input.read_text(encoding="utf-8"))
        if args.action == "targets":
            value = target_dates(report["date"], report["calendars"])
        elif args.action == "assess":
            value = assess(report)
        else:
            if args.state is None:
                raise ValueError("--state is required")
            if args.action == "init":
                value = initialize(args.state, report)
            elif args.action == "claim":
                if args.now is None:
                    raise ValueError("--now is required")
                value = claim_round(args.state, report, args.now)
            else:
                if args.local is None or args.delivered is None:
                    raise ValueError("--local and --delivered are required")
                confirm_publication(args.state, report, args.local, args.delivered)
                value = {"publication_verified": True}
        print(json.dumps(value, ensure_ascii=False))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        subprocess.CalledProcessError,
    ) as error:
        parser.exit(2, f"Collection control failed: {error}\n")


if __name__ == "__main__":
    main()
