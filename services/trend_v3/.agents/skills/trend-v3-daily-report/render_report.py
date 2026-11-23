"""Render the V1 reader from a verified, display-only JSON projection."""

import argparse
import json
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from string import Template

TEMPLATE_PATH = Path(__file__).resolve().parent / "assets/report.html"
FRAGMENT = re.compile(r'\n  <template id="([a-z_]+)">\n(.*?)\n  </template>', re.DOTALL)
DATE_FORMAT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
LOOKUP_LABELS = {"miss": "查不到", "pending": "待核"}
LEGACY_EVENT_NAMES = {
    "温转热",
    "凉转寒",
    "寒转凉",
    "热转沸",
    "沸中创历史新高",
    "寒转冻",
    "冻中创历史新低",
    "开香槟",
    "上天台",
    "反抽警报",
    "危险信号",
    "右侧结束",
    "左侧结束",
}
LEGACY_COVERAGE_SUFFIX = re.compile(r"\s*(?:（其余待核）|\(其余待核\))$")
LEGACY_POSITIVE_HEADING = re.compile(r"^\s*(?:已确认|已触发)\s*[:：]\s*")
EVENT_STATUS_WORDS = {
    "无",
    "否",
    "否定",
    "阴性",
    "不明",
    "不适用",
    "缺失",
    "冲突",
    "信号冲突",
    "疑似",
    "可能",
    "miss",
    "none",
    "null",
    "false",
    "n/a",
}
EVENT_STATUS_OR_SEPARATOR = re.compile(
    r"[、，,；;：:\r\n]|待核|待确认|待定|查不到|未知|"
    r"(?:疑似|可能)(?:触发|出现|发生)|"
    r"未(?:触发|确认|出现|发生|查到|核实|证实|检测到|见)|"
    r"没有|尚无|并无|无(?:已确认|确认|特殊)?(?:信号|事件|匹配)|"
    r"\b(?:unknown|pending|unconfirmed|uncertain|no\s+(?:event|signal)s?|"
    r"not\s+(?:triggered|confirmed|observed|verified|found|available))\b",
    re.IGNORECASE,
)
STRENGTH_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def display_text(value, default="UNKNOWN"):
    if value is None or value == "":
        value = default
    if not isinstance(value, str):
        raise ValueError("Display fields must be text")
    return escape(value)


def asset_metric(value):
    return display_text(None if value == "UNKNOWN" else value, "待核")


def legacy_event_names(value):
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError("Legacy signal must be text")
    for clause in re.split(r"[；;。.\r\n]", value):
        clause = LEGACY_POSITIVE_HEADING.sub("", clause)
        for part in re.split(r"[、，,]", clause):
            name = LEGACY_COVERAGE_SUFFIX.sub("", part.strip())
            if name not in LEGACY_EVENT_NAMES:
                break
            yield name


def verified_event_names(values):
    if not isinstance(values, list) or any(
        not isinstance(name, str) for name in values
    ):
        raise ValueError("Signal events must be an array of names")
    for name in map(str.strip, values):
        # Selection has already checked the evidence. Filter status text and lists
        # mistakenly placed in a single name slot without limiting the event names.
        negated_event = (
            name.startswith(("无", "未", "非", "不"))
            and name[1:].strip() in LEGACY_EVENT_NAMES
        )
        if (
            name
            and not negated_event
            and name.casefold() not in EVENT_STATUS_WORDS
            and not EVENT_STATUS_OR_SEPARATOR.search(name)
        ):
            yield name


def special_signal(asset):
    if "signal_events" in asset:
        names = verified_event_names(asset["signal_events"])
    else:
        names = legacy_event_names(asset.get("signal"))
    return display_text("，".join(dict.fromkeys(names)), "无")


def strength_value(value):
    """Keep source precision while validating the 0–100 comparison scale."""
    raw = str(value)
    if raw.endswith(" / 100"):
        raw = raw[:-6]
    try:
        if STRENGTH_NUMBER.fullmatch(raw) and Decimal(raw) <= 100:
            return raw
    except InvalidOperation:
        pass
    return None


def strength_fields(asset):
    global_value = asset.get("strength")
    local_value = asset.get("local_strength")
    # Old peer-only display text proves no global value. Explicit local null
    # is a withdrawal/missing value and must not revive the legacy peer text.
    if isinstance(global_value, str) and global_value.startswith("同品种 "):
        if "local_strength" not in asset:
            local_value = global_value.removeprefix("同品种 ")
        global_value = None
    return strength_value(global_value), strength_value(local_value)


def strength_note(asset, field, value, label):
    if value is not None:
        return ""
    reason = display_text(asset.get(field + "_reason"), label + "字段缺失或无效。")
    return f'<p class="reader-metric-note">{reason}</p>'


def render_card(pattern, asset):
    lookup = asset.get("lookup")
    if lookup is None:
        trend = asset_metric(asset.get("trend"))
        signal = special_signal(asset)
        global_value, local_value = strength_fields(asset)
        strength = f"{global_value} / 100" if global_value is not None else "待核"
        local_strength = f"{local_value} / 100" if local_value is not None else "待核"
        strength_reason = strength_note(asset, "strength", global_value, "全局强度")
        local_strength_reason = strength_note(
            asset, "local_strength", local_value, "同品种强度"
        )
        reason = display_text(asset.get("reason"), "品种证据待核。")
    else:
        if not isinstance(lookup, str) or lookup not in LOOKUP_LABELS:
            raise ValueError("Lookup status must be miss or pending")
        status = LOOKUP_LABELS[lookup]
        trend = strength = local_strength = status
        strength_reason = local_strength_reason = ""
        signal = "无"
        reason = display_text(asset.get("reason"), status)
    # These flags are supplied only after evidence selection. Neither list
    # placement nor a displayed event establishes membership or eligibility.
    markers = []
    if asset.get("direct_verified") is True:
        markers.append('<span class="reader-marker">直属</span>')
    if asset.get("platform_match_verified") is True:
        markers.append('<span class="reader-marker">趋势动物可查</span>')
    if lookup is None and asset.get("watch_eligible") is True:
        markers.append('<span class="reader-marker">应建观察仓</span>')
    return pattern.substitute(
        markers='<p class="reader-markers">' + " · ".join(markers) + "</p>"
        if markers
        else "",
        code=display_text(asset["code"]),
        name=display_text(asset["name"]),
        trend=trend,
        signal=signal,
        strength=strength,
        local_strength=local_strength,
        strength_reason=strength_reason,
        local_strength_reason=local_strength_reason,
        reason=reason,
    )


def render_assets(fragments, assets, note):
    if not assets:
        return fragments["empty_note"].substitute(note=note)
    cards = [render_card(fragments["asset_card"], asset) for asset in assets]
    return fragments["asset_list"].substitute(cards="\n".join(cards))


def platform_id(asset):
    value = asset.get("instrument_id")
    if value is None or value == "":
        return None
    if type(value) is int and value >= 0:
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    raise ValueError("Instrument ID must be text or a nonnegative integer")


def instrument_scope(asset):
    # Currency is an additional boundary when supplied, never inferred.
    values = (asset.get("market"), asset.get("instrument_type"))
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    currency = asset.get("currency")
    if currency is not None and not isinstance(currency, str):
        raise ValueError("Instrument currency must be text")
    return (*values, currency or None)


def instrument_identity(asset, ids_by_code):
    # A code-only alias can use a single established ID in this exact scope.
    # Multiple contradictory IDs never make the alias identify either one.
    scope = instrument_scope(asset)
    if scope is None:
        return None
    identifier = platform_id(asset)
    code = asset.get("code")
    if identifier is None and isinstance(code, str) and code.strip():
        ids = ids_by_code.get((*scope, code), set())
        if len(ids) == 1:
            identifier = next(iter(ids))
        else:
            return (*scope, "code", code)
    return (*scope, "instrument_id", identifier) if identifier is not None else None


def global_strength(asset):
    # Local strength never participates in direction ordering.
    if asset.get("lookup") is not None:
        return None
    value, _ = strength_fields(asset)
    return Decimal(value) if value is not None else None


def reconcile_lookup(preferred, aliases):
    # Winner selection uses the original rows. Only the exact-identity match
    # fact can aggregate here; quotes require the separate strength-only opt-in.
    # Events and eligibility never transfer.
    evidence = [row for row in aliases
                if row.get("platform_match_verified") is True
                or row.get("lookup") in LOOKUP_LABELS]
    if not evidence:
        return preferred
    has_match = any(row.get("platform_match_verified") is True for row in evidence)
    scopes = set()
    for row in evidence:
        scope = row.get("lookup_scope")
        if scope is not None and (not isinstance(scope, str) or not scope.strip()):
            raise ValueError("Lookup scope must be nonempty text")
        scopes.add(scope)
    conflict = len(scopes) > 1 or (
        has_match and any(row.get("lookup") == "miss" for row in evidence)
    )
    if not has_match and not conflict:
        return preferred
    reconciled = dict(preferred)
    reconciled["platform_match_verified"] = has_match and not conflict
    if conflict:
        reconciled["lookup"] = "pending"
        reconciled["reason"] = "平台匹配证据冲突或适用范围待核。"
    elif preferred.get("lookup") == "pending":
        # Removing a pending lookup must not unmask its old quote/event payload
        # or activate an old watch flag. Identity success proves no market data.
        for field in ("lookup", "trend", "strength", "local_strength", "signal",
                      "strength_reason", "local_strength_reason"):
            reconciled.pop(field, None)
        reconciled["signal_events"] = []
        reconciled["watch_eligible"] = False
        reconciled["reason"] = "平台身份已核实，行情字段待核。"
    return reconciled


def strength_scope(value):
    if not isinstance(value, dict):
        return None
    version = value.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    try:
        day = report_date(value.get("data_date"))
    except (TypeError, ValueError):
        return None
    return day, version


def reconcile_strengths(preferred, reconciled, aliases):
    """Opt-in, field-only bridge for independently verified split projections."""
    scope = strength_scope(preferred.get("strength_scope"))
    identifier = platform_id(preferred)
    identity_scope = instrument_scope(preferred)
    # Identity resolution alone must never revive pending/hidden market data.
    if (scope is None or identifier is None or identity_scope is None
            or preferred.get("lookup") is not None
            or reconciled.get("lookup") is not None):
        return reconciled
    result = dict(reconciled)
    own_evidence = preferred.get("strength_evidence", {})
    if not isinstance(own_evidence, dict):
        own_evidence = {}
    for field, current in zip(
        ("strength", "local_strength"), strength_fields(preferred), strict=True
    ):
        values = [current] if current is not None else []
        # Omission can be supplemented. An explicit null/invalid value cannot.
        invalidated = field in preferred and current is None
        if field in own_evidence:
            proof = own_evidence[field]
            invalidated |= (strength_scope(proof) != scope
                            or proof.get("state") != "verified")
        for row in aliases:
            if (row is preferred or row.get("lookup") is not None
                    or row.get("platform_match_verified") is not True
                    or platform_id(row) != identifier
                    or instrument_scope(row) != identity_scope):
                continue
            evidence = row.get("strength_evidence", {})
            proof = evidence.get(field) if isinstance(evidence, dict) else None
            if strength_scope(proof) != scope:
                continue
            if proof.get("state") == "withdrawn":
                invalidated = True
            elif proof.get("state") == "verified" and field in row:
                value = strength_value(row[field])
                if value is None:
                    invalidated = True
                else:
                    values.append(value)
        if invalidated or len({Decimal(value) for value in values}) > 1:
            result[field] = None
            result[field + "_reason"] = "该强度证据已失效或存在冲突，待核。"
        elif values:
            # Equal values are compatible; preserve preferred precision, otherwise
            # choose the longest source representation independently of row order.
            result[field] = current or max(
                values, key=lambda value: (len(value), value)
            )
            result.pop(field + "_reason", None)
    return result


def instrument_sections(group, direction):
    candidates = []
    for field in ("direct", "searchable", "related"):
        assets = group.get(field, [])
        if not isinstance(assets, list):
            raise ValueError("Asset lists must be arrays")
        candidates.extend(assets)
    ids_by_code = {}
    for asset in candidates:
        scope = instrument_scope(asset)
        identifier = platform_id(asset)
        code = asset.get("code")
        if scope is not None and identifier is not None and isinstance(code, str):
            ids_by_code.setdefault((*scope, code), set()).add(identifier)
    aliases_by_identity = {}
    for asset in candidates:
        identity = instrument_identity(asset, ids_by_code)
        if identity is not None:
            aliases_by_identity.setdefault(identity, []).append(asset)
    sections = [[], [], []]
    seen = set()
    # Resolve strongest verified classification before removing duplicates.
    # Keep selected-row facts and eligibility. Only explicitly verified strength
    # fields may cross rows below; never infer membership from list position.
    for priority in range(3):
        for asset in candidates:
            category = (0 if asset.get("direct_verified") is True else
                        1 if asset.get("platform_match_verified") is True else 2)
            if category != priority:
                continue
            identity = instrument_identity(asset, ids_by_code)
            if identity is not None:
                if identity in seen:
                    continue
                seen.add(identity)
            aliases = aliases_by_identity.get(identity, [asset])
            reconciled = reconcile_lookup(asset, aliases)
            reconciled = reconcile_strengths(asset, reconciled, aliases)
            final_category = (
                0 if reconciled.get("direct_verified") is True else
                1 if reconciled.get("platform_match_verified") is True else 2
            )
            sections[final_category].append(reconciled)
    def order(asset):
        strength = global_strength(asset)
        return (strength is None,
                -strength if direction == "long" and strength is not None else
                strength if strength is not None else Decimal(0), asset["code"])
    for section in sections:
        section.sort(key=order)
    return sections


def coverage_note(fragments, group, direct):
    general = group.get("coverage_note")
    legacy = group.get("direct_partial_note")
    for note in (general, legacy):
        if note is not None and not isinstance(note, str):
            raise ValueError("Coverage note must be text")
    # The general field is independent of known cards. Preserve the legacy
    # direct-only field's behavior when no general field was supplied.
    note = general if general is not None else legacy if direct else None
    if note and note.strip():
        return fragments["empty_note"].substitute(note=display_text(note))
    return ""


def render_direction(fragments, groups, note, direction):
    if not isinstance(groups, list):
        raise ValueError("Direction rows must be arrays")
    rows = []
    seen_seats = set()
    for index, group in enumerate(groups):
        seat = group["seat"]
        if type(seat) is not int or seat not in (1, 2):
            raise ValueError("A direction seat must be the integer 1 or 2")
        if seat in seen_seats:
            raise ValueError(f"Duplicate seat {seat} in one direction")
        seen_seats.add(seat)
        code = group.get("code")
        direct, searchable, related = instrument_sections(group, direction)
        rows.append(
            fragments["class_row"].substitute(
                open_attr=" open" if index == 0 else "",
                name=display_text(group["name"]),
                ticker=fragments["ticker"].substitute(code=display_text(code))
                if code
                else "",
                seat=f"{seat:02}",
                board_strength=display_text(group.get("board_strength")),
                direct=render_assets(
                    fragments, direct,
                    display_text(
                        group.get("direct_note") if not group.get("direct") else None,
                        "暂无已核实的直属成员。",
                    ),
                ),
                searchable=render_assets(
                    fragments, searchable,
                    display_text(
                        group.get("searchable_note"),
                        "暂无已核实的平台精确匹配标的。",
                    ),
                ),
                related=render_assets(
                    fragments, related,
                    display_text(
                        group.get("related_note") if not group.get("related") else None,
                        "暂无其他有据关联的标的。",
                    ),
                ),
                coverage_note=coverage_note(fragments, group, direct),
            )
        )

    if note or not rows:
        rows.append(
            fragments["empty_note"].substitute(
                note=display_text(note, "暂无已核实的大类席位。")
            )
        )
    return "\n".join(rows)


def report_date(value):
    if not isinstance(value, str) or not DATE_FORMAT.fullmatch(value):
        raise ValueError("Report and board dates must use YYYY-MM-DD")
    return date.fromisoformat(value)


def render(report, template):
    day = report_date(report["date"])
    board_day = report.get("board_date")
    if board_day:
        board_note = f"大类强度取自 {report_date(board_day).isoformat()} 官方榜图。"
    else:
        board_note = "官方榜图日期待核。"
    if version := report.get("board_version"):
        board_note += f"版次 {display_text(version)}。"

    fragments = {name: Template(markup) for name, markup in FRAGMENT.findall(template)}
    return Template(FRAGMENT.sub("", template)).substitute(
        date=day.isoformat(),
        year=day.year,
        month=day.month,
        day=day.day,
        board_note=board_note,
        short_rows=render_direction(
            fragments, report.get("short", []), report.get("short_note"), "short"
        ),
        long_rows=render_direction(
            fragments, report.get("long", []), report.get("long_note"), "long"
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Verified JSON projection")
    parser.add_argument(
        "output", type=Path, help="New HTML path in an existing directory"
    )
    args = parser.parse_args()
    try:
        report = json.loads(args.input.read_text(encoding="utf-8"), parse_float=Decimal)
        html = render(report, TEMPLATE_PATH.read_text(encoding="utf-8"))
        fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(html)
    except (
        AttributeError,
        InvalidOperation,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        parser.exit(2, f"Render failed: {error}\n")


if __name__ == "__main__":
    main()
