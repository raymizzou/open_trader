"""Allowlisted simulation section shared by the reader and trusted publisher."""

import re
from html import escape

PAPER_STATES = {
    "pending": "待执行",
    "skipped": "已跳过",
    "denied": "Ray 未批准",
    "active": "已提交",
    "filled": "全部成交",
    "unknown": "回执待核",
    "rejected": "拒单",
    "cancelled": "剩余撤销",
}
PAPER_REASONS = {
    "board_unverified": "榜单日期或版本证据待核",
    "verified_no_signal": "已核实无开仓信号",
    "board_seat_unverified": "榜单席位证据待核",
    "coverage_unverified": "成员范围证据待核",
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
    if "holdings" in paper:
        return render_holdings(paper["holdings"], report_day, report_revision)
    rows = []
    for record in paper["results"] + paper["skipped"]:
        if not isinstance(record, dict):
            continue
        code = record.get("code")
        if code is not None and (
            not isinstance(code, str) or not PAPER_CODE.fullmatch(code)
        ):
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
        if (
            state_code == "active"
            and type(record.get("filled")) is int
            and record["filled"] > 0
        ):
            state = "部分成交"
        if code is None and not reason:
            continue
        parts = ([escape(code)] if code is not None else []) + [state]
        if reason:
            parts.append(reason)
        for field, label in (("filled", "已成交"), ("remaining", "剩余")):
            amount = record.get(field)
            if type(amount) is int and amount >= 0:
                parts.append(f"{label} {amount}")
        if isinstance(state_code, str) and state_code in (
            "active",
            "unknown",
            "rejected",
            "cancelled",
            "filled",
        ):
            active = record.get("active")
            activity = (
                "有活动委托"
                if active is True
                else "无活动委托"
                if active is False
                else "活动委托待核"
            )
            parts.append(activity)
        direction = {"long": "做多", "short": "开空"}.get(str(record.get("direction")))
        if direction:
            parts.append(direction)
        quantity = record.get("quantity")
        if type(quantity) is int and quantity > 0:
            parts.append(f"申请数量 {quantity}")
        approved = record.get("approved")
        if type(approved) is int and approved >= 0:
            parts.append(f"批准数量 {approved}")
        ordinal = record.get("record")
        if type(ordinal) is int and ordinal > 0:
            parts.append(f"记录 {ordinal}")
        version = record.get("report_version")
        if isinstance(version, str) and re.fullmatch(r"v[0-9]{2,}", version):
            parts.append("日报 " + version)
        session_date = record.get("session_date")
        if isinstance(session_date, str) and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", session_date
        ):
            parts.append("交易日 " + session_date)
        rows.append("<li>" + " · ".join(parts) + "</li>")
    note = "模拟开仓记录" if rows else "本期无可确认的模拟开仓记录"
    section = f'        <p class="positions-note">{note}</p>'
    if rows:
        return section + '\n        <ul role="list">' + "".join(rows) + "</ul>"
    return section


HOLDINGS_STYLE = """<style>
#simulation .holdings{overflow:hidden;
border:1px solid #dcded4;
border-radius:9px;
margin:14px 0;
background:#fffefa}
#simulation .holdings h3{margin:0;
padding:7px 16px;
background:#ebeee5;
color:#354433;
font-size:17px}
#simulation .holdings table{width:100%;
border-collapse:collapse;
table-layout:fixed}
#simulation .holdings th,
#simulation .holdings td{text-align:right;
padding:6px 12px;
font-variant-numeric:tabular-nums;
white-space:nowrap}
#simulation .holdings th{font-size:13px;
color:#565e54;
font-weight:500;
border-bottom:1px solid #e7e8df}
#simulation .holdings td{font-size:16px}
#simulation .holdings tbody tr+tr{border-top:1px solid #eeeee7}
#simulation .holdings th:first-child,
#simulation .holdings td:first-child{text-align:left;
white-space:normal;
overflow-wrap:anywhere}
#simulation .holdings .profit{color:#a54030}
#simulation .holdings .loss{color:#25694e}
@media(max-width:550px){#simulation .holdings th,
#simulation .holdings td{padding:7px 4px;
font-size:clamp(10px,3vw,14px)}
#simulation .holdings th:first-child{width:17%}
#simulation .holdings th:nth-child(2){width:23%}
#simulation .holdings th:nth-child(3){width:32%}
#simulation .holdings th:last-child{width:28%}}
</style>"""


def holding_number(value):
    from decimal import Decimal, InvalidOperation

    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and abs(result) < Decimal("1e18") else None
    except InvalidOperation:
        return None


def render_holdings(holdings, report_day, revision):
    if (
        not isinstance(holdings, dict)
        or holdings.get("schema") != "trend-v3-paper-holdings.v1"
        or holdings.get("date") != report_day.isoformat()
        or holdings.get("report_version") != revision
        or not isinstance(holdings.get("positions"), list)
    ):
        return '<p class="positions-note">持仓证据待核</p>'
    names = {
        "CRYPTO": "加密货币",
        "US": "美股",
        "HK": "港股",
        "CN": "A股",
        "JP": "日股",
        "SG": "新加坡股票",
        "AU": "澳股",
        "CA": "加拿大股票",
        "MY": "马股",
    }
    money = {
        "USD": "US$",
        "HKD": "HK$",
        "CNY": "¥",
        "JPY": "¥",
        "SGD": "S$",
        "AUD": "A$",
        "CAD": "C$",
        "MYR": "RM",
    }
    groups = {}
    for row in holdings["positions"]:
        if not isinstance(row, dict):
            continue
        code, market, product = row.get("code"), row.get("market"), row.get("product")
        if (
            not isinstance(code, str)
            or not PAPER_CODE.fullmatch(code)
            or market not in names
        ):
            continue
        if (
            product not in {"stock", "etf", "future", "crypto"}
            or row.get("currency") not in money
        ):
            continue
        qty = holding_number(row.get("quantity"))
        if qty == 0:
            continue
        average, pnl = (
            holding_number(row.get("average_entry")),
            holding_number(row.get("unrealized_pnl")),
        )
        prefix = money[row["currency"]]
        unit = (
            code.split(".")[-1]
            if product == "crypto"
            else "张"
            if product == "future"
            else "股"
        )
        quantity = (
            "—"
            if qty is None
            else format(qty, "f").rstrip("0").rstrip(".")
            if qty % 1
            else str(int(qty))
        )
        quantity = quantity.replace("-", "−") + (" " + unit if qty is not None else "")
        price = "—" if average is None or average < 0 else prefix + f"{average:,.2f}"
        profit = (
            "—"
            if pnl is None
            else ("+" if pnl > 0 else "−" if pnl < 0 else "")
            + prefix
            + f"{abs(pnl):,.2f}"
        )
        tone = (
            "profit"
            if pnl is not None and pnl > 0
            else "loss"
            if pnl is not None and pnl < 0
            else ""
        )
        label = names[market]
        if product == "future":
            label = {"HK": "香港期货", "US": "美国期货", "JP": "日本期货"}.get(
                market, label + "期货"
            )
        groups.setdefault(label, []).append(
            "<tr><td>"
            + escape(code.split(".", 1)[-1])
            + "</td><td>"
            + escape(quantity)
            + "</td><td>"
            + escape(price)
            + '</td><td class="'
            + tone
            + '">'
            + escape(profit)
            + "</td></tr>"
        )
    sections = []
    order = [
        "加密货币",
        "美股",
        "港股",
        "A股",
        "日股",
        "新加坡股票",
        "澳股",
        "加拿大股票",
        "马股",
        "美国期货",
        "香港期货",
        "日本期货",
    ]
    for label in sorted(
        groups, key=lambda name: order.index(name) if name in order else len(order)
    ):
        rows = groups[label]
        sections.append(
            '<div class="holdings"><h3>'
            + label
            + "</h3><table><thead><tr>"
            + "".join(
                '<th scope="col">' + title + "</th>"
                for title in ("标的", "持仓数量", "开仓均价", "持仓盈亏")
            )
            + "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
        )
    return (
        HOLDINGS_STYLE + "".join(sections)
        if sections
        else '<p class="positions-note">'
        + (
            "已核实的 V3 头寸已归零；其他市场待核"
            if holdings.get("verified_flat") is True
            else "持仓证据待核"
        )
        + "</p>"
    )
