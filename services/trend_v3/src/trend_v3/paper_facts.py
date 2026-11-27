"""Produce planning facts from retained provider records and read-only OpenD."""

import re
from copy import deepcopy
from datetime import datetime, time
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from .paper_authority import private_bytes, strict_json
from .paper_evidence import (
    BRIDGE_MARKETS,
    REPORT_SCHEMA,
    SIGNALS,
    TAIPEI,
    a_share_symbol,
    board_seat_verified,
    board_verified,
    bridge_observations,
    day,
    decimal,
    field,
    identity,
    instant,
    native_signal,
    observation_signal,
    paper_report_projection,
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


def session_time(
    broker,
    code,
    quote_market,
    report_day,
    data_day,
    now,
    *,
    future=None,
    allow_late=False,
):
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
    if now >= opening and (not allow_late or now.astimezone(zone).date() != report_day):
        raise FactsMissing("missed_open")
    return session, currency


def exact_row(rows, code, reason):
    if len(rows) != 1 or rows[0].get("code") != code:
        raise FactsMissing(reason)
    return rows[0]


def quotes(
    broker,
    code,
    quote_market,
    product,
    direction,
    when,
    zone,
    now,
    *,
    future=None,
    bridge=False,
    live=False,
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
    if (bridge or live) and basic.get("suspension") is True:
        raise FactsMissing("market_halted")
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
    if (bridge or live) and (
        snapshot.get("suspension") is True or snapshot.get("sec_status") == "SUSPENDED"
    ):
        raise FactsMissing("market_halted")
    normal = {"NORMAL"}
    if live:
        normal.update({"AFTER_COMBINATION", "AFTER_TRANSACTION"})
    if (
        snapshot.get("suspension") is not False
        or snapshot.get("sec_status") not in normal
    ):
        raise FactsMissing("market_status_unverified")
    try:
        stamped = datetime.fromisoformat(snapshot.get("update_time") or "")
    except (TypeError, ValueError) as error:
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


def late_execution_terms(broker, line, now):
    """Recheck the frozen instrument and price the full approved quantity today."""
    code, product = line["code"], line["product"]
    market = code.split(".", 1)[0]
    trade_date = day(line["session_date"])
    zone = ZoneInfo(line["timezone"])
    future = None
    try:
        if product == "future":
            info = exact_row(
                broker.planning_quote("get_future_info", code_list=[code]),
                code,
                "future_contract_unverified",
            )
            expected_owner = line.get("contract_owner") or (
                "HK.800000" if market == "HK" else line["economic_instrument"]
            )
            size = decimal(info.get("size"))
            if (
                not isinstance(info.get("owner"), str)
                or info["owner"].upper() != expected_owner.upper()
                or size != decimal(line["multiplier"])
            ):
                raise FactsMissing("future_specification_unverified")
            currency, zone_name, sittings, native_lot = future_session_terms(
                info,
                market,
                "HSI" if market == "HK" and code.startswith("HK.HSI") else "",
                size,
            )
            if currency != line["currency"] or zone_name != line["timezone"]:
                raise FactsMissing("future_session_unverified")
            future = {
                "timezone": zone_name,
                "currency": currency,
                "sittings": sittings,
                "native_lot": native_lot,
                "last_trade_time": info.get("last_trade_time"),
            }
        session, currency = session_time(
            broker,
            code,
            market,
            trade_date,
            day(line["signal_date"]),
            now,
            future=future,
            allow_late=True,
        )
        price, lot, expiry = quotes(
            broker,
            code,
            market,
            product,
            line["direction"],
            trade_date,
            zone,
            now,
            future=future,
            live=True,
        )
        closes = instant(session["closes_at"])
        if future and expiry == trade_date:
            if market == "HK":
                closes = min(closes, datetime.combine(trade_date, time(16), zone))
            last = datetime.fromisoformat(future["last_trade_time"])
            if now >= min(
                closes, last.replace(tzinfo=zone) if last.tzinfo is None else last
            ):
                raise FactsMissing("future_expiry_unverified")
        if (
            currency != line["currency"]
            or instant(session["opens_at"]) != instant(line["opens_at"])
            or closes != instant(line["closes_at"])
        ):
            raise FactsMissing("opening_unverified")
        if lot != line["lot"]:
            raise FactsMissing("reference_price_or_lot_unverified")
    except FactsMissing as error:
        return None, str(error)
    except (BrokerUnavailable, KeyError, OSError, TypeError, ValueError):
        return None, "execution_conditions_unverified"
    return line | {"reference_price": str(price)}, None


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
            "contract_owner": expected_owner,
            "delivery_month": month,
            "underlying_id": asset["instrument_id"],
            "tradable_contract_verified": True,
        },
    }


def build_route(
    broker,
    raw,
    selected,
    asset,
    product,
    direction,
    report_day,
    when,
    now,
    *,
    bridge=False,
    allow_late=False,
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
    if market == "CN" or (bridge and market in {"SH", "SZ"}):
        if direction == "short":
            raise FactsMissing("short_route_unsupported")
        if product == "etf":
            raise FactsMissing("a_share_etf_route_unverified")
        if bridge:
            quote_market, symbol = a_share_symbol(market, symbol, explicit=True)
            if quote_market is None:
                raise FactsMissing("route_unverified")
        elif re.fullmatch(r"6[0-9]{5}", symbol):
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
    if bridge and future is None:
        timezone, _, usual_open, _, _ = EQUITIES[quote_market]
        # Supported whole and half sessions share their opening time.
        zone = ZoneInfo(timezone)
        if now >= datetime.combine(report_day, usual_open, zone) and (
            not allow_late or now.astimezone(zone).date() != report_day
        ):
            raise FactsMissing("missed_open")
    session, currency = session_time(
        broker,
        code,
        quote_market,
        report_day,
        when,
        now,
        future=future,
        allow_late=allow_late,
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
        bridge=bridge,
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


def produce(report, selection, info, evidence_root, broker, now, *, allow_late=False):
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
        "paper_binding": {
            key: info.get(key) for key in ("revision", "source", "projection_sha256")
        },
        "instruments": {},
        "gaps": {},
        "coverage_gaps": [],
        "daily_evidence_hash": digest,
    }
    observations = retained_bridge(report, selection, info, evidence_root)
    if observations is not None:
        if now < instant(info["cutoff"]):
            raise ValueError("daily observation evidence unverified")
        facts["daily_bridge_routes"] = preflight_observations(
            observations,
            report,
            selection,
            evidence_root,
            broker,
            now,
            allow_late=allow_late,
        )
        if all(row["state"] == "rejected" for row in observations):
            facts["daily_bridge_coverage_gap"] = bridge_coverage_gap(
                report, observations
            )
        return report, facts
    report = paper_report_projection(report, selection, info)
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
                        report,
                        facts,
                        raw,
                        broker,
                        group,
                        asset,
                        path,
                        direction,
                        now,
                        allow_late=allow_late,
                    )
                except FactsMissing as error:
                    facts["gaps"][path] = str(error)
                except (OSError, KeyError, IndexError, ValueError, TypeError):
                    facts["gaps"][path] = "raw_or_route_evidence_unavailable"
                except BrokerUnavailable:
                    facts["gaps"][path] = "paper_market_facts_unavailable"
    return report, facts


def bridge_coverage_gap(report, observations):
    """Prove the discovery scope only when reporting an absence of candidates."""
    collection = report.get("collection") or {}
    parent = collection.get("parent_board") or {}
    rows = parent.get("groups")
    report_day = day(report.get("date"))
    observed, cutoff = instant(parent.get("observed_at")), instant(report.get("cutoff"))
    edition = report.get("board_version")
    if (
        parent.get("state") != "verified"
        or parent.get("complete") is not True
        or not text(parent.get("source"))
        or not text(edition)
        or parent.get("version") != edition
        or observed is None
        or cutoff is None
        or observed > cutoff
        or observed.astimezone(TAIPEI).date() != report_day
        or not isinstance(rows, list)
        or not rows
        or type(parent.get("row_count")) is not int
        or parent["row_count"] != len(rows)
    ):
        return "daily_board_coverage_unverified"
    if report.get("selection_mode") != "threshold-v1":
        return "daily_class_coverage_unverified"

    expected_classes = {"short": {}, "long": {}}
    common_day = target(collection, "BOARD", report_day)
    if common_day != day(parent.get("data_date")) or common_day != day(
        report.get("board_date")
    ):
        common_day = None
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            return "daily_board_coverage_unverified"
        name, score = row.get("name"), decimal(row.get("strength"))
        if not text(name) or name in seen or score is None or not 0 <= score <= 100:
            return "daily_board_coverage_unverified"
        seen.add(name)
        market = row.get("market")
        expected_day = (
            target(collection, market, report_day) if text(market) else common_day
        )
        actual_day = day(row.get("row_date", parent.get("data_date")))
        if expected_day is None or actual_day != expected_day:
            return "daily_target_coverage_unverified"
        if score >= 80:
            expected_classes["long"][name] = (score, actual_day)
        elif score <= 20:
            expected_classes["short"][name] = (score, actual_day)

    evidence = collection.get("evidence") or {}
    for direction, expected in expected_classes.items():
        groups = report.get(direction, [])
        actual = {
            group.get("name"): decimal(group.get("board_strength")) for group in groups
        }
        if len(actual) != len(groups) or actual != {
            name: value[0] for name, value in expected.items()
        }:
            return "daily_class_coverage_unverified"
        for index, group in enumerate(groups):
            name = group["name"]
            _, when = expected[name]
            coverage = group.get("coverage")
            path = f"/{direction}/{index}/coverage"
            if (
                not isinstance(coverage, dict)
                or coverage.get("state") != "complete"
                or not isinstance(coverage.get("instrument_ids"), list)
                or not coverage_matches_projection(group)
                or not field(
                    evidence,
                    f"/{direction}/{index}",
                    "coverage",
                    coverage,
                    name,
                    "BOARD",
                    None,
                    when,
                )
                or evidence[path]["version"] != edition
            ):
                return "daily_member_coverage_unverified"
            markets = set(collection.get("targets", {})) - {"BOARD"}
            members = [
                asset
                for section in ("direct", "searchable", "related")
                for asset in group.get(section, [])
            ]
            for asset in members:
                market = asset.get("market")
                if not text(market) or day(asset.get("data_date")) != target(
                    collection, market, report_day
                ):
                    return "daily_target_coverage_unverified"
                markets.add(market)
            if any(
                target(collection, market, report_day) is None for market in markets
            ):
                return "daily_target_coverage_unverified"
    if any(
        day(row.get("target_day")) is None
        or day(row.get("actual_day")) != day(row["target_day"])
        for row in observations
    ):
        return "daily_target_coverage_unverified"
    return None


def retained_bridge(report, selection, info, root):
    """Recheck the raw sources before using the frozen decision."""
    try:
        rows = bridge_observations(report, selection, info)
        if rows is None:
            return None
        checked = {}
        originals, documents = {}, {}
        for row in rows:
            row_sources = {}
            for source in row["sources"]:
                path = Path(source["path"])
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or len(path.parts) < 3
                    or path.parts[0] != report["date"]
                    or path.parts[1] not in {info["revision"], "frames"}
                ):
                    raise ValueError("invalid observation source path")
                if path not in checked:
                    original = private_bytes(root / path)
                    checked[path] = sha256(original).hexdigest()
                    originals[path] = original
                if checked[path] != source["sha256"]:
                    raise ValueError("observation source digest mismatch")
                row_sources[path] = source
            if row["state"] == "qualified":
                for field in ("current", "labels"):
                    read_observation_source(
                        row, field, row_sources, originals, documents, info
                    )
                if "previous" in row["facts"]:
                    read_observation_source(
                        row, "previous", row_sources, originals, documents, info
                    )
        return rows
    except (AttributeError, IndexError, KeyError, OSError, TypeError, ValueError):
        raise ValueError("daily observation evidence unverified") from None


def read_observation_source(row, field, row_sources, originals, documents, info=None):
    proof = row["facts"][field]
    source, locator = proof["source"].split("#", 1)
    path = Path(source)
    if path not in row_sources:
        raise ValueError("own observation source is unavailable")
    if (
        "locations" in row_sources[path]
        and locator not in row_sources[path]["locations"]
    ):
        raise ValueError("own observation location is unavailable")
    location, _, native_field = locator.partition("#")
    if not location.startswith("$"):
        location = "$." + location
    if path.parts[1] == "frames":
        raise ValueError("a frame cannot supply a structured field directly")
    if path not in documents:
        documents[path] = strict_json(originals[path])
    record = location_value(documents[path], location)
    subject = record.get("tmId", record.get("identity", record.get("instrument_id")))
    market = record.get("asset", record.get("market"))
    code = record.get("tickerSymbol", record.get("code"))
    quote_day = record.get("asOfDate", record.get("data_date"))
    if (
        identity(subject) != identity(row["identity"])
        or BRIDGE_MARKETS.get(market, market)
        != BRIDGE_MARKETS.get(row["market"], row["market"])
        or code != row["code"]
        or quote_day != row["target_day"]
        or record.get("state", "verified") != "verified"
        or record.get("verified") is False
    ):
        raise ValueError("own observation identity is unverified")
    if location.startswith("$.certified_intersections["):
        observed = instant(record.get("observed_at"))
        if (
            path.name != "signal-evidence-index.json"
            or observed is None
            or info is None
            or observed > instant(info["cutoff"])
            or record.get("removed") is True
        ):
            raise ValueError("the visual intersection is outside the report scope")
        for kind in (
            "detail_source",
            "class_page_source",
            "signal_row_source",
            "exact_search_source",
        ):
            frame = Path(record[kind].split("#", 1)[0])
            if (
                frame.parts[:1] != ("frames",)
                or Path(row["report_date"]) / frame not in row_sources
            ):
                raise ValueError("the visual intersection frame is unavailable")
    fields = {
        "previous": ("trendTemperaturePrev", "previous"),
        "current": ("trendTemperatureCurr", "temperature", "current"),
        "labels": ("tickerLabels", "labels"),
        "etfLeverage": ("etfLeverage", "leverage"),
        "brokerContract": ("brokerContract",),
    }[field]
    if native_field and native_field not in fields:
        raise ValueError("own observation field is unverified")
    reported = [
        record[key]
        for key in fields
        if key in record and (not native_field or key == native_field)
    ]
    if field == "labels":
        reported = [
            [label for label in re.split(r"[,，;；、\s]+", value) if label]
            if isinstance(value, str)
            else value
            for value in reported
        ]
    if not reported or any(
        type(value) is not type(proof["value"]) or value != proof["value"]
        for value in reported
    ):
        raise ValueError("own observation field is unverified")
    return proof["value"]


class ObservationRouteFacts:
    """Execution specifications retained alongside the observation."""

    def __init__(self, root, row):
        self.root = root
        self.row = row

    def read(self, _selected, field):
        if field not in self.row["facts"]:
            raise FactsMissing(
                "etf_exposure_unverified"
                if field == "etfLeverage"
                else "future_contract_unverified"
            )
        proof = self.row["facts"][field]
        path = Path(proof["source"].split("#", 1)[0])
        sources = {Path(source["path"]): source for source in self.row["sources"]}
        if path not in sources:
            raise FactsMissing("route_source_unverified")
        original = private_bytes(self.root / path)
        if sha256(original).hexdigest() != sources[path]["sha256"]:
            raise FactsMissing("route_source_unverified")
        return read_observation_source(self.row, field, sources, {path: original}, {})


def preflight_observations(rows, report, selection, root, broker, now, *, allow_late):
    result = {}
    stock_routes = {}
    report_day = day(report["date"])
    for row in rows:
        path = row["projection_path"]
        result[path] = item = {"observation": row}
        if row["state"] != "qualified":
            item["reason"] = {
                "unknown": "observation_unverified",
                "conflict": "observation_conflict",
                "withdrawn": "observation_withdrawn",
                "rejected": "verified_no_signal",
            }[row["state"]]
            continue
        market = BRIDGE_MARKETS.get(row["market"], row["market"])
        kind = row["instrument_type"]
        asset = {
            "instrument_id": row["identity"],
            "code": row["code"],
            "market": market,
            "instrument_type": kind,
            "currency": row["currency"],
        }
        when = day(row["target_day"])
        item["asset"] = asset
        product = product_for(kind, market)
        if product == "crypto":
            item["reason"] = manual_crypto(item, asset, row)
            continue
        route_key = (
            market,
            row["identity"],
            row["code"],
            row["direction"],
            row["currency"],
            row["target_day"],
            kind,
        )
        if product == "stock" and route_key in stock_routes:
            item.update(stock_routes[route_key], observation=row)
            continue
        try:
            route, session = build_route(
                broker,
                ObservationRouteFacts(root, row),
                {},
                asset,
                product,
                row["direction"],
                report_day,
                when,
                now,
                bridge=True,
                allow_late=allow_late,
            )
        except FactsMissing as error:
            item["reason"] = str(error)
        except (OSError, KeyError, IndexError, ValueError, TypeError):
            item["reason"] = "raw_or_route_evidence_unavailable"
        except BrokerUnavailable:
            item["reason"] = "paper_market_facts_unavailable"
        else:
            asset["currency"] = route["currency"]
            item["route"] = proof(
                asset,
                when,
                row.get("version") or selection["revision"],
                route,
                "OpenD route, product, quote, lot and simulation account readback",
            )
            item["session"] = session
        if product == "stock":
            stock_routes[route_key] = item
    return result


def manual_crypto(item, asset, row):
    currency, code = asset["currency"], asset["code"]
    if (
        asset["market"] != "CRYPTO"
        or row["direction"] != "long"
        or not text(currency)
        or not re.fullmatch(r"[A-Z0-9]{1,20}[-/]" + re.escape(currency), code)
    ):
        return "crypto_route_unverified"
    item["manual"] = {
        "code": code,
        "direction": "long",
        "currency": currency,
        "state": "pending_verification",
        "missing": ["funds", "exposure", "limit_price", "quantity_precision"],
    }
    return "manual_crypto_preflight_required"


def record_facts(
    report, facts, raw, broker, group, asset, path, direction, now, *, allow_late
):
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
        allow_late=allow_late,
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
