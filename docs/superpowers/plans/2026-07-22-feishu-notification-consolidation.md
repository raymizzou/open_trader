# Feishu Notification Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce Feishu noise to A1–A7, B1, C1–C3, and D7; consolidate OpenD incidents across markets; and group actionable order alerts without changing trading, Dashboard, macOS, or Xiaoai behavior.

**Architecture:** Keep the current producers and `send_notification_with_results`; add one pure notification-format/grouping module and one small JSON-backed OpenD incident module. Apply channel filters at existing call sites, keep legacy delivery records final, and use existing frozen/replay mechanisms where present; new delivery state gets only one retry.

**Tech Stack:** Python 3.12, stdlib dataclasses/JSON/fcntl/pathlib, pytest, existing `RunLock`, existing notifier classes, launchd.

## Global Constraints

- Start implementation from local `main` in a separate branch and worktree; this plan worktree is documentation-only.
- Add no dependency, event bus, notifier framework, environment variable, card protocol, or Dashboard data/schema change.
- New titles, OpenD consolidation, and order grouping apply only to Feishu; macOS and Xiaoai retain their existing title, body, and granularity.
- Preserve A1–A7, B1, C1–C3, and D7 in Feishu; suppress B2–B6 and D1–D6 from Feishu only.
- Reuse the existing A1/A2 frozen delivery and B1 per-channel replay behavior.
- Where no stable replay exists, allow the initial Feishu attempt plus one retry; then persist exhaustion and stop.
- Treat pre-deployment delivery facts as final; do not replay historical notifications.
- Never query OpenD or a broker merely to render a notification.
- Do not run `make acceptance`: this task changes no Dashboard source or data contract.
- Before completion, run focused tests, full pytest, controlled direct workflows, one real D7 notification, restart all three launchd controllers, and verify new PID/cwd/SHA/heartbeat/log evidence.

---

### Task 1: Pure Feishu text and order grouping

**Files:**
- Create: `src/open_trader/notification_policy.py`
- Create: `tests/test_notification_policy.py`

**Interfaces:**
- Consumes: immutable action-event mappings already written under `data/trend_review/ledgers/<MARKET>/actions/<DATE>/`.
- Produces: `OrderAlertItem`, `OrderAlertGroup`, `group_order_alerts(market, events)`, `render_order_alert(group, broker_label, trading_date)`, `brief_zh_detail(value)`, `render_daily_title(broker_label, market_label, report_date)`, `render_attention(source, problem, event_date, happened, impact, action, detail)`, and `render_protection_alert(broker_label, market_label, symbol, last_price, active_line)`.

- [ ] **Step 1: Write the failing pure-function tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify the module is missing**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest tests/test_notification_policy.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'open_trader.notification_policy'`.

- [ ] **Step 3: Implement the pure policy module**

```python
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


MARKET_LABELS = {"CN": "A股", "HK": "港股", "US": "美股"}
BROKER_LABELS = {"CN": "东方财富", "HK": "辉立", "US": "老虎"}
SIDE_LABELS = {"buy": "买入", "sell": "卖出"}
STATUS_LABELS = {
    "pending": "待执行",
    "submitted": "已提交",
    "partially_filled": "部分成交",
    "failed": "失败",
    "blocked": "受阻",
    "uncertain": "状态不确定",
    "conflict": "订单冲突",
    "missed": "错过窗口",
    "missed_window": "错过窗口",
    "incomplete": "未完成",
}
STATUS_ACTIONS = {
    "pending": "检查阻塞原因并人工决定是否继续",
    "submitted": "检查订单是否仍在等待成交",
    "partially_filled": "检查剩余数量并人工决定是否撤单",
    "failed": "检查订单失败原因后人工处理",
    "blocked": "解除阻塞前不要自动重试",
    "uncertain": "核对不可变账本与券商订单，禁止自动重试",
    "conflict": "核对冲突订单，禁止自动提交",
    "missed": "今日不再追单",
    "missed_window": "今日不再追单",
    "incomplete": "核对券商订单并人工处理",
}
REASON_LABELS = {
    "window closed": "窗口已关闭",
    "buy_window_closed": "买入窗口已关闭",
    "broker order conflicts with immutable intent": "券商订单与不可变账本冲突",
    "position_zero_confirmed": "持仓数量已确认为零",
}


@dataclass(frozen=True)
class OrderAlertItem:
    symbol: str
    filled_qty: str = ""
    target_qty: str = ""
    reason: str = ""


@dataclass(frozen=True)
class OrderAlertGroup:
    market: str
    side: str
    status: str
    items: tuple[OrderAlertItem, ...]


def group_order_alerts(
    market: str, events: Iterable[Mapping[str, object]]
) -> list[OrderAlertGroup]:
    normalized_market = market.strip().upper()
    grouped: dict[tuple[str, str], dict[str, OrderAlertItem]] = {}
    for event in events:
        symbol = str(event.get("symbol") or "").strip().upper()
        side = str(event.get("side") or "").strip().lower()
        status = str(event.get("status") or "").strip().lower()
        if not symbol or side not in SIDE_LABELS or status not in STATUS_LABELS:
            continue
        grouped.setdefault((side, status), {})[symbol] = OrderAlertItem(
            symbol=symbol,
            filled_qty=str(event.get("filled_qty") or "").strip(),
            target_qty=str(event.get("target_qty") or "").strip(),
            reason=_reason_label(event.get("reason")),
        )
    return [
        OrderAlertGroup(
            market=normalized_market,
            side=side,
            status=status,
            items=tuple(items[symbol] for symbol in sorted(items)),
        )
        for (side, status), items in sorted(grouped.items())
    ]


def _reason_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in REASON_LABELS:
        return REASON_LABELS[text]
    return text if any("\u4e00" <= character <= "\u9fff" for character in text) else "详见动作账本"


def brief_zh_detail(value: object) -> str:
    text = str(value or "").strip().splitlines()[0] if str(value or "").strip() else ""
    if not text:
        return ""
    if "/Users/" in text or "/private/" in text or not any("\u4e00" <= character <= "\u9fff" for character in text):
        return "详见控制器日志"
    return text[:160]


def render_daily_title(broker_label: str, market_label: str, report_date: str) -> str:
    return f"【日报｜{broker_label}｜{market_label}趋势报告｜{report_date}】"


def render_attention(
    source: str,
    problem: str,
    event_date: str,
    *,
    happened: str,
    impact: str,
    action: str,
    detail: str = "",
) -> tuple[str, str]:
    lines = [f"发生：{happened}", f"影响：{impact}", f"现在做：{action}"]
    if detail:
        lines.append(f"原因：{detail}")
    return f"【需处理｜{source}｜{problem}｜{event_date}】", "\n".join(lines)


def render_protection_alert(
    broker_label: str,
    market_label: str,
    symbol: str,
    *,
    last_price: object,
    active_line: object,
) -> tuple[str, str]:
    return (
        f"【紧急｜{broker_label}｜{market_label}保护线触发｜{symbol}】",
        f"最新价：{last_price}\n活动保护线：{active_line}\n现在做：人工确认并全部卖出",
    )


def render_order_alert(
    group: OrderAlertGroup, *, broker_label: str, trading_date: str
) -> tuple[str, str]:
    market_label = MARKET_LABELS[group.market]
    status_label = STATUS_LABELS[group.status]
    problem = f"{market_label}{SIDE_LABELS[group.side]}{status_label}"
    lines = [
        f"发生：{len(group.items)} 个标的订单{status_label}",
        f"影响：{market_label}{SIDE_LABELS[group.side]}订单需要人工确认",
        f"现在做：{STATUS_ACTIONS[group.status]}",
        "",
        "标的：",
    ]
    for item in group.items:
        details: list[str] = []
        if item.filled_qty and item.target_qty:
            details.append(f"成交 {item.filled_qty}/{item.target_qty}")
        elif item.target_qty:
            details.append(f"目标 {item.target_qty}")
        if item.reason:
            details.append(f"原因：{item.reason}")
        suffix = f"｜{'｜'.join(details)}" if details else ""
        lines.append(f"- {item.symbol}{suffix}")
    return f"【需处理｜{broker_label}｜{problem}｜{trading_date}】", "\n".join(lines)
```

- [ ] **Step 4: Run the pure tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest tests/test_notification_policy.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit the pure module**

```bash
git add src/open_trader/notification_policy.py tests/test_notification_policy.py
git commit -m "feat: add Feishu notification policy renderers"
```

---

### Task 2: A/B notification titles and Feishu routing

**Files:**
- Modify: `src/open_trader/a_share_trend.py:2598-2691`
- Modify: `src/open_trader/a_share_trend_watch.py:350-856`
- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_market_trend.py`
- Modify: `tests/test_a_share_trend_watch.py`
- Modify: `tests/test_market_trend_watch.py`

**Interfaces:**
- Consumes: Task 1 `render_daily_title`, `render_attention`, and `render_protection_alert`; existing `send_notification_with_results` channel filter.
- Produces: A1/A2 and B1 Feishu text in the approved title levels; B2–B6 event facts without Feishu; unchanged macOS/Xiaoai B1/B4/B5 payloads.

- [ ] **Step 1: Change existing report-title expectations and add a channel-boundary test**

Update every A1 title assertion from:

```python
"【东方财富｜A股趋势报告｜2026-07-15】"
"【辉立｜港股趋势报告｜2026-07-16】"
"【老虎｜美股趋势报告｜2026-07-15】"
```

to:

```python
"【日报｜东方财富｜A股趋势报告｜2026-07-15】"
"【日报｜辉立｜港股趋势报告｜2026-07-16】"
"【日报｜老虎｜美股趋势报告｜2026-07-15】"
```

Update A2 title assertions to:

```python
"【需处理｜东方财富｜A股趋势报告生成失败｜2026-07-15】"
"【需处理｜老虎｜美股趋势报告生成失败｜2026-07-15】"
```

Add this B-series integration test to `tests/test_a_share_trend_watch.py` using its existing notifier fakes:

```python
def test_feishu_policy_keeps_only_b1_and_preserves_other_channels(tmp_path: Path) -> None:
    feishu = RecordingNotifier()
    macos = RecordingMacOSNotifier()
    voice = RecordingXiaoaiNotifier()
    notifier = CompositeNotifier([feishu, macos, voice])

    _deliver_trigger_notification(
        events_path=tmp_path / "events.jsonl",
        notifier=notifier,
        trading_date="2026-07-22",
        now=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
        symbol="600900",
        position_name="长江电力",
        last_price=Decimal("27.30"),
        active_line=Decimal("27.31"),
        delivered_feishu=set(),
        delivered_macos=set(),
        replay=False,
        market_label="A股",
        broker_label="东方财富",
    )

    assert feishu.messages == [(
        "【紧急｜东方财富｜A股保护线触发｜600900】",
        "最新价：27.30\n活动保护线：27.31\n现在做：人工确认并全部卖出",
    )]
    assert macos.messages == [(
        "A股保护线触发 · 600900",
        "最新价 27.30 <= 活动保护线 27.31\n建议动作：全部卖出（人工执行）",
    )]
    assert voice.messages[0][0] == "A股保护线触发 · 600900"
```

For the existing missing-line and unknown-quote tests, keep `protection_line_missing` and `quote_unknown` event expectations but remove the `*_notification_delivered` facts and assert `feishu.messages == []`. For interruption/recovery tests, use a composite notifier and assert Feishu is empty while macOS retains the current titles. For Xiaoai failure, retain `protection_triggered_notification_failed_xiaoai` and assert no “语音播报失败” Feishu message.

- [ ] **Step 2: Run the focused tests to verify old titles/routes fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py -q
```

Expected: failures show old report titles, old B1 title, and B2–B6 Feishu messages still present.

- [ ] **Step 3: Apply the report titles**

In `render_trend_feishu_text`, replace the title assignment with:

```python
title = render_daily_title(broker_label, market_label, execution_date)
```

In `render_trend_failure_text`, return:

```python
return render_attention(
    broker_label,
    f"{market_label}趋势报告生成失败",
    report_date,
    happened="趋势报告未生成",
    impact="不能依据旧报告交易",
    action=recovery_action,
    detail=reason,
)
```

Import both renderers from `notification_policy`.

- [ ] **Step 4: Apply B1–B6 routing without changing event detection**

Extend `_deliver_trigger_notification` with `broker_label: str = "东方财富"`. In its Feishu branch, call `render_protection_alert(broker_label, market_label, symbol, last_price=last_price, active_line=active_line)`; keep the current title/body in the macOS and Xiaoai branches. Pass `broker_label` from both CN and HK/US watcher call sites.

Delete only the two Feishu send blocks for missing lines and unknown quotes; leave `append_watch_event`, counters, fail-closed flow, and Dashboard input unchanged. Route interruption and recovery through:

```python
send_notification_with_results(
    notifier,
    current_title,
    current_message,
    channels={"macos", "xiaoai"},
)
```

After a Xiaoai delivery failure, retain the existing `protection_triggered_notification_failed_xiaoai` event and remove the trailing Feishu send call entirely.

- [ ] **Step 5: Run the focused tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py -q
```

Expected: all four files pass; B2/B3 facts remain and B2–B6 produce no Feishu records.

- [ ] **Step 6: Commit A/B behavior**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/a_share_trend_watch.py \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py
git commit -m "feat: apply Feishu A and B notification policy"
```

---

### Task 3: D1–D6 Feishu suppression

**Files:**
- Modify: `src/open_trader/daily_premarket.py:1465-1491`
- Modify: `src/open_trader/decision_plan_watch.py:126-156`
- Modify: `src/open_trader/t_signal_runner.py:198-250`
- Modify: `tests/test_daily_premarket.py`
- Modify: `tests/test_decision_plan_watch.py`
- Modify: `tests/test_t_signal_runner.py`

**Interfaces:**
- Consumes: existing `send_notification_with_results` and existing event/artifact stores.
- Produces: D1–D6 sent only to configured `macos`/`xiaoai`; D7 remains untouched in `cli.py`.

- [ ] **Step 1: Add failing route tests**

Use the existing Feishu and macOS fakes in `tests/test_daily_premarket.py` to assert `_notify` sends an old daily title only to macOS. Add these assertions to the decision-plan and T-signal tests with `CompositeNotifier([feishu, macos])`:

```python
from open_trader.notifications import CompositeNotifier, FeishuWebhookNotifier, MacOSNotifier


class RecordingFeishu(FeishuWebhookNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class RecordingMacOS(MacOSNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


assert feishu.messages == []
assert len(macos.messages) == 1
assert events[-1].event_type == "notification_sent"
```

Add a Feishu-only case for each watcher:

```python
assert feishu.messages == []
assert events[-1].event_type == "notification_suppressed"
```

The T-signal artifact must retain the signal in the UI, set `should_notify` false for the same dedupe key, and must not change the signal to error/review merely because Feishu was excluded.

- [ ] **Step 2: Run the tests to verify D messages still reach Feishu**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest \
  tests/test_daily_premarket.py \
  tests/test_decision_plan_watch.py \
  tests/test_t_signal_runner.py -q
```

Expected: new route assertions fail because the three paths call all configured notifiers.

- [ ] **Step 3: Filter the old daily runner**

Change `DailyPremarketRunner._notify` to:

```python
attempts = send_notification_with_results(
    self.notifier,
    title,
    message,
    channels={"macos", "xiaoai"},
)
```

Keep its CSV logging unchanged.

- [ ] **Step 4: Filter decision-plan triggers and preserve their facts**

Import `send_notification_with_results` from `daily_premarket`. Replace the direct `notifier.notify` block with:

```python
attempts = send_notification_with_results(
    notifier,
    f"交易计划触发 · {plan['market']}.{plan['symbol']}",
    _notification_message(plan, condition, snapshot.last_price),
    channels={"macos", "xiaoai"},
)
failures = [attempt for attempt in attempts if not attempt.success and not attempt.suppressed]
if failures:
    failed_count += 1
    notification_type = "notification_failed"
    payload = {"error": failures[0].error or failures[0].error_type}
elif attempts:
    sent_count += 1
    notification_type = "notification_sent"
    payload = {}
else:
    notification_type = "notification_suppressed"
    payload = {"reason": "feishu_disabled_by_policy"}
```

- [ ] **Step 5: Filter T signals and preserve their UI state**

Replace the direct notifier call in `_apply_notification_state` with:

```python
attempts = send_notification_with_results(
    notifier,
    _notification_title(signal),
    _notification_message(signal),
    channels={"macos", "xiaoai"},
)
```

If the result list is empty, append a `notification_suppressed` timeline event, set `should_notify=False`, retain the normal signal status, and set `last_attempted_dedupe_key` so the same D6 signal is not reconsidered every run. If a non-Feishu channel fails, keep the existing `notification_failed` path; if one succeeds, keep `notification_sent`.

- [ ] **Step 6: Run D-series tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest \
  tests/test_daily_premarket.py \
  tests/test_decision_plan_watch.py \
  tests/test_t_signal_runner.py \
  tests/test_premarket_cli.py -q
```

Expected: all selected tests pass, including the unchanged D7 CLI tests.

- [ ] **Step 7: Commit D routing**

```bash
git add src/open_trader/daily_premarket.py src/open_trader/decision_plan_watch.py \
  src/open_trader/t_signal_runner.py tests/test_daily_premarket.py \
  tests/test_decision_plan_watch.py tests/test_t_signal_runner.py
git commit -m "feat: suppress legacy watcher alerts from Feishu"
```

---

### Task 4: Controller channel-aware delivery and system protection blocker

**Files:**
- Modify: `src/open_trader/trend_market_controller.py:999-1050, 1880-2175, 2210-2280`
- Modify: `tests/test_trend_market_controller.py`

**Interfaces:**
- Consumes: Task 1 `BROKER_LABELS`, `MARKET_LABELS`, `brief_zh_detail`, and `render_attention`; existing controller notification JSON directory and `send_notification_with_results`.
- Produces: v2 controller delivery records with separate channel success, `_retry_pending_feishu_notifications(config)`, `_notify_protection_blocker(config, market, trading_date, protection_error, occurred_at)`, a maximum of two Feishu attempts, legacy-v1 finality, old non-Feishu copy, new Feishu A3/A4/A7 copy, and one A3 protection blocker per market/day.

- [ ] **Step 1: Add delivery-state tests**

Add tests around `_notify_once` using a `CompositeNotifier` whose macOS succeeds and Feishu fails once:

```python
from open_trader.notifications import CompositeNotifier, FeishuWebhookNotifier, MacOSNotifier


class FlakyFeishu(FeishuWebhookNotifier):
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempt_count = 0

    def notify(self, title: str, message: str) -> None:
        self.attempt_count += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("Feishu unavailable")


class RecordingMacOS(MacOSNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


def test_controller_notification_retries_only_feishu_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = replace(controller_config(tmp_path), notifiers=("feishu", "macos"))
    feishu = FlakyFeishu(failures=1)
    macos = RecordingMacOS()
    monkeypatch.setattr(controller, "build_notifier", lambda _config: CompositeNotifier([feishu, macos]))
    key = (config, "US", "2026-07-22", "controller", "snapshot_failed", "2026-07-22T10:00:00+08:00")

    assert controller._notify_once("US 趋势控制器阻塞", "snapshot unavailable", key) is False
    controller._retry_pending_feishu_notifications(config)
    assert controller._notify_once("US 趋势控制器阻塞", "snapshot unavailable", key) is True

    assert feishu.attempt_count == 2
    assert len(macos.messages) == 1
```

Add the exhaustion, legacy, and protection tests explicitly:

```python
def test_controller_notification_stops_after_one_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = controller_config(tmp_path)
    feishu = FlakyFeishu(failures=2)
    monkeypatch.setattr(controller, "build_notifier", lambda _config: CompositeNotifier([feishu]))
    key = (config, "US", "2026-07-22", "controller", "snapshot_failed", "2026-07-22T10:00:00+08:00")

    assert controller._notify_once("US 趋势控制器阻塞", "snapshot unavailable", key) is False
    controller._retry_pending_feishu_notifications(config)
    controller._retry_pending_feishu_notifications(config)

    assert feishu.attempt_count == 2


def test_legacy_controller_notification_is_not_replayed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = controller_config(tmp_path)
    feishu = FlakyFeishu(failures=0)
    macos = RecordingMacOS()
    monkeypatch.setattr(controller, "build_notifier", lambda _config: CompositeNotifier([feishu, macos]))
    identity = "|".join(("US", "2026-07-22", "controller", "snapshot_failed"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    path = config.data_dir / "trend_controller/US/notifications/2026-07-22" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "open_trader.trend_controller.notification.v1",
        "market": "US",
        "execution_date": "2026-07-22",
        "action": "controller",
        "reason": "snapshot_failed",
        "notified_at": "2026-07-22T09:00:00+08:00",
        "channels": ["macos"],
    }), encoding="utf-8")

    key = (config, "US", "2026-07-22", "controller", "snapshot_failed", "2026-07-22T10:00:00+08:00")
    assert controller._notify_once("US 趋势控制器阻塞", "snapshot unavailable", key) is True
    assert feishu.attempt_count == 0
    assert macos.messages == []


def test_protection_blocker_notifies_feishu_once_per_market_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = controller_config(tmp_path)
    feishu = FlakyFeishu(failures=0)
    monkeypatch.setattr(controller, "build_notifier", lambda _config: CompositeNotifier([feishu]))

    for occurred_at in ("2026-07-22T09:31:00+08:00", "2026-07-22T09:31:05+08:00"):
        controller._notify_protection_blocker(
            config,
            "CN",
            "2026-07-22",
            "protection pass abnormal: unknown_quotes=2",
            occurred_at,
        )

    assert feishu.attempt_count == 1
```

- [ ] **Step 2: Run the controller tests to verify channel-aware behavior is absent**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest \
  tests/test_trend_market_controller.py::test_controller_notification_retries_only_feishu_once \
  tests/test_trend_market_controller.py::test_protection_blocker_notifies_feishu_once_per_market_day -q
```

Expected: both tests fail because the current v1 ledger treats any channel success as final and protection blocking writes status only.

- [ ] **Step 3: Replace `_notify_once` internals without changing its three-argument interface**

Keep `_notify_once(title, message, key)` so existing controller tests and call sites remain stable. Add private `_notify_channels_once(title, message, key, *, send_feishu, send_non_feishu)` and an atomic v2 writer. The state payload is exactly:

```python
{
    "schema_version": "open_trader.trend_controller.notification.v2",
    "market": market,
    "execution_date": execution_date,
    "action": action,
    "reason": reason,
    "occurred_at": occurred_at,
    "non_feishu_attempted": True,
    "feishu_attempts": 1,
    "feishu_title": "【需处理｜老虎｜美股趋势控制器阻塞｜2026-07-22】",
    "feishu_message": "发生：趋势控制器已进入阻塞状态\n影响：美股自动趋势流程暂停\n现在做：检查 Dashboard 控制器状态与最近日志\n原因：详见控制器日志",
    "channels": ["macos"],
}
```

Rules in `_notify_channels_once`:

```python
if path.exists() and payload["schema_version"] == "open_trader.trend_controller.notification.v1":
    return True
if send_non_feishu and not state["non_feishu_attempted"]:
    send current title/message once with channels={"macos", "xiaoai"}
if send_feishu and "feishu"/"feishu_app" not delivered and state["feishu_attempts"] < 2:
    send the Feishu payload once and increment feishu_attempts
write the v2 state atomically even when every attempt fails
never resend a channel already listed in channels
```

Define all three wrappers with the existing three positional arguments so monkeypatched tests remain compatible:

```python
def _notify_once(title: str, message: str, key: object) -> bool:
    config, market, execution_date, action, _reason, _occurred_at = _notification_key(key)
    return _notify_channels_once(
        key,
        non_feishu_payload=(title, message),
        feishu_payload=_controller_feishu_payload(
            title,
            message,
            market=market,
            execution_date=execution_date,
            action=action,
        ),
    )


def _notify_non_feishu_once(title: str, message: str, key: object) -> bool:
    return _notify_channels_once(
        key,
        non_feishu_payload=(title, message),
        feishu_payload=None,
    )


def _notify_feishu_once(title: str, message: str, key: object) -> bool:
    return _notify_channels_once(
        key,
        non_feishu_payload=None,
        feishu_payload=(title, message),
    )
```

`_notification_key` retains the current six-field validation and returns `(DailyPremarketConfig, str, str, str, str, str)`. `_notify_channels_once` accepts the key plus optional `(title, message)` payloads, applies the v1/v2 rules above, and is the only function that reads or atomically replaces controller notification state.

Use `_notify_once` for both channel families, `_notify_non_feishu_once` for the OpenD shared path, and `_notify_feishu_once` for grouped/protection-only alerts. `_notify_once` renders the Feishu payload with `render_attention` while passing the current title/body unchanged to non-Feishu channels. Map controller actions to stable problem labels; use the short original message only as the final detail, never a path or traceback.

Store the frozen Feishu title/body in v2. `_retry_pending_feishu_notifications(config)` scans only v2 JSON below `data/trend_controller/*/notifications/`; for each undelivered record with `feishu_attempts == 1`, it sends the stored payload once to `{"feishu", "feishu_app"}`, writes attempt 2 and any successful channel atomically, and never touches v1 or exhausted records. Call it once near the top of each controller loop after the heartbeat write. This is the only proactive retry scan.

The controller Feishu renderer is explicit and receives the already-unpacked key fields:

```python
def _controller_feishu_payload(
    title: str,
    message: str,
    *,
    market: str,
    execution_date: str,
    action: str,
) -> tuple[str, str]:
    broker = BROKER_LABELS[market]
    market_label = MARKET_LABELS[market]
    if action == "revision_after_batch_lock":
        return render_attention(
            broker,
            f"{market_label}趋势报告修订异常",
            execution_date,
            happened="执行批次锁定后报告发生修订",
            impact="当日自动操作继续使用已锁定版本",
            action="核对冻结报告与修订记录",
            detail=brief_zh_detail(message),
        )
    if "复盘" in title:
        return render_attention(
            broker,
            f"{market_label}趋势复盘待恢复",
            execution_date,
            happened="趋势复盘未完成",
            impact="复盘数据暂未更新",
            action="检查 OpenD 与复盘账本后等待自动恢复",
            detail=brief_zh_detail(message),
        )
    return render_attention(
        broker,
        f"{market_label}趋势控制器阻塞",
        execution_date,
        happened="趋势控制器已进入阻塞状态",
        impact=f"{market_label}自动趋势流程暂停",
        action="检查 Dashboard 控制器状态与最近日志",
        detail=brief_zh_detail(message),
    )
```

- [ ] **Step 4: Add the protection-blocker A3 call**

Immediately after `_protection_blocker` returns a non-empty value, call `_notify_feishu_once` with this stable identity:

```python
(
    config,
    market,
    local.date().isoformat(),
    "protection_monitor_blocked",
    "protection_pass_abnormal",
    now.isoformat(timespec="seconds"),
)
```

Render it as:

```python
render_attention(
    broker_label,
    f"{market_label}保护监控阻塞",
    local.date().isoformat(),
    happened="保护检查整体异常，已禁止新买入",
    impact=f"{market_label}活动保护线无法完整检查",
    action="查看 Dashboard 风险状态并人工核价",
    detail=protection_error,
)
```

The fixed identity makes this at most one event per market/day. Do not send individual B2/B3 facts through this path.

Put that rendering and identity in `_notify_protection_blocker(...)` so the controller loop and test share the same function; call it only when `protection_error is not None`.

- [ ] **Step 5: Run the full controller test file**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest tests/test_trend_market_controller.py -q
```

Expected: all tests pass; v1 records do not replay and v2 records retry only missing Feishu once.

- [ ] **Step 6: Commit controller delivery**

```bash
git add src/open_trader/trend_market_controller.py tests/test_trend_market_controller.py
git commit -m "feat: track controller notification delivery by channel"
```

---

### Task 5: Shared OpenD incident state

**Files:**
- Create: `src/open_trader/opend_incident.py`
- Create: `tests/test_opend_incident.py`
- Modify: `src/open_trader/trend_market_controller.py`
- Modify: `tests/test_trend_market_controller.py`

**Interfaces:**
- Consumes: `RunLock(wait=True)`, controller status JSON, Task 1 `render_attention`, and Task 4 `_notify_non_feishu_once`/`_notify_once` fallback.
- Produces: `classify_opend_error(error: BaseException) -> Literal["connectivity", "rate_limit"] | None`, `record_opend_failure(data_dir, market, category, reason, occurred_at, send_feishu, title, message) -> bool`, `record_opend_health(data_dir: Path, market: str, observed_at: datetime) -> None`, and `OpenDIncidentStateError`.

- [ ] **Step 1: Write classifier and incident-state tests**

```python
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from open_trader.futu_quote import FutuQuoteError
from open_trader.opend_incident import (
    OpenDIncidentStateError,
    classify_opend_error,
    record_opend_failure,
    record_opend_health,
)


def incident_path(data_dir: Path, category: str = "connectivity") -> Path:
    return data_dir / "trend_controller/shared/incidents" / f"opend-{category.replace('_', '-')}.json"


def read_incident(data_dir: Path, category: str = "connectivity") -> dict[str, object]:
    return json.loads(incident_path(data_dir, category).read_text(encoding="utf-8"))


def create_active_incident(data_dir: Path, category: str) -> None:
    path = incident_path(data_dir, category)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "open_trader.opend_incident.v1",
        "category": category,
        "active": True,
        "first_detected_at": "2026-07-22T09:59:00+08:00",
        "updated_at": "2026-07-22T09:59:00+08:00",
        "affected_markets": ["CN"],
        "reasons": {"CN": "连接超时"},
        "healthy_markets": [],
        "feishu_attempts": 1,
        "feishu_delivered_at": "2026-07-22T09:59:00+08:00",
        "channels": ["feishu_app"],
    }, ensure_ascii=False), encoding="utf-8")


def write_controller_statuses(data_dir: Path, *, cn: str, hk: str, us: str) -> None:
    for market, clock in {"CN": cn, "HK": hk, "US": us}.items():
        path = data_dir / "trend_controller" / market / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"heartbeat_at": f"2026-07-22T{clock}+08:00"}), encoding="utf-8")


def test_classifies_known_opend_failures_without_guessing() -> None:
    assert classify_opend_error(FutuQuoteError("down", error_type="opend_unreachable", opend_reachable=False)) == "connectivity"
    assert classify_opend_error(RuntimeError("获取历史K线频率太高")) == "rate_limit"
    assert classify_opend_error(RuntimeError("Connect timeout")) == "connectivity"
    assert classify_opend_error(RuntimeError("unknown broker response")) is None


def test_three_concurrent_markets_send_one_incident(tmp_path: Path) -> None:
    sent: list[tuple[str, str]] = []
    barrier = threading.Barrier(3)

    def report(market: str) -> None:
        barrier.wait()
        record_opend_failure(
            data_dir=tmp_path,
            market=market,
            category="connectivity",
            reason="Connect timeout",
            occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
            send_feishu=lambda title, message: sent.append((title, message)) or "feishu_app",
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(report, ("CN", "HK", "US")))

    assert len(sent) == 1
    state = json.loads((tmp_path / "trend_controller/shared/incidents/opend-connectivity.json").read_text())
    assert state["affected_markets"] == ["CN", "HK", "US"]


def test_recovery_requires_every_fresh_controller_but_ignores_stale_heartbeat(tmp_path: Path) -> None:
    write_controller_statuses(tmp_path, cn="10:00:00", hk="10:00:00", us="09:57:59")
    create_active_incident(tmp_path, category="connectivity")

    record_opend_health(tmp_path, "CN", datetime.fromisoformat("2026-07-22T10:00:30+08:00"))
    assert read_incident(tmp_path)["active"] is True
    record_opend_health(tmp_path, "HK", datetime.fromisoformat("2026-07-22T10:00:30+08:00"))

    assert read_incident(tmp_path)["active"] is False


def test_recovery_waits_for_all_three_when_all_heartbeats_are_fresh(tmp_path: Path) -> None:
    write_controller_statuses(tmp_path, cn="10:00:00", hk="10:00:00", us="10:00:00")
    create_active_incident(tmp_path, category="connectivity")
    observed = datetime.fromisoformat("2026-07-22T10:00:30+08:00")

    record_opend_health(tmp_path, "CN", observed)
    record_opend_health(tmp_path, "HK", observed)
    assert read_incident(tmp_path)["active"] is True
    record_opend_health(tmp_path, "US", observed)

    assert read_incident(tmp_path)["active"] is False


def test_categories_are_separate_and_each_stops_after_one_retry(tmp_path: Path) -> None:
    attempts = {"connectivity": 0, "rate_limit": 0}
    for category in ("connectivity", "rate_limit"):
        for _ in range(3):
            record_opend_failure(
                data_dir=tmp_path,
                market="US",
                category=category,
                reason="连接异常" if category == "connectivity" else "请求限频",
                occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
                send_feishu=lambda _title, _message, category=category: attempts.__setitem__(category, attempts[category] + 1),
            )

    assert attempts == {"connectivity": 2, "rate_limit": 2}
    assert read_incident(tmp_path, "connectivity")["feishu_attempts"] == 2
    assert read_incident(tmp_path, "rate_limit")["feishu_attempts"] == 2


def test_recovered_incident_can_send_again(tmp_path: Path) -> None:
    delivered: list[str] = []
    record_opend_failure(
        data_dir=tmp_path,
        market="CN",
        category="connectivity",
        reason="连接超时",
        occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
        send_feishu=lambda _title, _message: delivered.append("first") or "feishu_app",
    )
    write_controller_statuses(tmp_path, cn="10:00:05", hk="09:57:00", us="09:57:00")
    record_opend_health(tmp_path, "CN", datetime.fromisoformat("2026-07-22T10:00:05+08:00"))
    record_opend_failure(
        data_dir=tmp_path,
        market="CN",
        category="connectivity",
        reason="连接再次超时",
        occurred_at=datetime.fromisoformat("2026-07-22T10:01:00+08:00"),
        send_feishu=lambda _title, _message: delivered.append("second") or "feishu_app",
    )

    assert delivered == ["first", "second"]


def test_malformed_incident_state_raises_for_controller_fallback(tmp_path: Path) -> None:
    path = incident_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(OpenDIncidentStateError):
        record_opend_failure(
            data_dir=tmp_path,
            market="CN",
            category="connectivity",
            reason="连接超时",
            occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
            send_feishu=lambda _title, _message: "feishu_app",
        )
```

- [ ] **Step 2: Run the new tests to verify the module is absent**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest tests/test_opend_incident.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'open_trader.opend_incident'`.

- [ ] **Step 3: Implement the JSON state machine**

Use these exact paths below `data_dir`:

```python
lock_path = data_dir / "trend_controller/shared/opend-incidents.lock"
incident_path = data_dir / "trend_controller/shared/incidents" / f"opend-{category.replace('_', '-')}.json"
```

State schema:

```python
{
    "schema_version": "open_trader.opend_incident.v1",
    "category": category,
    "active": True,
    "first_detected_at": occurred_at.isoformat(timespec="seconds"),
    "updated_at": occurred_at.isoformat(timespec="seconds"),
    "affected_markets": [market],
    "reasons": {market: reason},
    "healthy_markets": [],
    "feishu_attempts": 0,
    "feishu_delivered_at": "",
    "channels": [],
}
```

Implement the module with this state transition code (small private read/write helpers stay in the same file):

```python
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from .daily_premarket import RunLock


OpenDCategory = Literal["connectivity", "rate_limit"]
SendFeishu = Callable[[str, str], str | None]
SCHEMA = "open_trader.opend_incident.v1"
CATEGORIES: tuple[OpenDCategory, ...] = ("connectivity", "rate_limit")


class OpenDIncidentStateError(RuntimeError):
    pass


def classify_opend_error(error: BaseException) -> OpenDCategory | None:
    error_type = str(getattr(error, "error_type", "")).lower()
    message = str(error).lower()
    if error_type == "opend_unreachable" or getattr(error, "opend_reachable", None) is False:
        return "connectivity"
    if any(token in message for token in ("频率太高", "rate limit", "too many requests")):
        return "rate_limit"
    if error_type == "quote_server_interrupted" or any(token in message for token in (
        "connect timeout", "connection refused", "network down",
        "protocol disconnected", "网络中断",
    )):
        return "connectivity"
    return None


def _incident_path(data_dir: Path, category: OpenDCategory) -> Path:
    return data_dir / "trend_controller/shared/incidents" / f"opend-{category.replace('_', '-')}.json"


def _read(path: Path, category: OpenDCategory) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA or payload.get("category") != category:
        raise ValueError(f"invalid OpenD incident state: {path}")
    return payload


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def record_opend_failure(
    *,
    data_dir: Path,
    market: str,
    category: OpenDCategory,
    reason: str,
    occurred_at: datetime,
    send_feishu: SendFeishu,
    title: str = "",
    message: str = "",
) -> bool:
    lock_path = data_dir / "trend_controller/shared/opend-incidents.lock"
    path = _incident_path(data_dir, category)
    now_text = occurred_at.isoformat(timespec="seconds")
    try:
        with RunLock(lock_path, wait=True):
            state = _read(path, category)
            if state is None or state.get("active") is not True:
                state = {
                    "schema_version": SCHEMA,
                    "category": category,
                    "active": True,
                    "first_detected_at": now_text,
                    "updated_at": now_text,
                    "affected_markets": [],
                    "reasons": {},
                    "healthy_markets": [],
                    "feishu_attempts": 0,
                    "feishu_delivered_at": "",
                    "channels": [],
                }
            affected = {str(item) for item in state["affected_markets"]}
            affected.add(market)
            reasons = dict(state["reasons"])
            reasons[market] = reason
            state["affected_markets"] = sorted(affected)
            state["reasons"] = reasons
            state["updated_at"] = now_text
            if not state["feishu_delivered_at"] and int(state["feishu_attempts"]) < 2:
                state["feishu_attempts"] = int(state["feishu_attempts"]) + 1
                try:
                    delivered_channel = send_feishu(title, message)
                except Exception:
                    delivered_channel = None
                if delivered_channel:
                    state["feishu_delivered_at"] = now_text
                    state["channels"] = [delivered_channel]
            _write(path, state)
            return bool(state["feishu_delivered_at"])
    except Exception as exc:
        if isinstance(exc, OpenDIncidentStateError):
            raise
        raise OpenDIncidentStateError(str(exc)) from exc


def _fresh_markets(data_dir: Path, observed_at: datetime) -> set[str]:
    fresh: set[str] = set()
    for market in ("CN", "HK", "US"):
        try:
            payload = json.loads((data_dir / "trend_controller" / market / "status.json").read_text(encoding="utf-8"))
            heartbeat = datetime.fromisoformat(str(payload["heartbeat_at"]))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError):
            continue
        if abs(observed_at - heartbeat) <= timedelta(minutes=2):
            fresh.add(market)
    return fresh


def record_opend_health(data_dir: Path, market: str, observed_at: datetime) -> None:
    lock_path = data_dir / "trend_controller/shared/opend-incidents.lock"
    try:
        with RunLock(lock_path, wait=True):
            for category in CATEGORIES:
                path = _incident_path(data_dir, category)
                state = _read(path, category)
                if state is None or state.get("active") is not True:
                    continue
                healthy = {str(item) for item in state["healthy_markets"]}
                healthy.add(market)
                state["healthy_markets"] = sorted(healthy)
                state["updated_at"] = observed_at.isoformat(timespec="seconds")
                quorum = _fresh_markets(data_dir, observed_at) | {market}
                if quorum <= healthy:
                    state["active"] = False
                _write(path, state)
    except Exception as exc:
        if isinstance(exc, OpenDIncidentStateError):
            raise
        raise OpenDIncidentStateError(str(exc)) from exc
```

Inside `RunLock(lock_path, wait=True)`, load/validate, mutate, call the provided Feishu callback only when active, undelivered, and `feishu_attempts < 2`, then atomically replace the JSON file. A malformed file, lock error, or atomic-write error raises `OpenDIncidentStateError`.

Classifier order is strict: structured `opend_unreachable`/`opend_reachable is False`; known rate-limit tokens (`频率太高`, `rate limit`, `too many requests`); known connectivity tokens (`connect timeout`, `connection refused`, `network down`, `protocol disconnected`, `网络中断`); otherwise `None`.

`record_opend_health` checks both category files. It records the current market, reads `trend_controller/CN|HK|US/status.json`, and treats a heartbeat as fresh when its absolute age is at most two minutes. Always include the reporting market in the quorum. Close an active incident only when all fresh markets are present in `healthy_markets`; do not send a recovery message.

- [ ] **Step 4: Run the incident tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest tests/test_opend_incident.py -q
```

Expected: all new incident tests pass.

- [ ] **Step 5: Integrate controller failures and health observations**

Add `_notify_controller_failure(config, market, execution_date, action, error, occurred_at)`. For a classified failure:

1. call Task 4 `_notify_non_feishu_once` with the existing per-market copy;
2. call `record_opend_failure` with a Feishu callback using `channels={"feishu", "feishu_app"}` and a category-specific `render_attention` payload;
3. on `OpenDIncidentStateError`, call normal `_notify_once` with a per-market Feishu A3 and stable category reason.

The callback returns the actual successful Feishu channel:

```python
def send_shared_feishu(title: str, message: str) -> str | None:
    attempts = send_notification_with_results(
        build_notifier(config),
        title,
        message,
        channels={"feishu", "feishu_app"},
    )
    return next((attempt.channel for attempt in attempts if attempt.success), None)
```

Render `connectivity` as “OpenD 连接故障” with action “检查 OpenD 登录与网络”；render `rate_limit` as “OpenD 请求限频” with action “暂停手工重跑，等待限频窗口恢复”. Both messages state that CN/HK/US行情与订单监控 may be affected and pass the short reason through `brief_zh_detail`.

For an unclassified error, call normal `_notify_once` with identity reason `getattr(error, "error_type", error.__class__.__name__)`; the full error string appears only as a short body detail.

Replace the cycle, operation, and review exception notification calls with `_notify_controller_failure`. After `_derive_cycle` succeeds, call `record_opend_health(config.data_dir, market, now)` in a best-effort block; state-maintenance failure must not turn a successful controller cycle into a trading blocker.

- [ ] **Step 6: Add controller integration tests and run them**

Add tests proving two different OpenD strings map to one shared event, connectivity and rate-limit create two events, state failure falls back to per-market A3, and unknown errors never enter shared state.

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest \
  tests/test_opend_incident.py tests/test_trend_market_controller.py -q
```

Expected: both test files pass.

- [ ] **Step 7: Commit OpenD consolidation**

```bash
git add src/open_trader/opend_incident.py src/open_trader/trend_market_controller.py \
  tests/test_opend_incident.py tests/test_trend_market_controller.py
git commit -m "feat: consolidate OpenD incidents across markets"
```

---

### Task 6: Grouped A5/A6/C1/C2 Feishu order alerts

**Files:**
- Modify: `src/open_trader/a_share_trend_watch.py:521-636`
- Modify: `src/open_trader/trend_market_controller.py:810-930, 2100-2170`
- Modify: `tests/test_a_share_trend_watch.py`
- Modify: `tests/test_market_trend_watch.py`
- Modify: `tests/test_trend_market_controller.py`

**Interfaces:**
- Consumes: Task 1 `group_order_alerts`/`render_order_alert`, Task 4 `_notify_feishu_once`/`_notify_non_feishu_once`, existing action JSON and watch-event JSONL.
- Produces: Feishu groups keyed by trading date + market + side + status; controller `_notify_order_groups(config, market, execution_date, events, occurred_at)`; old non-Feishu per-symbol/batch alerts; per-group attempt/exhaustion facts; no normal submitted/filled stream.

- [ ] **Step 1: Replace the C1 per-symbol test with grouping/retry tests**

Write two latest action files for US buy/incomplete HIG and HST and one US sell/incomplete SPY. Use this complete integration test with the existing watcher notifier fakes:

```python
def write_action(data_dir: Path, key: str, payload: dict[str, object]) -> None:
    path = data_dir / "trend_review/ledgers/US/actions/2026-07-22" / key / "2026-07-22T15-30-00-04-00.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**payload, "recorded_at": "2026-07-22T15:30:00-04:00"}), encoding="utf-8")


def test_deadline_groups_same_side_and_status(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_action(data_dir, "hig-buy", {"symbol": "HIG", "side": "buy", "status": "incomplete", "filled_qty": "5", "target_qty": "10"})
    write_action(data_dir, "hst-buy", {"symbol": "HST", "side": "buy", "status": "incomplete", "target_qty": "20"})
    write_action(data_dir, "spy-sell", {"symbol": "SPY", "side": "sell", "status": "incomplete", "target_qty": "3"})
    feishu = RecordingNotifier()
    macos = RecordingMacOSNotifier()

    _notify_trend_review_deadline(
        data_dir=data_dir,
        market="US",
        trading_date="2026-07-22",
        now=datetime.fromisoformat("2026-07-22T15:30:00-04:00"),
        events_path=data_dir / "trend_us/watch_events.jsonl",
        notifier=CompositeNotifier([feishu, macos]),
    )

    assert [title for title, _ in feishu.messages] == [
        "【需处理｜老虎｜美股买入未完成｜2026-07-22】",
        "【需处理｜老虎｜美股卖出未完成｜2026-07-22】",
    ]
    assert "- HIG" in feishu.messages[0][1]
    assert "- HST" in feishu.messages[0][1]
    assert "- SPY" in feishu.messages[1][1]
    assert [title for title, _ in macos.messages] == [
        "趋势模拟执行未完成 · 2026-07-22",
        "趋势模拟执行未完成 · 2026-07-22",
        "趋势模拟执行未完成 · 2026-07-22",
    ]
```

Add a Flaky Feishu test: first group send fails, the next watcher pass retries the whole group, a third pass sends nothing, and macOS is not repeated. Add a two-failure test that records `trend_review_deadline_notification_exhausted_feishu` and makes no third attempt. Seed a legacy `trend_review_deadline_notified` event and assert no historical Feishu replay.

- [ ] **Step 2: Add A5/A6 group tests**

Create action ledgers with mixed buy/sell `uncertain` events, run the controller notification branch, and assert two Feishu group titles but one unchanged non-Feishu batch notification. Add missed buys for HIG/HST and assert one buy/missed group. Assert a plain `submitted` execution before the deadline creates no Feishu order message.

- [ ] **Step 3: Run the focused tests to verify current per-symbol/generic behavior fails**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  tests/test_trend_market_controller.py -q
```

Expected: new assertions fail because C1 sends one Feishu per symbol and A5/A6 send generic market messages.

- [ ] **Step 4: Group C1 from existing latest action facts**

In `_notify_trend_review_deadline`, load the latest valid JSON from every action directory, honor legacy `trend_review_deadline_notified` as final, and exclude `filled`. For every eligible symbol, send its current per-symbol title/body once to `{"macos", "xiaoai"}` so non-Feishu granularity stays unchanged. Group the same facts with `group_order_alerts`, render one Feishu payload per group, and append channel-specific events only after success.

Extend `append_watch_event` with optional `channel` and `group_id` fields. Use these event types:

```python
"trend_review_deadline_notification_attempted_feishu"
"trend_review_deadline_notification_delivered_feishu"
"trend_review_deadline_notification_exhausted_feishu"
"trend_review_deadline_notification_delivered_non_feishu"
```

The group identifier is a SHA-256 of `trading_date|market|side|status|sorted symbols`. Count failed attempts by group identifier; allow two total. On success, append one delivered-Feishu fact per symbol. On the second failure, append one exhausted group fact and never write a delivered fact.

- [ ] **Step 5: Group C2 only at natural callback batches**

Keep batch/infrastructure callback failure directionless and render one `批次执行失败` Feishu alert. For protection-trigger callbacks, direction is `sell`; collect all failures from one watcher iteration and call the same group renderer once at the end of that iteration. Preserve each original `trend_review_callback_failed` fact. Record Feishu attempt/delivery/exhaustion with the same two-attempt rule: attempt immediately in the current iteration, retry the frozen group only on the next natural watcher iteration, then stop. Do not query a broker.

- [ ] **Step 6: Group A5/A6 from action ledgers**

Add `_latest_action_events(config, market, execution_date)` to read only the latest JSON in each action directory. For `uncertain`, `conflict`, and `missed_window`:

1. call `_notify_non_feishu_once` once with the existing batch title/body and identity;
2. select matching latest facts, call `group_order_alerts`, and call `_notify_feishu_once` once per group with an identity containing market/date/side/status;
3. if an execution is a directionless infrastructure failure, send one batch failure rather than inventing a side.

Leave `submitted`, `filled`, and ordinary monitoring branches without a new Feishu message.

Implement the controller group sender as:

```python
def _notify_order_groups(
    config: DailyPremarketConfig,
    market: str,
    execution_date: str,
    events: list[Mapping[str, object]],
    occurred_at: str,
) -> int:
    sent = 0
    for group in group_order_alerts(market, events):
        title, message = render_order_alert(
            group,
            broker_label=BROKER_LABELS[market],
            trading_date=execution_date,
        )
        key = (
            config,
            market,
            execution_date,
            f"order_{group.side}_{group.status}",
            f"{group.side}:{group.status}",
            occurred_at,
        )
        sent += int(_notify_feishu_once(title, message, key))
    return sent
```

- [ ] **Step 7: Run grouped-order tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest \
  tests/test_notification_policy.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  tests/test_trend_market_controller.py -q
```

Expected: all selected tests pass; same market/side/status is one Feishu message, and non-Feishu behavior is unchanged.

- [ ] **Step 8: Commit grouped orders**

```bash
git add src/open_trader/a_share_trend_watch.py src/open_trader/trend_market_controller.py \
  tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py \
  tests/test_trend_market_controller.py
git commit -m "feat: group actionable Feishu order alerts"
```

---

### Task 7: Verification, exact-SHA deployment, and live evidence

**Files:**
- Verify only; no source file is expected to change.

**Interfaces:**
- Consumes: all prior tasks, real config at `/Users/ray/projects/open_trader/config/daily_premarket.env`, existing launchd installer, D7 CLI.
- Produces: focused/full test output, controlled concurrency output, one real D7 delivery result, and three fresh controller PID/cwd/SHA/heartbeat/log records.

- [ ] **Step 1: Run the policy-focused suite**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest \
  tests/test_notification_policy.py \
  tests/test_opend_incident.py \
  tests/test_notifications.py \
  tests/test_trend_delivery.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py \
  tests/test_trend_market_controller.py \
  tests/test_drawdown_preflight.py \
  tests/test_daily_premarket.py \
  tests/test_decision_plan_watch.py \
  tests/test_t_signal_runner.py \
  tests/test_premarket_cli.py -q
```

Expected: exit 0 with zero failures.

- [ ] **Step 2: Run the complete suite**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest -q
```

Expected: exit 0 with zero failures. Record the exact passed count and elapsed time in the handoff.

- [ ] **Step 3: Run controlled direct notification workflows**

Run the concurrency and grouping tests with output enabled so their real JSON paths and message records are visible:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest -q -s \
  tests/test_opend_incident.py::test_three_concurrent_markets_send_one_incident \
  tests/test_a_share_trend_watch.py::test_deadline_groups_same_side_and_status
```

Expected: two tests pass; the first records one Feishu callback for three markets and the second records one buy group for multiple symbols. These use temporary data and no real order client.

- [ ] **Step 4: Inspect current long-running processes before replacement**

```bash
launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)'
ps -axo pid,lstart,command | rg 'open_trader trend-market run'
screen -ls
```

Expected: record the old CN/HK/US PIDs and working commands; do not touch the Dashboard screen.

- [ ] **Step 5: Send the one approved D7 real notification**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  test-notification \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Expected: exit 0 and channel output confirms `feishu_app` success. Do not send any other synthetic external message.

- [ ] **Step 6: Freeze the clean deployment SHA**

Run:

```bash
git status --short
git rev-parse HEAD
```

Expected: status is empty. Record the full SHA as `ACCEPTED_SHA`; do not change source or data afterward.

If status is not empty or any correction is needed, return to the owning task, commit its exact listed files, and rerun Steps 1–6 from the beginning.

- [ ] **Step 7: Reinstall all three controllers from the exact verified worktree**

From the implementation worktree, run:

```bash
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all
```

Expected: installer reports verified CN, HK, and US controller labels with new PIDs and no surviving legacy process. This is the required old-code restart; do not restart the Dashboard because it was not changed.

- [ ] **Step 8: Verify PID, cwd, SHA, advancing heartbeat, and fresh logs**

Run this from the exact worktree; the script derives the worktree and SHA directly from Git:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python - <<'PY'
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time

from open_trader.daily_premarket import load_env_config

config = load_env_config(Path("/Users/ray/projects/open_trader/config/daily_premarket.env"))
expected_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
expected_worktree = str(Path.cwd().resolve())

def read(market: str) -> dict[str, object]:
    path = config.data_dir / "trend_controller" / market / "status.json"
    return json.loads(path.read_text(encoding="utf-8"))

before = {market: read(market) for market in ("CN", "HK", "US")}
time.sleep(10)
for market, previous in before.items():
    current = read(market)
    pid = int(current["pid"])
    os.kill(pid, 0)
    assert current["working_directory"] == expected_worktree
    assert current["git_sha"] == expected_sha
    assert datetime.fromisoformat(str(current["heartbeat_at"])) > datetime.fromisoformat(str(previous["heartbeat_at"]))
    print(market, pid, current["git_sha"], current["heartbeat_at"], current["phase"])
PY

launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)'
ps -axo pid,lstart,command | rg 'open_trader trend-market run'
tail -n 100 logs/daily_premarket/launchd-trend-controller-*.out.log
tail -n 100 logs/daily_premarket/launchd-trend-controller-*.err.log
```

Expected: all PIDs are live and newer than the pre-deploy PIDs; every cwd and SHA matches the exact worktree; all three heartbeats advance; fresh logs have no traceback, routing error, B2–B6 title, or D1–D6 title.

- [ ] **Step 9: Record the completion evidence**

The final handoff must state the focused/full pytest counts, D7 channel result, deployed full SHA, CN/HK/US new PIDs, worktree, heartbeat timestamps, and fresh-log result. If any process, channel, or log check fails, continue diagnosis and do not describe the live change as complete.
