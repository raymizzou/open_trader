from open_trader.notification_policy import (
    brief_zh_detail,
    group_order_alerts,
    render_attention,
    render_daily_title,
    render_order_alert,
    render_protection_alert,
)


def test_group_order_alerts_separates_direction_and_status() -> None:
    groups = group_order_alerts("US", [
        {"symbol": "HST", "side": "buy", "status": "incomplete", "target_qty": "20"},
        {"symbol": "HIG", "side": "buy", "status": "incomplete", "filled_qty": "5", "target_qty": "10"},
        {"symbol": "HIG", "side": "buy", "status": "incomplete", "filled_qty": "5", "target_qty": "10"},
        {"symbol": "SPY", "side": "sell", "status": "incomplete", "reason": "window closed"},
        {"symbol": "QQQ", "side": "buy", "status": "conflict", "reason": "broker order conflicts with immutable intent"},
    ])

    assert [(group.side, group.status, [item.symbol for item in group.items]) for group in groups] == [
        ("buy", "conflict", ["QQQ"]),
        ("buy", "incomplete", ["HIG", "HST"]),
        ("sell", "incomplete", ["SPY"]),
    ]


def test_order_alert_uses_only_supplied_ledger_fields() -> None:
    group = group_order_alerts("US", [
        {"symbol": "HIG", "side": "buy", "status": "incomplete", "filled_qty": "5", "target_qty": "10", "reason": "window closed"},
        {"symbol": "HST", "side": "buy", "status": "incomplete", "target_qty": "20"},
    ])[0]

    title, message = render_order_alert(group, broker_label="老虎", trading_date="2026-07-22")

    assert title == "【需处理｜老虎｜美股买入未完成｜2026-07-22】"
    assert "- HIG｜成交 5/10｜原因：窗口已关闭" in message
    assert "- HST｜目标 20" in message
    assert "OpenD" not in message


def test_fixed_feishu_title_levels() -> None:
    assert render_daily_title("老虎", "美股", "2026-07-22") == "【日报｜老虎｜美股趋势报告｜2026-07-22】"
    assert render_attention("系统", "OpenD 连接故障", "2026-07-22", happened="连接超时", impact="CN、HK、US 行情与订单监控可能中断", action="检查 OpenD 登录与网络") == (
        "【需处理｜系统｜OpenD 连接故障｜2026-07-22】",
        "发生：连接超时\n影响：CN、HK、US 行情与订单监控可能中断\n现在做：检查 OpenD 登录与网络",
    )
    assert render_protection_alert("老虎", "美股", "HIG", last_price="133.90", active_line="134.1650") == (
        "【紧急｜老虎｜美股保护线触发｜HIG】",
        "最新价：133.90\n活动保护线：134.1650\n现在做：人工确认并全部卖出",
    )
    assert brief_zh_detail("failed at /Users/ray/secret.json") == "详见控制器日志"
    assert brief_zh_detail("行情连接超时\ntraceback") == "行情连接超时"


def test_attention_hides_mixed_technical_detail() -> None:
    _, message = render_attention(
        "系统",
        "连接故障",
        "2026-07-22",
        happened="连接超时",
        impact="行情监控可能中断",
        action="检查网络",
        detail="行情连接超时 Traceback: /Users/ray/secret.json",
    )

    assert message.endswith("原因：详见控制器日志")
    assert "/Users/" not in message
    assert "Traceback" not in message


def test_order_alert_hides_arbitrary_chinese_containing_reason() -> None:
    group = group_order_alerts("US", [
        {
            "symbol": "HIG",
            "side": "buy",
            "status": "incomplete",
            "reason": "订单失败 Traceback: /private/token=secret",
        },
    ])[0]

    _, message = render_order_alert(group, broker_label="老虎", trading_date="2026-07-22")

    assert "原因：详见动作账本" in message
    assert "/private/" not in message
    assert "Traceback" not in message


def test_protection_alert_hides_non_numeric_price_detail() -> None:
    _, message = render_protection_alert(
        "老虎", "美股", "HIG", last_price="133.90", active_line="/Users/ray/secret"
    )

    assert "活动保护线：详见控制器日志" in message
    assert "/Users/" not in message


def test_group_order_alerts_skips_unsafe_symbol() -> None:
    groups = group_order_alerts("US", [{
        "symbol": "HIG Traceback: /Users/ray/secret",
        "side": "buy",
        "status": "incomplete",
        "target_qty": "10",
    }])

    assert groups == []


def test_order_alert_replaces_unsafe_quantity() -> None:
    group = group_order_alerts("US", [{
        "symbol": "HIG",
        "side": "buy",
        "status": "incomplete",
        "target_qty": "10 Traceback: /Users/ray/secret",
    }])[0]

    _, message = render_order_alert(group, broker_label="老虎", trading_date="2026-07-22")

    assert "- HIG｜目标 数量无效" in message
    assert "/Users/" not in message
    assert "Traceback" not in message


def test_protection_alert_normalizes_symbol_and_hides_unsafe_title_detail() -> None:
    title, _ = render_protection_alert(
        "老虎", "美股", "b1", last_price="133.90", active_line="134.1650"
    )
    unsafe_title, _ = render_protection_alert(
        "老虎",
        "美股",
        "HIG Traceback: /Users/ray/secret",
        last_price="133.90",
        active_line="134.1650",
    )

    assert unsafe_title == "【紧急｜老虎｜美股保护线触发｜未知标的】"
    assert "/Users/" not in unsafe_title
    assert "Traceback" not in unsafe_title
    assert title == "【紧急｜老虎｜美股保护线触发｜B1】"
