"""Render the V1 reader from a verified, display-only JSON projection."""

import argparse
import json
import os
import re
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from string import Template

from collection import (
    LOOKUP_LABELS,
    assess,
    dated_projection,
    reconciled_instruments,
    report_date,
    strength_fields,
)

TEMPLATE_PATH = Path(__file__).resolve().parent / "assets/report.html"
FRAGMENT = re.compile(r'\n  <template id="([a-z_]+)">\n(.*?)\n  </template>', re.DOTALL)
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


def strength_note(asset, field, value, label):
    if value is not None:
        return ""
    reason = display_text(asset.get(field + "_reason"), label + "字段缺失或无效。")
    return f'<p class="reader-metric-note">{reason}</p>'


def render_card(pattern, asset):
    lookup = asset.get("lookup")
    if lookup is None:
        trend = asset_metric(asset.get("trend"))
        signal = "待查" if asset.get("_signal_pending") else special_signal(asset)
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
        signal = "待查" if "_signal_pending" in asset else "无"
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
        freshness=asset.get("_freshness", "waiting"),
        freshness_label=display_text(asset.get("_freshness_label"), "日期待核"),
        date_note=display_text(asset.get("_date_note"), "行情截至 待核"),
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


def global_strength(asset):
    # Local strength never participates in direction ordering.
    if asset.get("lookup") is not None:
        return None
    value, _ = strength_fields(asset)
    return Decimal(value) if value is not None else None


def instrument_sections(group, direction):
    sections = reconciled_instruments(group)
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
    notes = []
    if note and note.strip():
        notes.append(fragments["empty_note"].substitute(note=display_text(note)))
    if group.get("_coverage_pending") and "成员范围待查" not in (note or ""):
        notes.append(fragments["empty_note"].substitute(note="成员范围待查。"))
    return "\n".join(notes)


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
                date_note=display_text(group.get("_date_note"), "大类数据日期待核"),
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


def render(report, template):
    assessment = assess(report)
    report, _ = dated_projection(report)
    day = report_date(report["date"])
    fragments = {name: Template(markup) for name, markup in FRAGMENT.findall(template)}
    return Template(FRAGMENT.sub("", template)).substitute(
        collection_status="数据已完备" if assessment["complete"] else "数据收集中",
        collection_time=display_text(
            "核实时间：" + assessment["verified_at"] if assessment["complete"]
            else "下次核实：" + str(report.get("collection", {}).get("next_check")
                                      or "待核（普通读取不启动轮询）")
        ),
        date=day.isoformat(),
        year=day.year,
        month=day.month,
        day=day.day,
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
