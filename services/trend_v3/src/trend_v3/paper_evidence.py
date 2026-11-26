"""Verify paper candidates against the daily report and its private evidence."""

import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .freshness import is_opening_data_fresh

SIGNALS = {"long": ("温", "热", "温转热"), "short": ("凉", "寒", "凉转寒")}

BOARD_SEATS = (1, 2)
TAIPEI = ZoneInfo("Asia/Taipei")
SOURCE_FIELDS = ("trendTemperaturePrev", "trendTemperatureCurr", "tickerLabels")
REPORT_SCHEMA = "trend-v3-paper-evidence/v1"


def observation_signal(direction, previous, current, labels):
    """Match a first observation signal; labels must describe this observation."""
    before, after, event = SIGNALS[direction]
    return (
        previous == before
        and current in ((after, "冻") if direction == "short" else (after,))
        and isinstance(labels, list)
        and event in labels
    )


def decimal(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return (
        result
        if result.is_finite() and (not result or abs(result.adjusted()) <= 18)
        else None
    )


def instant(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return result if result.utcoffset() is not None else None


def day(value):
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return result if result.isoformat() == value else None


def text(value):
    return isinstance(value, str) and bool(value.strip())


def identity(value):
    if text(value) or (type(value) is int and value >= 0):
        return str(value).strip()
    return None


def receipt(proof, owner, market, kind, expected):
    return (
        isinstance(proof, dict)
        and proof.get("state") == "verified"
        and text(proof.get("source"))
        and text(proof.get("version"))
        and identity(proof.get("identity")) == owner
        and proof.get("market", market if market == "BOARD" else None) == market
        and proof.get("instrument_type") == kind
        and day(proof.get("data_date")) == expected
        and expected is not None
        and "value" in proof
    )


def target(collection, market, report_day):
    expected = day(collection.get("targets", {}).get(market))
    basis = report_day - timedelta(days=1)
    proof = collection.get("target_evidence", {}).get(market, {})
    if (
        not expected
        or expected > basis
        or proof.get("state") != "verified"
        or not text(proof.get("source"))
        or day(proof.get("report_date")) != report_day
        or day(proof.get("basis_date")) != basis
    ):
        return None
    if proof.get("kind") == "continuous":
        return expected if expected == basis else None
    if proof.get("kind") != "equity":
        return None
    covered = day(proof.get("covered_through"))
    sessions = [day(value) for value in proof.get("sessions", [])]
    if not covered or covered < basis or not sessions:
        return None
    if any(value is None or value > basis for value in sessions):
        return None
    return expected if max(sessions) == expected else None


def opening(facts, market, report_day, expected):
    session = facts.get("sessions", {}).get(market, {})
    opens, closes = instant(session.get("opens_at")), instant(session.get("closes_at"))
    try:
        zone = ZoneInfo(session.get("timezone", ""))
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if (
        session.get("state") != "verified"
        or not text(session.get("source"))
        or session.get("market") != market
        or session.get("session") != "regular"
        or day(session.get("report_date")) != report_day
        or day(session.get("data_date")) != expected
        or opens is None
        or closes is None
        or opens >= closes
        or opens.astimezone(zone).date() != day(session.get("session_date"))
        or opens.astimezone(zone).utcoffset() != opens.utcoffset()
        or closes.astimezone(zone).utcoffset() != closes.utcoffset()
        or opens.astimezone(TAIPEI).date() != report_day
    ):
        return None
    return session


def field(evidence, prefix, name, value, owner, market, kind, expected):
    proof = evidence.get(f"{prefix}/{name}")
    return receipt(proof, owner, market, kind, expected) and (
        type(proof["value"]) is type(value) and proof["value"] == value
    )


def tradable_contract(route, owner, raw_code, market, report_day):
    month, root = route.get("delivery_month"), route.get("contract_root")
    if not text(month) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        return False
    delivery = day(month + "-01")
    if (
        delivery is None
        or delivery < report_day.replace(day=1)
        or not text(root)
        or not re.fullmatch(r"[A-Z0-9]+", root)
        or route.get("tradable_contract_verified") is not True
        or identity(route.get("underlying_id")) != owner
    ):
        return False
    year_code = month[2:4]
    month_code = "FGHJKMNQUVXZ"[delivery.month - 1]
    code = route["code"]
    return code != f"{market}.{raw_code}" and code in {
        f"{market}.{root}{year_code}{month[-2:]}",
        f"{market}.{root}{month_code}{year_code}",
        f"{market}.{root}{month_code}{year_code[-1:]}",
    }


def board_verified(report, collection, evidence, report_day):
    expected = day(collection.get("targets", {}).get("BOARD"))
    return (
        expected is not None
        and expected < report_day
        and day(report.get("board_date")) == expected
        and text(report.get("board_version"))
        and all(
            field(
                evidence, "", name, report.get(name), "board", "BOARD", None, expected
            )
            and evidence[f"/{name}"]["version"] == report["board_version"]
            for name in ("board_date", "board_version")
        )
    )


def legacy_parent_seat(report, direction, group):
    """Discovery may grow; paper eligibility remains the original two extremes."""
    mode = report.get("selection_mode", "legacy-two-seat")
    if mode == "legacy-two-seat":
        return True
    if mode != "threshold-v1":
        return False
    parent = report.get("collection", {}).get("parent_board", {})
    rows = parent.get("groups", [])
    observed, cutoff = instant(parent.get("observed_at")), instant(report.get("cutoff"))
    if (
        parent.get("state") != "verified"
        or parent.get("complete") is not True
        or not text(parent.get("source"))
        or not rows
        or parent.get("row_count") != len(rows)
        or day(parent.get("data_date")) != day(report.get("board_date"))
        or parent.get("version") != report.get("board_version")
        or observed is None
        or cutoff is None
        or observed > cutoff
        or len({r.get("name") for r in rows}) != len(rows)
        or any(
            not text(r.get("name"))
            or decimal(r.get("strength")) is None
            or not 0 <= decimal(r["strength"]) <= 100
            for r in rows
        )
    ):
        return False
    ordered = sorted(
        rows, key=lambda row: decimal(row["strength"]), reverse=direction == "long"
    )[:2]
    seat = group.get("seat")
    return (
        type(seat) is int
        and 1 <= seat <= len(ordered)
        and group.get("name") == ordered[seat - 1]["name"]
        and decimal(group.get("board_strength"))
        == decimal(ordered[seat - 1]["strength"])
    )


def board_seat_verified(report, evidence, direction, index):
    groups = report.get(direction, [])
    group = groups[index]
    seat, name = group.get("seat"), group.get("name")
    return (
        type(seat) is int
        and seat in BOARD_SEATS
        and legacy_parent_seat(report, direction, group)
        and text(name)
        and sum(other.get("seat") == seat for other in groups) == 1
        and field(
            evidence,
            f"/{direction}/{index}",
            "board_strength",
            group.get("board_strength"),
            name,
            "BOARD",
            None,
            day(report["board_date"]),
        )
    )


def paper_report_projection(report, selection, binding):
    """Read original seats from the existing same-revision hashed selection only.

    This projection is private input for the existing producer/candidate pipeline;
    it never changes the public report or invents members from the parent ranks.
    """
    if report.get("selection_mode") != "threshold-v1" or not isinstance(
        selection, dict
    ):
        return report
    private = selection.get("paper_projection")
    if not isinstance(private, dict) or not isinstance(binding, dict):
        return report
    collection = report.get("collection") or {}
    retained = private.get("collection") or {}
    if not isinstance(collection, dict) or not isinstance(retained, dict):
        return report
    if (
        selection.get("schema") != REPORT_SCHEMA
        or selection.get("report_date") != report.get("date")
        or selection.get("revision") != binding.get("revision")
        or private.get("revision") != binding.get("revision")
        or selection.get("source") != binding.get("source")
        or not isinstance(binding.get("projection_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", binding["projection_sha256"])
        or selection.get("projection_sha256") != binding["projection_sha256"]
        or selection.get("cutoff") != report.get("cutoff")
        or any(
            private.get(key) != report.get(key)
            for key in ("date", "board_date", "board_version", "cutoff")
        )
        or any(
            retained.get(key) != collection.get(key)
            for key in ("targets", "target_evidence", "parent_board")
        )
    ):
        return report
    result = deepcopy(report)
    evidence = result.setdefault("collection", {}).setdefault("evidence", {})
    proofs = retained.get("evidence", {})
    if not isinstance(proofs, dict):
        return report
    for direction in SIGNALS:
        groups = result.setdefault(direction, [])
        hidden = private.get(direction, [])
        if not isinstance(hidden, list) or not all(isinstance(g, dict) for g in hidden):
            continue
        for index, group in enumerate(hidden):
            seat = group.get("seat")
            if (
                any(g.get("seat") == seat for g in groups)
                or sum(g.get("seat") == seat for g in hidden) != 1
                or not legacy_parent_seat(report, direction, group)
            ):
                continue
            old_prefix, new_prefix = (
                f"/{direction}/{index}/",
                f"/{direction}/{len(groups)}/",
            )
            mapped = {
                new_prefix + path[len(old_prefix) :]: proof
                for path, proof in proofs.items()
                if path.startswith(old_prefix)
            }
            if any(
                path in evidence and evidence[path] != proof
                for path, proof in mapped.items()
            ):
                continue
            groups.append(deepcopy(group))
            evidence.update(deepcopy(mapped))
    return result


def native_signal(selection, asset, group, when, proof, *, entry=True):
    """Read the existing selection-evidence row and its field references."""
    matching = [
        row
        for row in selection.get("instruments", [])
        if identity(row.get("id")) == identity(asset["instrument_id"])
        and row.get("code") == asset["code"]
        and row.get("group") == group["name"]
    ]
    if len(matching) != 1:
        return None
    (row,) = matching
    expected_day = when.isoformat()
    actual_days = row.get("actual_data_days", [row.get("data_day")])
    if (
        row.get("selected_new_position") is not entry
        or row.get("state") != ("已触发" if entry else "核实后未触发")
        or row.get("relationship") != "直属"
        or not text(row.get("market"))
        or actual_days != [expected_day]
        or row.get("data_day", expected_day) != expected_day
        or row.get("expected_day", row.get("expected_data_day")) != expected_day
        or row.get("expected_data_day", expected_day) != expected_day
        or not isinstance(proof, dict)
        or not text(proof.get("version"))
    ):
        return None
    refs, records = row.get("field_refs", {}), selection.get("field_records", {})
    if not isinstance(refs, dict) or not isinstance(records, dict):
        return None
    expected_identity = {
        "tmId": row["id"],
        "asset": row["market"],
        "tickerSymbol": row["code"],
        "asOfDate": when.isoformat(),
    }
    for field in SOURCE_FIELDS:
        record = records.get(refs.get(field), {})
        if (
            not isinstance(record, dict)
            or not same_raw_identity(record.get("identity"), expected_identity)
            or not isinstance(record.get("fields"), list)
            or field not in record["fields"]
            or not text(record.get("source"))
            or not text(record.get("location"))
            or not text(record.get("sha256"))
            or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
            or (
                ("version" in record or selection.get("schema") == REPORT_SCHEMA)
                and record.get("version") != proof["version"]
            )
        ):
            return None
    labels = row.get("labels")
    if isinstance(labels, str):
        labels = [
            label.strip()
            for label in re.split(r"[,，、;；\s]+", labels)
            if label.strip()
        ]
    return {
        "state": "verified",
        "identity": asset["instrument_id"],
        "market": asset["market"],
        "instrument_type": asset["instrument_type"],
        "data_date": when.isoformat(),
        "version": proof["version"],
        "source": "selection-evidence:field_records",
        "value": {
            "previous": row.get("previous"),
            "current": row.get("current"),
            "labels": labels,
            "action": "entry" if entry else "no_signal",
        },
    }


def same_raw_identity(actual, expected):
    return isinstance(actual, dict) and all(
        type(actual.get(key)) is type(value) and actual.get(key) == value
        for key, value in expected.items()
    )


def product_for(kind, market):
    product = kind.strip().lower()
    if product == "spot" and market in {"CRYPTO", "CC"}:
        return "crypto"
    return product


def read_candidate(
    asset, group, prefix, direction, report_day, collection, evidence, facts
):
    """Return a routed entry or its best established reason for skipping."""
    if prefix in facts.get("gaps", {}):
        return None, facts["gaps"][prefix]
    market, kind = asset.get("market"), asset.get("instrument_type")
    owner = identity(asset.get("instrument_id"))
    raw_code = asset.get("code")
    if (
        not owner
        or not text(raw_code)
        or not text(market)
        or not text(kind)
        or asset.get("lookup")
        or asset.get("direct_verified") is not True
        or asset.get("platform_match_verified") is not True
        or asset.get("watch_eligible") is not True
    ):
        return None, "eligibility_unverified"
    expected = target(collection, market, report_day)
    if expected is None or not is_opening_data_fresh(
        day(asset.get("data_date")), expected
    ):
        return None, "data_date_unverified"

    board_day = day(collection["targets"]["BOARD"])
    supplement = facts.get("instruments", {}).get(owner, {})
    product = product_for(kind, market)
    member = supplement.get("membership")
    if not receipt(member, owner, market, kind, board_day):
        return None, "membership_unverified"
    membership = member["value"]
    if (
        not isinstance(membership, dict)
        or membership.get("direct") is not True
        or type(membership.get("seat")) is not int
        or (
            membership.get("group"),
            membership.get("seat"),
            membership.get("direction"),
        )
        != (group["name"], group["seat"], direction)
        or member["version"] != facts["board_version"]
    ):
        return None, "membership_unverified"

    selection = facts.get("selection")
    signal = (
        native_signal(
            selection, asset, group, expected, evidence.get(f"{prefix}/trend")
        )
        if selection is not None
        else supplement.get("signal")
    )
    if not receipt(signal, owner, market, kind, expected):
        return None, "signal_unverified"
    event = SIGNALS[direction][2]
    observation = signal["value"]
    events = asset.get("signal_events")
    if (
        not isinstance(observation, dict)
        or observation.get("action") != "entry"
        or not observation_signal(
            direction, observation.get("previous"), observation.get("current"),
            observation.get("labels"),
        )
        or asset.get("trend") != observation.get("current")
        or not isinstance(events, list)
        or event not in events
        or not all(
            field(evidence, prefix, name, asset[name], owner, market, kind, expected)
            and evidence[f"{prefix}/{name}"]["version"] == signal["version"]
            for name in ("trend", "signal_events")
        )
    ):
        return None, "signal_unverified"

    route = supplement.get("route")
    if not receipt(route, owner, market, kind, expected) or not isinstance(
        route["value"], dict
    ):
        return None, "route_unverified"
    route = route["value"]
    if route.get("order_type") != "MARKET":
        return None, "market_order_unsupported"
    price, multiplier = (
        decimal(route.get("reference_price")),
        decimal(route.get("multiplier")),
    )
    lot, code, currency = route.get("lot"), route.get("code"), route.get("currency")
    prefix_code = "CC" if market == "CRYPTO" else market
    if market == "CN":
        prefix_code = route.get("quote_market")
        if prefix_code not in {"SH", "SZ"}:
            return None, "route_unverified"
    if (
        route.get("environment") != "SIMULATE"
        or not text(route.get("account"))
        or not text(code)
        or not code.startswith(f"{prefix_code}.")
        or not text(currency)
        or currency != asset.get("currency")
        or type(lot) is not int
        or lot < 1
        or not price
        or price <= 0
        or not multiplier
        or multiplier <= 0
    ):
        return None, "route_unverified"
    if product not in {"stock", "etf", "future", "crypto"}:
        return None, "route_unverified"
    if product == "etf" and (
        type(route.get("leverage")) is not int or route["leverage"] != 1
    ):
        return None, "etf_exposure_unverified"
    if product == "crypto" and (
        market not in {"CC", "CRYPTO"}
        or direction != "long"
        or (route.get("spot_verified") is not True)
    ):
        return None, "crypto_route_unverified"
    if market in {"CN", "SH", "SZ"}:
        if direction == "short":
            return None, "short_route_unsupported"
        if product == "etf" and route.get("simulated_a_share_etf_verified") is not True:
            return None, "etf_route_unverified"
    if product in {"stock", "etf"} and (
        multiplier != 1 or code != f"{prefix_code}.{raw_code}"
    ):
        return None, "route_unverified"
    if product == "crypto" and (
        multiplier != 1
        or (
            code != f"{prefix_code}.{raw_code}"
            and (
                route.get("ticker_mapping_verified") is not True
                or (identity(route.get("underlying_id")) != owner)
            )
        )
    ):
        return None, "crypto_route_unverified"
    if product == "future" and not tradable_contract(
        route, owner, raw_code, market, report_day
    ):
        return None, "future_contract_unverified"

    sessions = (
        {"sessions": {market: supplement["session"]}}
        if "session" in supplement
        else facts
    )
    session = opening(sessions, market, report_day, expected)
    if session is None:
        return None, "opening_unverified"
    strength = decimal(asset.get("strength"))
    if not field(
        evidence,
        prefix,
        "strength",
        asset.get("strength"),
        owner,
        market,
        kind,
        expected,
    ):
        strength = None
    return {
        "code": code,
        "instrument_id": owner,
        "market": market,
        "product": product,
        "economic_instrument": raw_code.upper() if product == "future" else code,
        "direction": direction,
        "signal": event,
        "signal_date": expected.isoformat(),
        "reference_price": str(price),
        "multiplier": str(multiplier),
        "lot": lot,
        "account": route["account"],
        "currency": currency,
        "order_type": "MARKET",
        "opens_at": session["opens_at"],
        "closes_at": session["closes_at"],
        "timezone": session["timezone"],
        "session_date": session["session_date"],
        "strength": strength,
    }, None


def candidates(report, facts):
    if reason := facts.get("report_gap"):
        return [], [{"reason": reason}]
    report = paper_report_projection(
        report, facts.get("selection"), facts.get("paper_binding")
    )
    report_day = day(report.get("date"))
    collection = report.get("collection") or {}
    evidence = collection.get("evidence") or {}
    if not report_day or not board_verified(report, collection, evidence, report_day):
        return [], [{"reason": "board_unverified"}]
    facts = {**facts, "board_version": report["board_version"]}
    rows, skipped = [], []
    daily = "daily_evidence_hash" in facts
    sections = ("direct",) if daily else ("direct", "searchable", "related")
    for direction in SIGNALS:
        groups = report.get(direction, [])
        verified_seats = set()
        for index, group in enumerate(groups):
            if not board_seat_verified(report, evidence, direction, index):
                if not daily:
                    skipped.append({"reason": "board_seat_unverified"})
                continue
            verified_seats.add(group["seat"])
            if f"/{direction}/{index}" in facts.get("coverage_gaps", ()):
                skipped.append({"reason": "coverage_unverified"})
            for section in sections:
                for offset, asset in enumerate(group.get(section, [])):
                    item, reason = read_candidate(
                        asset,
                        group,
                        f"/{direction}/{index}/{section}/{offset}",
                        direction,
                        report_day,
                        collection,
                        evidence,
                        facts,
                    )
                    if item is None:
                        skipped.append({"code": asset.get("code"), "reason": reason})
                    else:
                        rows.append(item)
        if daily:
            skipped.extend(
                {
                    "reason": "board_seat_unverified",
                    "direction": direction,
                    "seat": seat,
                }
                for seat in BOARD_SEATS
                if seat not in verified_seats
            )
    rows.sort(
        key=lambda row: (
            row["direction"],
            row["strength"] is None,
            (1 if row["direction"] == "short" else -1) * (row["strength"] or 0),
            row["code"],
        )
    )
    unique = {}
    conflicts = set()
    for row in rows:
        row.pop("strength")
        key = (row["market"], row["product"], row["code"])
        if key in unique and unique[key] != row:
            conflicts.add(key)
        unique.setdefault(key, row)
    for key in conflicts:
        skipped.append({"code": key[-1], "reason": "identity_conflict"})
    return [row for key, row in unique.items() if key not in conflicts], skipped
