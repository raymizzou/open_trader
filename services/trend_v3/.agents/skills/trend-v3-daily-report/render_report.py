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
PAPER_STATES = {
    "pending": "待执行", "skipped": "已跳过", "denied": "Ray 未批准",
    "active": "已提交", "filled": "全部成交", "unknown": "回执待核",
    "rejected": "拒单", "cancelled": "剩余撤销",
}
PAPER_REASONS = {
    "waiting_for_ray": "等候 Ray 确认",
    "waiting_for_open": "等候目标市场开盘",
    "not_approved": "开盘前未收到确认",
    "ray_denied": "Ray 明确拒绝",
    "missed_open": "已错过本次开盘",
    "open_dispatch_deadline": "开盘报单时间已过",
    "retry_deadline": "本次重试授权已过期",
    "approval_evidence_unavailable": "原计划回复记录无法核实",
    "report_revised_or_unverified": "原报告已更新或无法核实",
    "execution_conditions_unverified": "开盘交易条件无法核实",
    "market_not_open": "开盘时未确认市场可接受市价单",
    "market_order_unsupported": "仅限价或市价不可用",
    "broker_timeout": "券商回执超时",
    "broker_disconnected": "券商连接中断",
    "broker_receipt_unverified": "未核实券商回执",
    "broker_receipt_conflict": "券商回执有矛盾，需重新对账",
    "awaiting_broker_receipt": "等待原订单对账",
    "plan_superseded": "原计划已由新报告替代",
    "instrument_busy": "已有持仓、挂单或待对账委托",
    "approved_size_unavailable": "已批准数量当前不可用",
    "short_capacity_unverified": "模拟账户可卖空数量待核",
    "crypto_route_unverified": "加密现货模拟路径待核",
    "portfolio_slots_full": "模拟组合已达到 10 个标的",
    "portfolio_currency_unverified": "账户敞口币种待核",
    "raw_signal_unverified": "原始信号记录未核实",
    "identity_market_unverified": "标的及市场身份待核",
    "membership_unverified": "直属归属证据待核",
    "market_no_session": "目标日无正常交易时段",
    "opening_unverified": "正常开盘时段待核",
    "reference_price_or_lot_unverified": "参考价或整手规格待核",
    "simulate_account_unavailable": "可用模拟账户待核",
    "paper_market_facts_unavailable": "模拟账户或市场字段待核",
    "future_contract_unverified": "期货交割月映射待核",
    "future_specification_unverified": "期货合约规格或交易时段待核",
    "future_underlying_unverified": "期货标的与券商合约对应关系待核",
    "future_currency_unverified": "期货合约计价币种待核",
    "future_session_unverified": "期货适用日间交易时段待核",
    "future_expiry_unverified": "期货最后交易日待核",
    "future_quantity_unit_unverified": "期货报单单位或乘数待核",
    "market_status_unverified": "券商快照未确认标的正常可交易",
    "a_share_etf_route_unverified": "A 股 ETF 的模拟市价路径待核",
    "etf_exposure_unverified": "ETF 的 1 倍敞口待核",
}
PAPER_CODE = re.compile(r"[A-Z0-9][A-Z0-9_.-]{0,39}", re.IGNORECASE | re.ASCII)
PAPER_PLACEHOLDER = '        <p class="positions-note">未接入</p>'


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
    for source in asset.get("signal_sources", []):
        reason += (
            " 信号榜："
            + " · ".join(display_text(source[key]) for key in ("title", "label"))
            + " · 数据日 "
            + display_text(source["data_date"])
            + "。"
        )
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
    if group.get("_signal_coverage_pending"):
        notes.append(fragments["empty_note"].substitute(note="信号搜索覆盖待核。"))
    if group.get("_missing_signal_hits"):
        notes.append(
            fragments["empty_note"].substitute(note="已核实信号命中尚未入直属卡。")
        )
    return "\n".join(notes)


def render_direction(fragments, groups, note, direction, *, threshold=False):
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


def render_simulation(paper, report_day, report_revision):
    if (
        not isinstance(report_revision, str)
        or not re.fullmatch(r"v[0-9]{2,}", report_revision)
        or not isinstance(paper, dict)
        or paper.get("schema") != "trend-v3-paper-entry.v1"
        or paper.get("date") != report_day.isoformat()
        or paper.get("report_version") != report_revision
        or not isinstance(paper.get("results"), list)
        or not isinstance(paper.get("skipped"), list)
    ):
        return PAPER_PLACEHOLDER
    rows = []
    for record in paper["results"] + paper["skipped"]:
        if not isinstance(record, dict):
            continue
        code = record.get("code")
        if not isinstance(code, str) or not PAPER_CODE.fullmatch(code):
            continue
        state_code, reason_code = record.get("state"), record.get("reason")
        state = (
            PAPER_STATES.get(state_code, "已跳过")
            if isinstance(state_code, str)
            else "已跳过"
        )
        reason = (
            PAPER_REASONS.get(reason_code) if isinstance(reason_code, str) else None
        )
        if not reason and state == "已跳过":
            reason = "开仓条件待核或不符"
        parts = [escape(code), state]
        if reason:
            parts.append(reason)
        for field, label in (("filled", "已成交"), ("remaining", "剩余")):
            amount = record.get(field)
            if type(amount) is int and amount >= 0:
                parts.append(f"{label} {amount}")
        if isinstance(state_code, str) and state_code in (
            "active", "unknown", "rejected", "cancelled", "filled",
        ):
            active = record.get("active")
            activity = (
                "有活动委托" if active is True else "无活动委托" if active is False
                else "活动委托待核"
            )
            parts.append(activity)
        rows.append("<li>" + " · ".join(parts) + "</li>")
    note = "模拟开仓记录" if rows else "本期无可确认的模拟开仓记录"
    section = f'        <p class="positions-note">{note}</p>'
    if rows:
        return section + '\n        <ul role="list">' + "".join(rows) + "</ul>"
    return section


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
            report.get("short_note"),
            "short",
            threshold=report.get("selection_mode") == "threshold-v1",
        ),
        long_rows=render_direction(
            fragments,
            report.get("long", []),
            report.get("long_note"),
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
        report = json.loads(args.input.read_text(encoding="utf-8"), parse_float=Decimal)
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
