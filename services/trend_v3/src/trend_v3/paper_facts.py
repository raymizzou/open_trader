"""Produce planning facts from retained provider records and read-only OpenD."""

import re
from copy import deepcopy
from datetime import datetime, time
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from .paper_authority import private_bytes, strict_json
from .paper_evidence import (
    REPORT_SCHEMA,
    SIGNALS,
    board_seat_verified,
    board_verified,
    day,
    decimal,
    field,
    identity,
    native_signal,
    observation_signal,
    product_for,
    receipt,
    same_raw_identity,
    target,
    text,
)
from .paper_futu import BrokerUnavailable, integer
from .paper_report import normalize_field_sources

# Futu reports the session date and day type. Regular exchange hours apply
# only to those exact day types. Unsupported/multi-session products stay pending.
EQUITIES = {
    "US": ("America/New_York", "USD", time(9, 30), time(16), time(13)),
    "HK": ("Asia/Hong_Kong", "HKD", time(9, 30), time(16), time(12)),
    "SH": ("Asia/Shanghai", "CNY", time(9, 30), time(15), None),
    "SZ": ("Asia/Shanghai", "CNY", time(9, 30), time(15), None),
}
ASSETS = {
    ("US", "stock"): {"美国股票", "美股"},
    ("US", "etf"): {"美国ETF", "美股ETF"},
    ("HK", "stock"): {"香港股票", "港股"},
    ("HK", "etf"): {"香港ETF", "港股ETF"},
    ("CN", "stock"): {"A股股票", "A股", "中国A股"},
    ("CN", "etf"): {"A股ETF", "中国ETF"},
    ("US", "future"): {"美国期货", "美国股指期货", "美国商品期货"},
    ("HK", "future"): {"香港期货", "香港股指期货"},
    ("CRYPTO", "crypto"): {"加密现货", "加密货币"},
}
LOCATION = re.compile(r"\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])*")
PATH_PARTS = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+)\]")
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,30}", re.IGNORECASE | re.ASCII)
HSI_HOURS = "(09:15-12:00),(13:00-16:30),(17:00-03:00)"
HSI_MORNING = (time(9, 15), time(12))
HSI_AFTERNOON = (time(13), time(16, 30))
TEMPERATURES = ("冻", "寒", "凉", "平", "温", "热", "沸")


class FactsMissing(ValueError):
    """A specific evidence gap makes an entry ineligible."""


def location_value(document, location):
    if not LOCATION.fullmatch(location or ""):
        raise FactsMissing("raw_signal_unverified")
    for key, index in PATH_PARTS.findall(location):
        document = document[key] if key else document[int(index)]
    if not isinstance(document, dict):
        raise FactsMissing("raw_signal_unverified")
    return document


class RawEvidence:
    def __init__(self, root, selection):
        self.root = root
        self.records = selection.get("field_records") or {}
        self.cache = {}

    def read(self, selected, field):
        proof = self.records.get(selected.get("field_refs", {}).get(field), {})
        source = Path(proof.get("source") or "")
        if (
            source.is_absolute()
            or len(source.parts) < 2
            or ".." in source.parts
            or not day(source.parts[0])
            or field not in proof.get("fields", [])
        ):
            raise FactsMissing("raw_signal_unverified")
        path = self.root / source
        if path not in self.cache:
            data = private_bytes(path)
            self.cache[path] = (sha256(data).hexdigest(), strict_json(data))
        digest, document = self.cache[path]
        if digest != proof.get("sha256"):
            raise FactsMissing("raw_signal_unverified")
        value = location_value(document, proof.get("location"))
        if not same_raw_identity(value, proof["identity"]):
            raise FactsMissing("raw_signal_unverified")
        if field not in value:
            raise FactsMissing("raw_signal_unverified")
        if "record" in proof and (
            field not in proof["record"]
            or type(value[field]) is not type(proof["record"][field])
            or value[field] != proof["record"][field]
        ):
            raise FactsMissing("raw_signal_unverified")
        return value[field]

    def signal(self, selected):
        fields = ("trendTemperaturePrev", "trendTemperatureCurr", "tickerLabels")
        info = ("previous", "current", "labels")
        for key, raw_field in zip(info, fields, strict=True):
            if self.read(selected, raw_field) != selected.get(key):
                raise FactsMissing("raw_signal_unverified")


def proof(asset, data_day, version, value, source):
    return {
        "identity": asset["instrument_id"],
        "data_date": data_day.isoformat(),
        "market": asset["market"],
        "instrument_type": asset["instrument_type"],
        "version": version,
        "state": "verified",
        "value": value,
        "source": source,
    }


def direct_selection(selection, asset, group):
    rows = [
        row
        for row in selection.get("instruments", [])
        if identity(row.get("id")) == identity(asset.get("instrument_id"))
        and row.get("code") == asset.get("code")
        and row.get("group") == group.get("name")
    ]
    if len(rows) == 1 and rows[0].get("relationship") == "直属":
        return rows[0]
    raise FactsMissing("membership_unverified")


def group_coverage_verified(report, group, group_path):
    expected = day(report.get("board_date"))
    evidence = report["collection"]["evidence"]
    coverage = group.get("coverage")
    return (
        isinstance(coverage, dict)
        and coverage.get("state") == "complete"
        and isinstance(coverage.get("instrument_ids"), list)
        and all(
            field(
                evidence,
                group_path,
                name,
                group.get(name),
                group.get("name"),
                "BOARD",
                None,
                expected,
            )
            and evidence[f"{group_path}/{name}"]["version"] == report["board_version"]
            for name in ("coverage", "board_strength")
        )
    )


def verify_membership(report, asset, group, group_path):
    if not group_coverage_verified(report, group, group_path) or identity(
        asset["instrument_id"]
    ) not in {identity(member) for member in group["coverage"]["instrument_ids"]}:
        raise FactsMissing("membership_unverified")


def coverage_matches_projection(group):
    sections = [group.get(name, []) for name in ("direct", "searchable", "related")]
    if not all(isinstance(assets, list) for assets in sections):
        return False
    members = [
        identity(asset.get("instrument_id")) if isinstance(asset, dict) else None
        for assets in sections
        for asset in assets
    ]
    covered = [identity(member) for member in group["coverage"]["instrument_ids"]]
    return all(members) and len(set(members)) == len(members) and covered == members


def session_time(broker, code, quote_market, report_day, data_day, now, *, future=None):
    if future is None:
        timezone, currency, opens, closes, half_close = EQUITIES[quote_market]
    else:
        timezone, currency = future["timezone"], future["currency"]
    stamp = report_day.isoformat()
    days = broker.planning_quote(
        "request_trading_days", code=code, start=stamp, end=stamp
    )
    if not days:
        raise FactsMissing("market_no_session")
    if len(days) != 1 or days[0].get("time") != stamp:
        raise FactsMissing("opening_unverified")
    kind = days[0].get("trade_date_type")
    if future is not None:
        sitting = future["sittings"].get(kind)
        if sitting is None:
            raise FactsMissing("future_session_unverified")
        opens, closes = sitting
    elif kind == "MORNING" and half_close:
        closes = half_close
    elif kind != "WHOLE":
        raise FactsMissing("opening_unverified")
    zone = ZoneInfo(timezone)
    opening = datetime.combine(report_day, opens, zone)
    closing = datetime.combine(report_day, closes, zone)
    if opening >= closing:
        raise FactsMissing("opening_unverified")
    session = {
        "state": "verified",
        "source": "OpenD request_trading_days and regular exchange hours",
        "market": quote_market,
        "report_date": stamp,
        "session_date": stamp,
        "data_date": data_day.isoformat(),
        "opens_at": opening.isoformat(),
        "closes_at": closing.isoformat(),
        "timezone": timezone,
        "session": "regular",
    }
    if now >= opening:
        raise FactsMissing("missed_open")
    return session, currency


def exact_row(rows, code, reason):
    if len(rows) != 1 or rows[0].get("code") != code:
        raise FactsMissing(reason)
    return rows[0]


def quotes(
    broker, code, quote_market, product, direction, when, zone, now, *, future=None
):
    kind = {"stock": "STOCK", "etf": "ETF", "future": "FUTURE"}[product]
    basic = exact_row(
        broker.planning_quote(
            "get_stock_basicinfo",
            market=quote_market,
            stock_type=kind,
            code_list=[code],
        ),
        code,
        "product_unverified",
    )
    if (
        basic.get("stock_type") != kind
        or (basic.get("suspension") is not False and basic.get("suspension") != "N/A")
        or basic.get("delisting") is not False
        or (product == "future" and basic.get("main_contract") is not False)
    ):
        raise FactsMissing("product_unverified")
    snapshot = exact_row(
        broker.planning_quote("get_market_snapshot", code_list=[code]),
        code,
        "reference_price_unverified",
    )
    if (
        snapshot.get("suspension") is not False
        or snapshot.get("sec_status") != "NORMAL"
    ):
        raise FactsMissing("market_status_unverified")
    try:
        stamped = datetime.fromisoformat(snapshot.get("update_time") or "")
    except ValueError as error:
        raise FactsMissing("reference_price_unverified") from error
    local = (
        stamped.replace(tzinfo=zone)
        if stamped.tzinfo is None
        else stamped.astimezone(zone)
    )
    if local > now or local.date() not in {when, now.astimezone(zone).date()}:
        raise FactsMissing("reference_price_unverified")
    price = decimal(snapshot.get("last_price"))
    lot = integer(basic.get("lot_size"))
    if not price or price <= 0:
        raise FactsMissing("reference_price_unverified")
    expiry = None
    if future is not None:
        expiry = verify_future_quote(
            basic, snapshot, lot, future, now.astimezone(zone).date()
        )
        lot = 1
    elif not lot or lot < 1 or lot != integer(snapshot.get("lot_size")):
        raise FactsMissing("reference_price_or_lot_unverified")
    if direction == "short" and product != "future":
        borrow = integer(snapshot.get("short_available_volume"))
        if snapshot.get("enable_short_sell") is not True or not borrow or borrow < lot:
            raise FactsMissing("short_capacity_unverified")
    return price, lot, expiry


def verify_future_quote(basic, snapshot, native_lot, future, report_day):
    expiry_dates = {
        day(value[:10])
        for value in (
            future["last_trade_time"],
            basic.get("last_trade_time"),
            snapshot.get("future_last_trade_time"),
        )
        if isinstance(value, str) and value not in {"", "N/A"}
    }
    if len(expiry_dates) != 1:
        raise FactsMissing("future_expiry_unverified")
    (expiry,) = expiry_dates
    if expiry is None or expiry < report_day:
        raise FactsMissing("future_expiry_unverified")
    expected = future["native_lot"]
    if native_lot != expected or integer(snapshot.get("lot_size")) != expected:
        raise FactsMissing("future_quantity_unit_unverified")
    return expiry


def future_session_terms(info, market, root, size):
    currency = info.get("price_currency")
    trade_time = info.get("trade_time") or ""
    zone = info.get("time_zone")
    if market == "HK" and root == "HSI" and info.get("type") == "股指期货":
        if size != 50:
            raise FactsMissing("future_quantity_unit_unverified")
        if currency not in {"HKD", "港币", "港幣"}:
            raise FactsMissing("future_currency_unverified")
        if (
            zone not in {"CCT", "Asia/Hong_Kong"}
            or re.sub(r"\s+", "", trade_time) != HSI_HOURS
        ):
            raise FactsMissing("future_session_unverified")
        return (
            "HKD",
            "Asia/Hong_Kong",
            {
                "WHOLE": HSI_MORNING,
                "MORNING": HSI_MORNING,
                "AFTERNOON": HSI_AFTERNOON,
            },
            size,
        )
    if market != "US":
        raise FactsMissing("future_session_unverified")
    if currency != "USD":
        raise FactsMissing("future_currency_unverified")
    trading = re.fullmatch(r"([0-9]{2}:[0-9]{2})\s*-\s*([0-9]{2}:[0-9]{2})", trade_time)
    if not trading or zone != "America/New_York":
        raise FactsMissing("future_session_unverified")
    return (
        "USD",
        zone,
        {
            "WHOLE": (time.fromisoformat(trading[1]), time.fromisoformat(trading[2])),
        },
        1,
    )


def actual_future(broker, raw, selected, asset, report_day):
    try:
        mapped = raw.read(selected, "brokerContract")
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise FactsMissing("future_contract_unverified") from error
    if not isinstance(mapped, dict):
        raise FactsMissing("future_contract_unverified")
    code, root, month = (mapped.get(key) for key in ("code", "root", "delivery_month"))
    market = asset["market"]
    if (
        not isinstance(code, str)
        or not code.startswith(market + ".")
        or not root
        or not month
        or mapped.get("underlying") != asset["code"]
        or day(mapped.get("as_of")) != report_day
    ):
        raise FactsMissing("future_contract_unverified")
    info = exact_row(
        broker.planning_quote("get_future_info", code_list=[code]),
        code,
        "future_contract_unverified",
    )
    size = decimal(info.get("size"))
    if not size or size <= 0:
        raise FactsMissing("future_specification_unverified")
    expected_owner = mapped.get("broker_owner", mapped["underlying"])
    if (
        not isinstance(expected_owner, str)
        or info.get("owner") != expected_owner
        or (market == "HK" and root == "HSI" and expected_owner != "HK.800000")
    ):
        raise FactsMissing("future_underlying_unverified")
    currency, timezone, sittings, native_lot = future_session_terms(
        info, market, root, size
    )
    return {
        "code": code,
        "multiplier": str(size),
        "currency": currency,
        "timezone": timezone,
        "sittings": sittings,
        "native_lot": native_lot,
        "last_trade_time": info.get("last_trade_time"),
        "route": {
            "contract_root": root,
            "delivery_month": month,
            "underlying_id": asset["instrument_id"],
            "tradable_contract_verified": True,
        },
    }


def build_route(
    broker, raw, selected, asset, product, direction, report_day, when, now
):
    market = asset["market"]
    symbol = asset["code"]
    future = None
    extra = {}
    if product == "crypto":
        raise FactsMissing("crypto_route_unverified")
    if product not in {"stock", "etf", "future"} or not SYMBOL.fullmatch(symbol):
        raise FactsMissing("route_unverified")
    if product == "future":
        future = actual_future(broker, raw, selected, asset, report_day)
        extra = future["route"]
    quote_market = market
    if market == "CN":
        if direction == "short":
            raise FactsMissing("short_route_unsupported")
        if product == "etf":
            raise FactsMissing("a_share_etf_route_unverified")
        if re.fullmatch(r"6[0-9]{5}", symbol):
            quote_market = "SH"
        elif re.fullmatch(r"[03][0-9]{5}", symbol):
            quote_market = "SZ"
        else:
            raise FactsMissing("route_unverified")
        extra["quote_market"] = quote_market
    if product == "etf":
        if (
            type(leverage := raw.read(selected, "etfLeverage")) is not int
            or leverage != 1
        ):
            raise FactsMissing("etf_exposure_unverified")
        extra["leverage"] = 1
    if future is None and quote_market not in EQUITIES:
        raise FactsMissing("opening_unverified")
    code = future["code"] if future else f"{quote_market}.{symbol}"
    session, currency = session_time(
        broker,
        code,
        quote_market,
        report_day,
        when,
        now,
        future=future,
    )
    session["market"] = market
    zone = ZoneInfo(session["timezone"])
    price, lot, expiry = quotes(
        broker,
        code,
        quote_market,
        product,
        direction,
        when,
        zone,
        now,
        future=future,
    )
    if expiry == report_day and market == "HK":
        close = datetime.fromisoformat(session["closes_at"])
        expiry_close = datetime.combine(report_day, time(16), zone)
        session["closes_at"] = min(close, expiry_close).isoformat()
    if asset.get("currency") not in (None, currency):
        raise FactsMissing("currency_unverified")
    multiplier = future["multiplier"] if future else "1"
    account = broker.planning_account(quote_market, product, currency, code, multiplier)
    if not account:
        raise FactsMissing("simulate_account_unavailable")
    route = {
        **extra,
        "code": code,
        "currency": currency,
        "account": account,
        "lot": lot,
        "multiplier": multiplier,
        "reference_price": str(price),
        "order_type": "MARKET",
        "environment": "SIMULATE",
    }
    return route, session


def produce(report, selection, info, evidence_root, broker, now):
    """Only ask OpenD about assets with report, membership and raw signal evidence."""
    report = deepcopy(report)
    selection = normalize_field_sources(selection, report["date"])
    digest = sha256(
        (info["projection_sha256"] + info["selection_sha256"]).encode()
    ).hexdigest()
    facts = {
        "report_version": info["revision"],
        "report_revision": int(info["revision"][1:]),
        "selection": selection,
        "instruments": {},
        "gaps": {},
        "coverage_gaps": [],
        "daily_evidence_hash": digest,
    }
    collection = report.get("collection") or {}
    evidence = collection.get("evidence") or {}
    report_day = day(report["date"])
    board = selection.get("board")
    if (
        not board_verified(report, collection, evidence, report_day)
        or (
            "board" in selection
            and (
                not isinstance(board, dict)
                or board.get("board_date") != report["board_date"]
                or board.get("edition") != report["board_version"]
            )
        )
        or (
            selection.get("schema") == REPORT_SCHEMA
            and target(collection, "BOARD", report_day) != day(report["board_date"])
        )
    ):
        facts["report_gap"] = "board_unverified"
        return report, facts
    raw = RawEvidence(evidence_root, selection)
    for direction in SIGNALS:
        groups = report.get(direction, [])
        for number, group in enumerate(groups):
            if not board_seat_verified(report, evidence, direction, number):
                continue
            group_path = f"/{direction}/{number}"
            if group_coverage_verified(report, group, group_path):
                coverage_gap = not coverage_matches_projection(group)
            else:
                # Direct cards already report their unverified membership individually.
                coverage_gap = not group.get("direct")
            if coverage_gap:
                facts["coverage_gaps"].append(group_path)
            for offset, asset in enumerate(group.get("direct", [])):
                if (
                    not all(
                        asset.get(flag) is True
                        for flag in ("direct_verified", "platform_match_verified")
                    )
                    or type(asset.get("watch_eligible")) is not bool
                    or asset.get("lookup")
                    or not identity(asset.get("instrument_id"))
                    or not all(
                        text(asset.get(name))
                        for name in ("code", "market", "instrument_type")
                    )
                ):
                    continue
                path = f"{group_path}/direct/{offset}"
                try:
                    record_facts(
                        report, facts, raw, broker, group, asset, path, direction, now
                    )
                except FactsMissing as error:
                    facts["gaps"][path] = str(error)
                except (OSError, KeyError, IndexError, ValueError, TypeError):
                    facts["gaps"][path] = "raw_or_route_evidence_unavailable"
                except BrokerUnavailable:
                    facts["gaps"][path] = "paper_market_facts_unavailable"
    return report, facts


def record_facts(report, facts, raw, broker, group, asset, path, direction, now):
    collection, report_day = report["collection"], day(report["date"])
    evidence = collection["evidence"]
    verify_membership(report, asset, group, path.split("/direct/")[0])
    selected = direct_selection(facts["selection"], asset, group)
    market, kind = asset["market"], asset["instrument_type"]
    product = product_for(kind, market)
    source_market = quote_market_for_source(market)
    if selected.get("market") not in ASSETS.get((source_market, product), set()):
        raise FactsMissing("identity_market_unverified")
    when = target(collection, market, report_day)
    if when is None or day(asset.get("data_date")) != when:
        raise FactsMissing("data_date_unverified")
    owner = identity(asset["instrument_id"])
    entry = asset["watch_eligible"]
    signal = native_signal(
        facts["selection"],
        asset,
        group,
        when,
        evidence.get(path + "/trend"),
        entry=entry,
    )
    if not receipt(signal, owner, market, kind, when) or any(
        not field(evidence, path, name, asset.get(name), owner, market, kind, when)
        or evidence[path + "/" + name]["version"] != signal["version"]
        for name in ("trend", "signal_events")
    ):
        raise FactsMissing("signal_unverified")
    event = SIGNALS[direction][2]
    observation = signal["value"]
    actual = (observation["previous"], observation["current"])
    labels, events = observation["labels"], asset.get("signal_events")
    if (
        any(value not in TEMPERATURES for value in actual)
        or asset.get("trend") != actual[1]
        or not isinstance(labels, list)
        or not all(text(label) for label in labels)
        or not isinstance(events, list)
        # Check the transition independently so missing labels cannot certify no-entry.
        or observation_signal(direction, *actual, [event]) != entry
        or (event in labels) != entry
        or (event in events) != entry
    ):
        raise FactsMissing("signal_unverified")
    raw.signal(selected)
    if not entry:
        facts["gaps"][path] = "verified_no_signal"
        return
    route, session = build_route(
        broker,
        raw,
        selected,
        asset,
        product,
        direction,
        report_day,
        when,
        now,
    )
    member = {
        "group": group["name"],
        "seat": group["seat"],
        "direction": direction,
        "direct": True,
    }
    asset["currency"] = route["currency"]
    facts["instruments"][owner] = {
        "membership": proof(
            asset,
            day(report["board_date"]),
            report["board_version"],
            member,
            "verified daily direct card and original coverage proof",
        ),
        "route": proof(
            asset,
            when,
            signal["version"],
            route,
            "OpenD route, product, quote, lot and simulation account readback",
        ),
        "session": session,
    }


def quote_market_for_source(market):
    if market in {"SH", "SZ"}:
        return "CN"
    if market == "CC":
        return "CRYPTO"
    return market
