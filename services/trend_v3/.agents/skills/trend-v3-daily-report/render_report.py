"""Render the V1 reader from a verified, display-only JSON projection."""

import argparse
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
    load_report,
    reconciled_instruments,
    report_date,
    strength_fields,
)
from paper_public import PAPER_PLACEHOLDER, render_simulation

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


def render_card(pattern, asset):
    lookup = asset.get("lookup")
    if lookup is None:
        trend = asset_metric(asset.get("trend"))
        signal = "待查" if asset.get("_signal_pending") else special_signal(asset)
        global_value, local_value = strength_fields(asset)
        strength = f"{global_value} / 100" if global_value is not None else "待核"
        local_strength = f"{local_value} / 100" if local_value is not None else "待核"
    else:
        if not isinstance(lookup, str) or lookup not in LOOKUP_LABELS:
            raise ValueError("Lookup status must be miss or pending")
        status = LOOKUP_LABELS[lookup]
        trend = strength = local_strength = status
        signal = "待查" if "_signal_pending" in asset else "无"
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


def _verify_recorded_cards(markup, group, chosen):
    """Check rendered event cells against each exact reconciled instrument."""
    cards = re.findall(r'<li class="asset reader-card">.*?</li>', markup, re.S)
    for section in ("direct", "searchable", "related"):
        for original in group.get(section, []):
            names = original.get("_recorded_own_events", [])
            if not names:
                continue

            def identity(asset):
                return tuple(
                    asset.get(k)
                    for k in ("instrument_id", "market", "instrument_type", "currency")
                )

            indices = [
                i
                for i, asset in enumerate(chosen)
                if identity(asset) == identity(original)
            ]
            if len(indices) != 1 or len(cards) != len(chosen):
                raise ValueError("own-event discrepancy: rendered identity")
            cell = re.search(
                r'<dd class="reader-event">(.*?)</dd>', cards[indices[0]], re.S
            )
            tokens = cell[1].split("，") if cell else []
            if any(display_text(name) not in tokens for name in names):
                raise ValueError(
                    "own-event discrepancy: rendered special signal " + original["code"]
                )


def render_direction(fragments, groups, direction, *, threshold=False):
    if not isinstance(groups, list):
        raise ValueError("Direction rows must be arrays")
    rows = []
    seen_seats = set()
    for index, group in enumerate(groups):
        seat = group["seat"]
        if type(seat) is not int or (seat < 1 if threshold else seat not in (1, 2)):
            raise ValueError("Invalid direction seat for this report version")
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
                direct=render_assets(fragments, direct, "暂无已核实的直属成员。"),
                searchable=render_assets(
                    fragments, searchable, "暂无已核实的平台精确匹配标的。"
                ),
                related=render_assets(fragments, related, "暂无其他有据关联的标的。"),
            )
        )

        _verify_recorded_cards(rows[-1], group, direct + searchable + related)

    if not rows:
        rows.append(
            fragments["empty_note"].substitute(
                note="暂无已核实的大类席位。"
            )
        )
    return "\n".join(rows)




def render(report, template, *, report_revision=None):
    assessment = assess(report)
    paper = report.get("paper_entry")
    report, _ = dated_projection(report)
    day = report_date(report["date"])
    template = template.replace(
        PAPER_PLACEHOLDER + "<!-- paper-entry -->",
        render_simulation(paper, day, report_revision),
        1,
    )
    fragments = {name: Template(markup) for name, markup in FRAGMENT.findall(template)}
    return Template(FRAGMENT.sub("", template)).substitute(
        collection_status="数据已完备" if assessment["complete"] else "数据收集中",
        collection_time=display_text(
            "核实时间：" + assessment["verified_at"]
            if assessment["complete"]
            else "下次核实："
            + str(
                report.get("collection", {}).get("next_check")
                or "待核（普通读取不启动轮询）"
            )
        ),
        date=day.isoformat(),
        year=day.year,
        month=day.month,
        day=day.day,
        short_rows=render_direction(
            fragments,
            report.get("short", []),
            "short",
            threshold=report.get("selection_mode") == "threshold-v1",
        ),
        long_rows=render_direction(
            fragments,
            report.get("long", []),
            "long",
            threshold=report.get("selection_mode") == "threshold-v1",
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Verified JSON projection")
    parser.add_argument(
        "output", type=Path, help="New HTML path in an existing directory"
    )
    parser.add_argument(
        "--report-revision", help="Independently verified report revision"
    )
    args = parser.parse_args()
    try:
        report = load_report(args.input, parse_float=Decimal)
        html = render(
            report, TEMPLATE_PATH.read_text(encoding="utf-8"),
            report_revision=args.report_revision,
        )
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
