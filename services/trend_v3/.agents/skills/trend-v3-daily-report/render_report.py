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
    for clause in re.split(r"[；;。\n]", value):
        clause = LEGACY_POSITIVE_HEADING.sub("", clause)
        for part in re.split(r"[、，,]", clause):
            name = LEGACY_COVERAGE_SUFFIX.sub("", part.strip())
            # A heading can qualify the rest of this list, e.g. 未触发：甲、乙.
            if re.search(r"[:：(（]", name):
                break
            if name in LEGACY_EVENT_NAMES:
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


def card_strength(asset):
    if "local_strength" not in asset:
        return asset_metric(asset.get("strength"))
    raw = str(asset["local_strength"])
    try:
        if STRENGTH_NUMBER.fullmatch(raw) and Decimal(raw) <= 100:
            return f"同品种 {raw} / 100"
    except InvalidOperation:
        pass
    return "待核"


def render_card(pattern, asset):
    lookup = asset.get("lookup")
    if lookup is None:
        trend = asset_metric(asset.get("trend"))
        signal = special_signal(asset)
        strength = card_strength(asset)
        reason = display_text(asset.get("reason"), "品种证据待核。")
    else:
        if not isinstance(lookup, str) or lookup not in LOOKUP_LABELS:
            raise ValueError("Lookup status must be miss or pending")
        status = LOOKUP_LABELS[lookup]
        trend = strength = status
        signal = "无"
        reason = display_text(asset.get("reason"), status)
    return pattern.substitute(
        code=display_text(asset["code"]),
        name=display_text(asset["name"]),
        trend=trend,
        signal=signal,
        strength=strength,
        reason=reason,
    )


def render_assets(fragments, assets, note):
    if not isinstance(assets, list):
        raise ValueError("Asset lists must be arrays")
    if not assets:
        return fragments["empty_note"].substitute(note=note)

    cards = [render_card(fragments["asset_card"], asset) for asset in assets]
    return fragments["asset_list"].substitute(cards="\n".join(cards))


def render_direction(fragments, groups, note):
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
                    fragments,
                    group.get("direct", []),
                    display_text(
                        group.get("direct_note"), "暂无已确认的直接交易标的。"
                    ),
                ),
                related=render_assets(
                    fragments,
                    group.get("related", []),
                    display_text(group.get("related_note"), "暂无已确认的关联标的。"),
                ),
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
            fragments, report.get("short", []), report.get("short_note")
        ),
        long_rows=render_direction(
            fragments, report.get("long", []), report.get("long_note")
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
