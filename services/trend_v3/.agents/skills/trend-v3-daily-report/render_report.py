"""Render the V1 reader from a verified, display-only JSON projection."""

import argparse
import json
import os
import re
from datetime import date
from html import escape
from pathlib import Path
from string import Template

TEMPLATE_PATH = Path(__file__).resolve().parent / "assets/report.html"
FRAGMENT = re.compile(r'\n  <template id="([a-z_]+)">\n(.*?)\n  </template>', re.DOTALL)
DATE_FORMAT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
LOOKUP_LABELS = {"miss": "查不到", "pending": "待核"}


def display_text(value, default="UNKNOWN"):
    if value is None or value == "":
        value = default
    if not isinstance(value, str):
        raise ValueError("Display fields must be text")
    return escape(value)


def asset_metric(value):
    return display_text(None if value == "UNKNOWN" else value, "待核")


def render_card(pattern, asset):
    lookup = asset.get("lookup")
    if lookup is None:
        trend = asset_metric(asset.get("trend"))
        signal = asset_metric(asset.get("signal"))
        strength = asset_metric(asset.get("strength"))
        reason = display_text(asset.get("reason"), "品种证据待核。")
    else:
        if not isinstance(lookup, str) or lookup not in LOOKUP_LABELS:
            raise ValueError("Lookup status must be miss or pending")
        status = LOOKUP_LABELS[lookup]
        trend = signal = strength = status
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
        report = json.loads(args.input.read_text(encoding="utf-8"))
        html = render(report, TEMPLATE_PATH.read_text(encoding="utf-8"))
        fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(html)
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(2, f"Render failed: {error}\n")


if __name__ == "__main__":
    main()
