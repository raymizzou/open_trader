# Broker Trend Dashboard and Feishu v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a same-day execution-timeline report to the three trend-enabled broker sections and send exactly one concise, actionable plain-text Feishu message per broker trading day.

**Architecture:** Keep the frozen trend-report JSON as the only presentation input. Add pure notification renderers beside the existing report renderer, add one small daily text-delivery ledger that freezes the first semantic message for retry/deduplication, project the three report directories into the existing Dashboard payload, and render the approved B timeline in a read-only workspace. Do not recalculate strategy decisions in either presentation path.

**Tech Stack:** Python 3.12, dataclasses/JSON/pathlib, pytest, vanilla JavaScript, HTML/CSS, Node-based frontend tests, existing Dashboard acceptance harness.

## Global Constraints

- v1 covers only 富途美股、辉立港股、东方财富 A 股; 老虎证券 does not get a trend-report entry.
- Every trend-enabled broker section shows `当天趋势报告`, `报告日期`, and `数据截至`; the full view also shows `生成时间`.
- A stale report must render as `今日暂无趋势报告`; never fall back to another broker or another execution date.
- The report is read-only and must not change trading judgments, scheduling, market calendars, protection state, or watcher notifications.
- Feishu is plain text only: no URL, button, card, Markdown table, local path, audit appendix, internal English action/reason/status, or full hold list.
- Send one semantic message per broker trading day. The first success/failure semantic message owns that day's slot; transport retries resend only that frozen text. Revisions never send a second message.
- All sell, buy, and manual-review items are listed; holds are count-only. Unknown actions or reasons become manual-review items.
- Failure text contains one Chinese reason, one recovery action, and `报告未生成，请勿依据旧报告交易。`.
- Buy windows are exact: 富途 `美股常规交易时段`; 辉立 and 东方财富 `09:30–10:00`.
- After every task modification, run the targeted tests and `make acceptance`. Only `PASS` is a completion result.
- After the final `make acceptance` returns `PASS`, redeploy the exact accepted Git SHA and verify PID, cwd, SHA, fresh log timestamp, and HTTP 200 before review.

---

### Task 1: Render the fixed Feishu v1 text from report JSON

**Files:**
- Modify: `src/open_trader/a_share_trend.py:847-1045`
- Test: `tests/test_a_share_trend.py`

**Interfaces:**
- Consumes: the mapping returned by existing `_report_payload(report: TrendReport) -> dict[str, object]`.
- Produces: `render_trend_feishu_text(payload: Mapping[str, object], *, broker_label: str, market_label: str) -> tuple[str, str]` and `render_trend_failure_text(*, broker_label: str, market_label: str, report_date: str, reason: str, recovery_action: str) -> tuple[str, str]`.

- [ ] **Step 1: Add failing formatter tests**

Add tests with literal expected text so the v1 copy cannot drift:

```python
def test_trend_feishu_text_lists_actions_but_only_counts_holds() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": {"fresh": True},
        "metadata": {"market": "US", "broker": "futu"},
        "strategy_judgments": {
            "holding_decisions": [
                {"action": "SELL_ALL", "symbol": "AAPL", "name": "苹果", "reason": "left_trend_right_side", "active_line": "190"},
                {"action": "HOLD", "symbol": "MSFT", "name": "微软", "reason": "right_side"},
                {"action": "MANUAL_REVIEW", "symbol": "TSLA", "name": "特斯拉", "reason": "missing_snapshot"},
                {"action": "NEW_CODE", "symbol": "NVDA", "name": "英伟达", "reason": "new_reason"},
            ],
            "formal_actions": [
                {"action": "SELL_ALL", "symbol": "AAPL", "name": "苹果", "reason": "left_trend_right_side", "active_line": "190"},
                {"action": "BUY", "symbol": "CRWD", "name": "CrowdStrike", "estimated_shares": 2, "target_amount": "500", "estimated_initial_line": "198"},
            ],
        },
    }

    title, message = render_trend_feishu_text(
        payload, broker_label="富途", market_label="美股"
    )

    assert title == "【富途｜美股趋势报告｜2026-07-15】"
    assert message == "\n".join([
        "数据截至：2026-07-14",
        "账户状态：已更新",
        "今日动作：卖出 1｜买入 1｜持有 1｜复核 2",
        "",
        "卖出",
        "1. AAPL 苹果｜右侧趋势已结束｜保护线 190",
        "",
        "买入",
        "1. CRWD CrowdStrike｜美股常规交易时段｜约 2 股｜金额上限 500｜保护线 198",
        "",
        "人工复核",
        "1. TSLA 特斯拉｜未知动作或原因，需人工确认",
        "2. NVDA 英伟达｜未知动作或原因，需人工确认",
        "",
        "请人工确认，不自动下单。",
    ])
    assert "MSFT" not in message
    assert "http" not in message.lower()


def test_trend_feishu_text_uses_short_no_trade_template() -> None:
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "account": {"fresh": False},
        "metadata": {"market": "HK", "broker": "phillips"},
        "strategy_judgments": {
            "holding_decisions": [{"action": "HOLD", "symbol": "02800"}],
            "formal_actions": [],
        },
    }
    title, message = render_trend_feishu_text(
        payload, broker_label="辉立", market_label="港股"
    )
    assert title == "【辉立｜港股趋势报告｜2026-07-15】"
    assert message == (
        "数据截至：2026-07-14\n"
        "账户状态：已过期\n"
        "今日无买卖动作｜持有 1｜复核 0\n\n"
        "请人工确认，不自动下单。"
    )


def test_trend_failure_text_is_plain_and_actionable() -> None:
    title, message = render_trend_failure_text(
        broker_label="东方财富",
        market_label="A股",
        report_date="2026-07-15",
        reason="趋势数据在截止时间前仍未更新",
        recovery_action="确认 Trend Animals 数据状态后手动重跑东方财富报告",
    )
    assert title == "【东方财富｜A股趋势报告生成失败｜2026-07-15】"
    assert message == (
        "原因：趋势数据在截止时间前仍未更新\n"
        "现在做：确认 Trend Animals 数据状态后手动重跑东方财富报告\n\n"
        "报告未生成，请勿依据旧报告交易。"
    )
```

- [ ] **Step 2: Verify the tests fail for the missing public functions**

Run: `pytest tests/test_a_share_trend.py -k 'trend_feishu_text or trend_failure_text' -v`

Expected: collection/import failure because `render_trend_feishu_text` and `render_trend_failure_text` do not exist.

- [ ] **Step 3: Implement the two pure renderers**

Add the functions next to `render_markdown`. Reuse `_reason_label` for known reasons, but force any unknown action or untranslated English reason into review rather than exposing an internal code:

```python
TREND_BUY_WINDOWS = {
    "US": "美股常规交易时段",
    "HK": "09:30–10:00",
    "CN": "09:30–10:00",
}


def render_trend_feishu_text(
    payload: Mapping[str, object], *, broker_label: str, market_label: str
) -> tuple[str, str]:
    execution_date = str(payload.get("execution_date") or "-")
    as_of_date = str(payload.get("as_of_date") or "-")
    account = payload.get("account")
    account = account if isinstance(account, dict) else {}
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    market = str(metadata.get("market") or "CN").upper()
    judgments = payload.get("strategy_judgments")
    judgments = judgments if isinstance(judgments, dict) else {}
    holdings = [item for item in judgments.get("holding_decisions", []) if isinstance(item, dict)]
    formal = [item for item in judgments.get("formal_actions", []) if isinstance(item, dict)]
    sells = [
        item for item in formal
        if item.get("action") == "SELL_ALL" and item.get("reason") in REASON_LABELS
    ]
    buys = [item for item in formal if item.get("action") == "BUY"]
    holds = [
        item for item in holdings
        if item.get("action") == "HOLD" and item.get("reason") in REASON_LABELS
    ]
    reviews = [
        item for item in holdings
        if item.get("action") == "MANUAL_REVIEW"
        or item.get("action") not in ACTION_LABELS
        or (
            item.get("action") in {"SELL_ALL", "HOLD"}
            and item.get("reason") not in REASON_LABELS
        )
    ]
    title = f"【{broker_label}｜{market_label}趋势报告｜{execution_date}】"
    fresh = bool(account.get("fresh"))
    status = "已更新" if fresh else ("已过期，禁止买入" if buys else "已过期")
    if not sells and not buys:
        return title, "\n".join([
            f"数据截至：{as_of_date}",
            f"账户状态：{status}",
            f"今日无买卖动作｜持有 {len(holds)}｜复核 {len(reviews)}",
            "",
            "请人工确认，不自动下单。",
        ])
    lines = [
        f"数据截至：{as_of_date}",
        f"账户状态：{status}",
        f"今日动作：卖出 {len(sells)}｜买入 {len(buys)}｜持有 {len(holds)}｜复核 {len(reviews)}",
    ]
    _append_feishu_action_sections(lines, sells, buys, reviews, market=market)
    lines.extend(["", "请人工确认，不自动下单。"])
    return title, "\n".join(lines)


def render_trend_failure_text(
    *, broker_label: str, market_label: str, report_date: str,
    reason: str, recovery_action: str,
) -> tuple[str, str]:
    return (
        f"【{broker_label}｜{market_label}趋势报告生成失败｜{report_date}】",
        f"原因：{reason}\n现在做：{recovery_action}\n\n报告未生成，请勿依据旧报告交易。",
    )
```

Add the section helpers exactly as pure list formatting; this omits empty sections and never exposes an unknown internal code:

```python
def _feishu_identity(item: Mapping[str, object]) -> str:
    return " ".join(
        part for part in (str(item.get("symbol") or "-").strip(), str(item.get("name") or "").strip())
        if part
    )


def _feishu_reason(item: Mapping[str, object]) -> str:
    reason = str(item.get("reason") or "")
    if reason not in REASON_LABELS:
        return "未知动作或原因，需人工确认"
    return _reason_label(reason)


def _append_feishu_action_sections(
    lines: list[str],
    sells: Sequence[Mapping[str, object]],
    buys: Sequence[Mapping[str, object]],
    reviews: Sequence[Mapping[str, object]],
    *,
    market: str,
) -> None:
    if sells:
        lines.extend(["", "卖出"])
        for index, item in enumerate(sells, 1):
            line = f"{index}. {_feishu_identity(item)}｜{_feishu_reason(item)}"
            if item.get("active_line") not in {None, ""}:
                line += f"｜保护线 {_money(Decimal(str(item['active_line'])))}"
            lines.append(line)
    if buys:
        lines.extend(["", "买入"])
        for index, item in enumerate(buys, 1):
            lines.append(
                f"{index}. {_feishu_identity(item)}｜{TREND_BUY_WINDOWS[market]}｜"
                f"约 {item.get('estimated_shares', '-')} 股｜"
                f"金额上限 {_money(Decimal(str(item.get('target_amount') or '0')))}｜"
                f"保护线 {_money(Decimal(str(item.get('estimated_initial_line') or '0')))}"
            )
    if reviews:
        lines.extend(["", "人工复核"])
        lines.extend(
            f"{index}. {_feishu_identity(item)}｜{_feishu_reason(item)}"
            for index, item in enumerate(reviews, 1)
        )
```

- [ ] **Step 4: Run formatter tests and the existing report-rendering tests**

Run: `pytest tests/test_a_share_trend.py -k 'render or feishu or report_payload' -v`

Expected: all selected tests pass, including the three new literal-template tests.

- [ ] **Step 5: Run the repository acceptance gate and commit**

Run: `make acceptance`

Expected: final line/result reports `PASS`.

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "feat: render concise trend notifications"
```

### Task 2: Freeze and deduplicate one semantic Feishu message per broker day

**Files:**
- Create: `src/open_trader/trend_delivery.py`
- Create: `tests/test_trend_delivery.py`

**Interfaces:**
- Consumes: an existing `Notifier`, a broker-specific ledger path, and the `(title, message)` from Task 1.
- Produces: `deliver_daily_trend_text(...) -> str` and `retry_daily_trend_text(...) -> str | None`. Returned statuses are `sent`, `delivery_failed`, and `sent_prior_message`; an on-disk `pending` message remains retryable because only an explicit Feishu success response completes delivery.

- [ ] **Step 1: Add failing state-machine tests**

```python
class RecordingNotifier(FeishuWebhookNotifier):
    def __init__(self, fail: bool = False) -> None:
        super().__init__(webhook_url="https://example.invalid")
        self.fail = fail
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))
        if self.fail:
            raise RuntimeError("network down")


def test_first_message_owns_day_and_revision_cannot_replace_it(tmp_path: Path) -> None:
    notifier = RecordingNotifier()
    ledger = tmp_path / "delivery/2026-07-15.json"
    assert deliver_daily_trend_text(
        notifier, ledger_path=ledger, title="success", message="first"
    ) == "sent"
    assert deliver_daily_trend_text(
        notifier, ledger_path=ledger, title="revision", message="second"
    ) == "sent_prior_message"
    assert notifier.messages == [("success", "first")]


def test_failed_transport_retries_exact_frozen_text(tmp_path: Path) -> None:
    ledger = tmp_path / "delivery/2026-07-15.json"
    failing = RecordingNotifier(fail=True)
    assert deliver_daily_trend_text(
        failing, ledger_path=ledger, title="failure", message="frozen"
    ) == "delivery_failed"
    recovered = RecordingNotifier()
    assert deliver_daily_trend_text(
        recovered, ledger_path=ledger, title="new report", message="must not replace"
    ) == "sent"
    assert recovered.messages == [("failure", "frozen")]


def test_pending_delivery_retries_the_same_frozen_text(tmp_path: Path) -> None:
    ledger = tmp_path / "delivery/2026-07-15.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({
        "status": "pending", "title": "frozen", "message": "body",
        "content_sha256": hashlib.sha256(b"frozen\0body").hexdigest(),
    }), encoding="utf-8")
    notifier = RecordingNotifier()
    assert retry_daily_trend_text(notifier, ledger_path=ledger) == "sent"
    assert notifier.messages == [("frozen", "body")]
```

- [ ] **Step 2: Verify tests fail because the module is absent**

Run: `pytest tests/test_trend_delivery.py -v`

Expected: import failure for `open_trader.trend_delivery`.

- [ ] **Step 3: Implement the minimal atomic ledger**

The ledger stores the first title/body and never accepts replacement content. `prepared`, `pending`, and `delivery_failed` all retry the stored text until Feishu explicitly returns success; `sent` short-circuits:

```python
def deliver_daily_trend_text(
    notifier: Notifier, *, ledger_path: Path, title: str, message: str
) -> str:
    ledger = _read_ledger(ledger_path)
    if ledger is None:
        ledger = _write_ledger(ledger_path, "prepared", title, message)
    return _deliver_frozen(notifier, ledger_path, ledger)


def retry_daily_trend_text(
    notifier: Notifier, *, ledger_path: Path
) -> str | None:
    ledger = _read_ledger(ledger_path)
    if ledger is None:
        return None
    return _deliver_frozen(notifier, ledger_path, ledger)


def _deliver_frozen(
    notifier: Notifier, ledger_path: Path, ledger: Mapping[str, object]
) -> str:
    status = str(ledger["status"])
    if status == "sent":
        return "sent_prior_message"
    _write_ledger(ledger_path, "pending", str(ledger["title"]), str(ledger["message"]))
    attempts = send_notification_with_results(
        notifier, str(ledger["title"]), str(ledger["message"]),
        channels={"feishu", "feishu_app"},
    )
    delivered = any(item.channel.startswith("feishu") and item.success for item in attempts)
    result = "sent" if delivered else "delivery_failed"
    _write_ledger(ledger_path, result, str(ledger["title"]), str(ledger["message"]))
    return result
```

Use this complete ledger validation/write shape around those public functions:

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping

from .daily_premarket import send_notification_with_results
from .notifications import Notifier


_VALID_STATUSES = {"prepared", "pending", "sent", "delivery_failed"}


def _content_hash(title: str, message: str) -> str:
    return hashlib.sha256(f"{title}\0{message}".encode("utf-8")).hexdigest()


def _write_ledger(
    path: Path, status: str, title: str, message: str
) -> dict[str, str]:
    if status not in _VALID_STATUSES:
        raise ValueError("invalid daily trend delivery status")
    payload = {
        "status": status,
        "title": title,
        "message": message,
        "content_sha256": _content_hash(title, message),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return payload


def _read_ledger(path: Path) -> dict[str, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("daily trend delivery ledger is unreadable") from None
    if not isinstance(payload, dict) or payload.get("status") not in _VALID_STATUSES:
        raise ValueError("daily trend delivery ledger has invalid status")
    title, message = payload.get("title"), payload.get("message")
    if not isinstance(title, str) or not isinstance(message, str):
        raise ValueError("daily trend delivery ledger has invalid text")
    if payload.get("content_sha256") != _content_hash(title, message):
        raise ValueError("daily trend delivery ledger hash mismatch")
    return {
        "status": str(payload["status"]),
        "title": title,
        "message": message,
        "content_sha256": str(payload["content_sha256"]),
    }
```

- [ ] **Step 4: Run the ledger tests**

Run: `pytest tests/test_trend_delivery.py -v`

Expected: all tests pass and recorded messages prove exact frozen-text retry.

- [ ] **Step 5: Run acceptance and commit**

Run: `make acceptance`

Expected: `PASS`.

```bash
git add src/open_trader/trend_delivery.py tests/test_trend_delivery.py
git commit -m "feat: deduplicate daily trend messages"
```

### Task 3: Route CN, US, and HK report success/failure through the v1 message path

**Files:**
- Modify: `src/open_trader/a_share_trend.py:1527-1602,1850-1940,1950-2040`
- Modify: `src/open_trader/market_trend.py:320-620`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Consumes: Task 1 renderers and Task 2 delivery functions.
- Produces: broker-specific ledger files at `data/trend_a_share/daily_delivery/{run_date}.json`, `data/trend_us_futu/daily_delivery/{run_date}.json`, and `data/trend_hk_phillips/daily_delivery/{run_date}.json`. The internal slot key is the scheduler/artifact `run_date`; user-visible titles continue to use JSON `execution_date`, so a failure and later recovery from the same scheduled run cannot occupy different slots.

- [ ] **Step 1: Add integration tests for success, failure ownership, retry, and revision suppression**

For A-share, extend the existing `RecordingFeishu` runner tests to assert the exact title/body and that `revision=True` writes an artifact but does not add a Feishu call. In `test_hk_report_uses_real_api_fields_and_auto_manages_advised_buys`, replace the final run block with two real runs using its existing `Api` and `Quote` fakes:

```python
notifier = RecordingFeishu()
result = run_market_trend_report(
    config=cfg, market="HK", run_date="2026-07-15",
    notifier=notifier, api_factory=Api, quote_factory=Quote,
)
revised = run_market_trend_report(
    config=cfg, market="HK", run_date="2026-07-15", revision=True,
    notifier=notifier, api_factory=Api, quote_factory=Quote,
)
assert result.status == revised.status == "generated"
assert len(notifier.messages) == 1
title, message = notifier.messages[0]
assert title == "【辉立｜港股趋势报告｜2026-07-16】"
assert "09:30–10:00" in message
assert "http" not in message.lower()
```

Extend `test_market_report_stops_at_one_hour_deadline` with a `RecordingFeishu`, then assert its only message is the fixed 富途 failure template and the ledger status is `sent`. Task 2 already proves that a later success cannot replace that frozen failure and that failed transport retries it exactly. For A-share, extend its existing deadline test in the same way and assert the ledger lives under `trend_a_share/daily_delivery/2026-07-15.json`.

Use this recorder in `tests/test_market_trend.py` so channel filtering exercises the real Feishu path:

```python
class RecordingFeishu(FeishuWebhookNotifier):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(webhook_url="https://example.invalid")
        self.fail = fail
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))
        if self.fail:
            raise NotificationError("network down")
```

- [ ] **Step 2: Run the new tests and confirm current behavior fails**

Run: `pytest tests/test_a_share_trend.py tests/test_market_trend.py -k 'v1_text or revision_does_not_resend or failure_owns_day or frozen_failure' -v`

Expected: failures show long Markdown bodies, repeat revision sends, or missing A-share Feishu failure delivery.

- [ ] **Step 3: Wire A-share report delivery without changing its report receipt transaction**

When the report payload is prepared, render the v1 title/body from that payload and call the daily ledger instead of sending `receipt["markdown"]`. On receipt recovery, parse `receipt["report_json"]` and render the same text again; the daily ledger retains the first semantic text. Use one helper in both paths:

```python
def _deliver_a_share_daily_text(
    *, config: DailyPremarketConfig, notifier: Notifier,
    run_date: str, payload: Mapping[str, object],
) -> str:
    title, message = render_trend_feishu_text(
        payload, broker_label="东方财富", market_label="A股"
    )
    return deliver_daily_trend_text(
        notifier,
        ledger_path=config.data_dir / "trend_a_share/daily_delivery" / f"{run_date}.json",
        title=title,
        message=message,
    )
```

Map `sent` and `sent_prior_message` to a successful report-receipt state, and retain `delivery_failed` for retry safety:

```python
daily_status = _deliver_a_share_daily_text(
    config=config, notifier=notifier, run_date=run_date, payload=payload,
)
receipt_status = "sent" if daily_status in {"sent", "sent_prior_message"} else daily_status
receipt = _transition_delivery_receipt(
    receipt_path, receipt, status=receipt_status, delivery_status=daily_status,
)
```

In `_recover_receipt_report`, include the existing report-receipt `pending` state with `prepared` and `delivery_failed`, call `_deliver_a_share_daily_text`, and transition from the daily result. Remove the branch that converts a pending report receipt to `delivery_unknown`; the frozen daily ledger now gives pending delivery a safe same-text retry path. Keep reading legacy `delivery_unknown` report receipts as non-resendable for backward compatibility, but never create a new one in v1.

At the 18:00 deadline, use:

```python
title, message = render_trend_failure_text(
    broker_label="东方财富",
    market_label="A股",
    report_date=run_date,
    reason="趋势数据在截止时间前仍未更新" if "not ready" in last_error.lower() else "趋势报告生成失败，需检查运行日志",
    recovery_action="确认 Trend Animals 数据状态后手动重跑东方财富报告",
)
deliver_daily_trend_text(
    notifier,
    ledger_path=config.data_dir / "trend_a_share/daily_delivery" / f"{run_date}.json",
    title=title,
    message=message,
)
```

Broaden only the report-attempt retry boundary to match the CLI's declared report errors, so generation failures reach this failure template instead of escaping silently:

```python
except (TrendAnimalsError, FutuQuoteError, ValueError, RuntimeError) as exc:
    last_error = _redact_api_key(exc, config.trend_animals_api_key)
```

Do not catch `KeyboardInterrupt` or `SystemExit`.

Keep the existing macOS status notification separate; it does not consume the Feishu daily slot.

- [ ] **Step 4: Wire US/HK delivery and existing-report retry**

In `_attempt_market_report`, build `_report_payload(report)` before delivery, render with labels from a constant mapping, and use the market root ledger:

```python
broker_label, market_label, _ = MARKET_NOTIFICATION_LABELS[market]
payload = _report_payload(report)
title, message = render_trend_feishu_text(
    payload, broker_label=broker_label, market_label=market_label
)
delivery_status = deliver_daily_trend_text(
    notifier,
    ledger_path=paths.root / "daily_delivery" / f"{run_date}.json",
    title=title,
    message=message,
)
report = replace(
    report, metadata={**report.metadata, "delivery_status": delivery_status}
)
```

Before returning `existing`, call `retry_daily_trend_text(notifier, ledger_path=paths.root / "daily_delivery" / f"{run_date}.json")` so a prior transport failure can complete without regenerating/refetching. A revision uses the same `run_date` ledger and therefore cannot send again.

At deadline, render one broker-specific failure message with these fixed recovery actions:

```python
MARKET_NOTIFICATION_LABELS = {
    "US": ("富途", "美股", "确认 Trend Animals 与富途账户状态后手动重跑富途报告"),
    "HK": ("辉立", "港股", "确认 Trend Animals 与辉立日结单状态后手动重跑辉立报告"),
}
```

Use the same ledger path as success: `paths.root / "daily_delivery" / f"{run_date}.json"`. Do not change `_run_market_trend_retry` deadlines, sleeps, market calendars, or watcher code.

- [ ] **Step 5: Run all trend report and notification tests**

Run: `pytest tests/test_a_share_trend.py tests/test_market_trend.py tests/test_trend_delivery.py tests/test_notifications.py -v`

Expected: all pass; the integration assertions show one message per broker day, exact same-text retry, and no revision send.

- [ ] **Step 6: Exercise the non-network rendering workflow directly**

Run:

```bash
python - <<'PY'
import json
from pathlib import Path
from open_trader.a_share_trend import render_trend_feishu_text

sources = [
    ("reports/trend_us_futu", "富途", "美股"),
    ("reports/trend_hk_phillips", "辉立", "港股"),
    ("reports/trend_a_share", "东方财富", "A股"),
]
for directory, broker, market in sources:
    payloads = []
    for path in Path(directory).glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("generated_at"):
            payloads.append((str(payload["generated_at"]), path.name, payload))
    payload = max(payloads, key=lambda item: (item[0], item[1]))[2]
    title, body = render_trend_feishu_text(
        payload, broker_label=broker, market_label=market
    )
    text = f"{title}\n\n{body}"
    forbidden = ("http://", "https://", ".md", ".json", "|---", "SELL_ALL", "MANUAL_REVIEW")
    assert not any(token in text for token in forbidden), text
    print(text, "\n")
PY
```

Expected: three separate fixed-template plain-text messages, each with its correct broker, market, report date, and buy window.

- [ ] **Step 7: Run acceptance and commit**

Run: `make acceptance`

Expected: `PASS`.

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "feat: send one trend report per broker day"
```

### Task 4: Project only same-day reports for all three brokers into Dashboard state

**Files:**
- Modify: `src/open_trader/dashboard.py:100-190,1200-1320`
- Test: `tests/test_dashboard.py:200-285`

**Interfaces:**
- Consumes: frozen JSON files in the three existing report directories.
- Produces: `DashboardState.trend_reports: dict[str, dict[str, Any]]`, keyed by `futu`, `phillips`, and `eastmoney`. Each value contains report metadata, counts, full timeline actions, and audit data; it contains no recomputed trading decisions.

- [ ] **Step 1: Replace the two-market summary test with a three-broker same-day projection test**

Build fixtures containing `execution_date`, `as_of_date`, `generated_at`, `account`, full `strategy_judgments`, industry concentration, excluded items, data sources, costs, and protection events. Assert:

```python
reports = load_dashboard_state(config).to_dict()["trend_reports"]
assert set(reports) == {"futu", "phillips", "eastmoney"}
assert reports["futu"]["report_date"] == "2026-07-15"
assert reports["futu"]["data_date"] == "2026-07-14"
assert reports["futu"]["generated_at"] == "2026-07-15T11:30:36+08:00"
assert reports["futu"]["sell_actions"][0]["symbol"] == "AAPL"
assert reports["phillips"]["buy_window"] == "09:30–10:00"
assert reports["eastmoney"]["market_label"] == "A股"
assert reports["eastmoney"]["audit"]["data_sources"] == ["Trend Animals"]
```

Add a stale fixture whose `execution_date` is one day before injected `today`; assert `available is False`, `status_text == "今日暂无趋势报告"`, and that none of the stale actions are exposed. Add an unknown action/reason fixture and assert it appears in `review_actions`.

- [ ] **Step 2: Run the focused Dashboard test and verify failure**

Run: `pytest tests/test_dashboard.py -k 'trend_report' -v`

Expected: failure because only `trend_market_summaries` for US/HK exists and CN/full action data are absent.

- [ ] **Step 3: Implement a deterministic broker report loader**

Add an optional date input for testability and call it with Shanghai-local `date.today()` in production:

```python
TREND_REPORT_SOURCES = {
    "futu": ("US", "美股", "富途", "trend_us_futu", "美股常规交易时段"),
    "phillips": ("HK", "港股", "辉立", "trend_hk_phillips", "09:30–10:00"),
    "eastmoney": ("CN", "A股", "东方财富", "trend_a_share", "09:30–10:00"),
}


def _load_trend_reports(
    data_dir: Path, reports_dir: Path, *, today: date | None = None
) -> dict[str, dict[str, Any]]:
    report_date = (today or date.today()).isoformat()
    return {
        broker: _load_broker_trend_report(
            data_dir=data_dir,
            reports_dir=reports_dir / directory,
            broker=broker,
            market=market,
            market_label=market_label,
            broker_label=broker_label,
            buy_window=buy_window,
            report_date=report_date,
        )
        for broker, (market, market_label, broker_label, directory, buy_window)
        in TREND_REPORT_SOURCES.items()
    }
```

Parse and select the same-day JSON without using mtime:

```python
def _same_day_report_payload(
    reports_dir: Path, report_date: str
) -> tuple[Path, dict[str, Any]] | None:
    matches: list[tuple[str, str, Path, dict[str, Any]]] = []
    for path in reports_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("execution_date") != report_date:
            continue
        generated_at = str(payload.get("generated_at") or "")
        matches.append((generated_at, path.name, path, payload))
    if not matches:
        return None
    _, _, path, payload = max(matches, key=lambda item: (item[0], item[1]))
    return path, payload


def _load_broker_trend_report(
    *, data_dir: Path, reports_dir: Path, broker: str, market: str,
    market_label: str, broker_label: str, buy_window: str, report_date: str,
) -> dict[str, Any]:
    selected = _same_day_report_payload(reports_dir, report_date)
    if selected is None:
        return {
            "available": False, "broker": broker, "broker_label": broker_label,
            "market": market, "market_label": market_label,
            "status_text": "今日暂无趋势报告",
        }
    path, payload = selected
    judgments = payload.get("strategy_judgments")
    judgments = judgments if isinstance(judgments, dict) else {}
    formal = [item for item in judgments.get("formal_actions", []) if isinstance(item, dict)]
    holdings = [item for item in judgments.get("holding_decisions", []) if isinstance(item, dict)]
    sell_actions = [
        item for item in formal
        if item.get("action") == "SELL_ALL" and item.get("reason") in REASON_LABELS
    ]
    buy_actions = [item for item in formal if item.get("action") == "BUY"]
    hold_actions = [
        item for item in holdings
        if item.get("action") == "HOLD" and item.get("reason") in REASON_LABELS
    ]
    review_actions = [
        item for item in holdings
        if item.get("action") == "MANUAL_REVIEW"
        or item.get("action") not in ACTION_LABELS
        or (
            item.get("action") in {"SELL_ALL", "HOLD"}
            and item.get("reason") not in REASON_LABELS
        )
    ]
    account = payload.get("account")
    account = account if isinstance(account, dict) else {}
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    directory = reports_dir.name
    return {
        "available": True,
        "broker": broker,
        "broker_label": broker_label,
        "market": market,
        "market_label": market_label,
        "report_date": str(payload.get("execution_date") or ""),
        "data_date": str(payload.get("as_of_date") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "account_source_date": str(account.get("source_date") or ""),
        "account_fresh": bool(account.get("fresh")),
        "account_status": "已更新" if account.get("fresh") else "已过期，禁止买入",
        "buy_window": buy_window,
        "run_status": _latest_trend_run_status(
            data_dir / directory / "run.log",
            str(payload.get("delivery_status") or metadata.get("delivery_status") or "generated"),
        ),
        "sell_actions": sell_actions,
        "buy_actions": buy_actions,
        "hold_actions": hold_actions,
        "review_actions": review_actions,
        "counts": {
            "sell": len(sell_actions), "buy": len(buy_actions),
            "hold": len(hold_actions), "review": len(review_actions),
        },
        "recent_protection_alert": _recent_trend_protection_alert(
            data_dir / directory / "watch_events.jsonl"
        ),
        "audit": {
            "candidates": judgments.get("top10_candidates", []),
            "excluded": payload.get("excluded", {}),
            "industry_concentration": payload.get("industry_concentration", []),
            "data_sources": payload.get("data_sources", []),
            "estimated_api_cost": payload.get("estimated_api_cost"),
            "actual_api_cost": payload.get("actual_api_cost"),
            "artifact": path.name,
        },
    }
```

Import `ACTION_LABELS` and `REASON_LABELS` from `a_share_trend`; this is a one-way Dashboard dependency and avoids maintaining a second definition of known strategy codes.

Replace the serialized state field with `trend_reports`; remove the frontend's dependency on `trend_market_summaries` in Task 5, then delete the old summary loader rather than maintaining two parallel projections.

- [ ] **Step 4: Run backend Dashboard tests**

Run: `pytest tests/test_dashboard.py -v`

Expected: all pass, including fresh/stale/revision/unknown-action coverage for all three brokers.

- [ ] **Step 5: Run acceptance and commit**

Run: `make acceptance`

Expected: `PASS`.

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: expose broker trend reports to dashboard"
```

### Task 5: Build the approved B execution-timeline Dashboard workspace

**Files:**
- Modify: `src/open_trader/dashboard_static/index.html:85-110`
- Modify: `src/open_trader/dashboard_static/dashboard.js:150-330,1835-1890,2090-2205`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py:1520-1705`

**Interfaces:**
- Consumes: `state.dashboard.trend_reports[broker]` from Task 4.
- Produces: `openTrendReport(broker: string)`, `closeTrendReport()`, and `renderTrendReportWorkspace(report: object) -> string` plus one `当天趋势报告` entry in each of the three covered broker headers.

- [ ] **Step 1: Add failing semantic rendering and interaction tests**

Extend the Node harness mounts with `trend-report-workspace` and `close-trend-report`. Provide fresh report payloads for the three brokers and assert:

```javascript
if ((html.match(/当天趋势报告/g) || []).length !== 3) throw new Error(html);
for (const broker of ["futu", "phillips", "eastmoney"]) {
  if (!html.includes(`data-trend-report="${broker}"`)) throw new Error(html);
}
if (!html.includes("报告日期 2026-07-15") || !html.includes("数据截至 2026-07-14")) {
  throw new Error(html);
}
```

Click the Futu entry and assert the portfolio workspace is hidden, the report workspace is visible, and its text order is `开盘前` before `美股常规交易时段` before `盘中持续` before `人工复核`. Assert sell/buy/review symbols appear, hold symbols appear only in the Dashboard timeline (not Feishu), audit details are a closed `<details>`, and the return button restores the account workspace.

Add stale-state assertions: the entry position remains, the button is disabled, and `今日暂无趋势报告` is visible without stale action text.

- [ ] **Step 2: Verify frontend tests fail**

Run: `pytest tests/test_dashboard_web.py -k 'trend_report or account_sections' -v`

Expected: missing workspace/entry/interaction failures.

- [ ] **Step 3: Add the workspace mount and state transitions**

Add after `.workspace-grid`:

```html
<section id="trend-report-workspace" class="trend-report-workspace hidden" hidden aria-live="polite"></section>
```

Cache the element in `initializeElements`, add `selectedTrendBroker: ""` to the existing `state`, and use these transitions:

```javascript
function openTrendReport(broker) {
  const report = state.dashboard?.trend_reports?.[broker];
  if (!report?.available) return;
  state.selectedTrendBroker = broker;
  elements["workspace-grid"].classList.add("hidden");
  elements["standard-backtest-workspace"].hidden = true;
  elements["standard-backtest-workspace"].classList.add("hidden");
  elements["trend-report-workspace"].innerHTML = renderTrendReportWorkspace(report);
  elements["trend-report-workspace"].hidden = false;
  elements["trend-report-workspace"].classList.remove("hidden");
}


function closeTrendReport() {
  const broker = state.selectedTrendBroker;
  elements["trend-report-workspace"].hidden = true;
  elements["trend-report-workspace"].classList.add("hidden");
  elements["trend-report-workspace"].innerHTML = "";
  elements["workspace-grid"].classList.remove("hidden");
  state.selectedTrendBroker = "";
  document.getElementById(`account-${broker}`)?.scrollIntoView({block: "start"});
}
```

In the delegated `account-holdings` click listener, handle `event.target.closest("[data-trend-report]")` before holding-row actions and pass its `dataset.trendReport` to `openTrendReport`. In the trend workspace's delegated listener, handle `[data-close-trend-report]` with `closeTrendReport`. Do not add routing, a new API, or URL query state.

- [ ] **Step 4: Render broker header entries and B timeline**

In `renderAccountSection`, call this function inside the account header. It returns no entry for Tiger and retains the same slot for missing data:

```javascript
function renderTrendReportEntry(broker) {
  if (!["futu", "phillips", "eastmoney"].includes(broker)) return "";
  const report = state.dashboard?.trend_reports?.[broker] || {};
  if (!report.available) {
    return `<div class="trend-report-entry trend-report-entry-empty">
      <button type="button" disabled>当天趋势报告</button>
      <span>今日暂无趋势报告</span>
    </div>`;
  }
  return `<div class="trend-report-entry">
    <button type="button" data-trend-report="${escapeHtml(broker)}">当天趋势报告</button>
    <span>报告日期 ${escapeHtml(formatPlain(report.report_date))}</span>
    <span>数据截至 ${escapeHtml(formatPlain(report.data_date))}</span>
  </div>`;
}
```

Use the existing Chinese report reason vocabulary in one frontend map and render the B layout with escaped report data:

```javascript
const TREND_REASON_LABELS = {
  protection_line_already_triggered: "活动保护线已触发",
  danger_signal: "危险信号触发",
  left_trend_right_side: "右侧趋势已结束",
  holding_signal_unknown: "趋势信号不完整",
  holding_kline_unavailable: "持仓日线数据不可用",
  trend_intact: "趋势保持完好",
  right_side_not_true: "尚未进入右侧趋势",
  strength_not_above_90: "趋势强度未超过 90",
  right_side_days_not_below_10: "进入右侧趋势已满 10 天",
  not_tradable: "当前不可交易",
  amount_below_1: "日成交额不足 1 亿元",
  danger_unknown: "危险信号未知",
  name_missing: "标的名称缺失",
  asset_missing: "资产类型缺失",
  unsupported_asset: "不属于 A 股股票或境内 ETF",
  already_held: "当前账户已经持有",
  excluded_security: "北交所、ST 或退市标的",
  unsupported_exchange: "不属于沪深市场",
  atr_unavailable: "缺少 ATR 数据",
  data_date_mismatch: "数据日期不一致",
};

function renderTrendAction(item, kind) {
  const identity = [item.symbol, item.name].filter(Boolean).map(formatPlain).join(" ");
  const reason = TREND_REASON_LABELS[item.reason] || "未知动作或原因，需人工确认";
  const fields = [identity];
  if (kind === "buy") {
    fields.push(`约 ${formatPlain(item.estimated_shares)} 股`);
    fields.push(`金额上限 ${formatPlain(item.target_amount)}`);
    fields.push(`预计保护线 ${formatPlain(item.estimated_initial_line)}`);
  } else {
    fields.push(reason);
    if (item.active_line !== null && item.active_line !== undefined && item.active_line !== "") {
      fields.push(`活动保护线 ${formatPlain(item.active_line)}`);
    }
  }
  return `<li>${fields.map(escapeHtml).join("<span>｜</span>")}</li>`;
}

function renderTrendStage(title, items, kind) {
  return `<section class="trend-stage">
    <h2>${escapeHtml(title)}</h2>
    ${items.length ? `<ol>${items.map((item) => renderTrendAction(item, kind)).join("")}</ol>` : "<p>无</p>"}
  </section>`;
}

function renderTrendAudit(audit) {
  const candidates = Array.isArray(audit.candidates) ? audit.candidates : [];
  const excluded = audit.excluded && typeof audit.excluded === "object" ? audit.excluded : {};
  const industries = Array.isArray(audit.industry_concentration) ? audit.industry_concentration : [];
  return `<details class="trend-audit"><summary>审计详情</summary>
    <section><h3>候选榜</h3><ol>${candidates.length
      ? candidates.map((item) => `<li>${escapeHtml([item.symbol, item.name, `强度 ${item.strength ?? "-"}`].filter(Boolean).map(formatPlain).join("｜"))}</li>`).join("")
      : "<li>无</li>"}</ol></section>
    <section><h3>排除项</h3><ul>${Object.entries(excluded).length
      ? Object.entries(excluded).map(([symbol, reasons]) => `<li>${escapeHtml(formatPlain(symbol))}｜${escapeHtml((Array.isArray(reasons) ? reasons : []).map((reason) => TREND_REASON_LABELS[reason] || "未知原因").join("、"))}</li>`).join("")
      : "<li>无</li>"}</ul></section>
    <section><h3>行业集中度</h3><ul>${industries.length
      ? industries.map((item) => `<li>${escapeHtml((Array.isArray(item) ? item : []).map(formatPlain).join("｜"))}</li>`).join("")
      : "<li>无</li>"}</ul></section>
    <p>数据来源：${escapeHtml((audit.data_sources || []).map(formatPlain).join("、") || "无")}</p>
    <p>API 成本：${escapeHtml(formatPlain(audit.actual_api_cost ?? audit.estimated_api_cost ?? "未知"))}</p>
  </details>`;
}

function renderTrendReportWorkspace(report) {
  const counts = report.counts || {};
  const audit = report.audit || {};
  return `<header class="trend-report-header">
      <div><p>${escapeHtml(`${report.broker_label}｜${report.market_label}`)}</p><h1>当天趋势报告</h1></div>
      <button type="button" data-close-trend-report>返回持仓看板</button>
      <dl>
        <div><dt>报告日期</dt><dd>${escapeHtml(formatPlain(report.report_date))}</dd></div>
        <div><dt>数据截至</dt><dd>${escapeHtml(formatPlain(report.data_date))}</dd></div>
        <div><dt>生成时间</dt><dd>${escapeHtml(formatPlain(report.generated_at))}</dd></div>
        <div><dt>账户状态</dt><dd>${escapeHtml(formatPlain(report.account_status))}</dd></div>
      </dl>
      <div class="trend-report-metrics"><span>卖出 ${counts.sell || 0}</span><span>买入 ${counts.buy || 0}</span><span>持有 ${counts.hold || 0}</span><span>人工复核 ${counts.review || 0}</span></div>
    </header>
    <div class="trend-report-body">
      <main class="trend-timeline">
        ${renderTrendStage("开盘前", report.sell_actions || [], "sell")}
        ${renderTrendStage(report.buy_window, report.buy_actions || [], "buy")}
        ${renderTrendStage("盘中持续", report.hold_actions || [], "hold")}
        ${renderTrendStage("人工复核", report.review_actions || [], "review")}
        ${renderTrendAudit(audit)}
      </main>
      <aside class="trend-checklist"><h2>今日执行检查</h2><ol>
        <li>确认全部卖出动作</li><li>按顺序考虑允许买入项</li><li>盘中观察活动保护线</li><li>完成人工复核</li>
      </ol></aside>
    </div>`;
}
```

The rendered content order is:

```text
header: 券商｜市场, 报告日期, 数据截至, 生成时间, 账户状态
metrics: 卖出, 买入, 持有, 人工复核
timeline: 开盘前 -> configured buy_window -> 盘中持续 -> 人工复核
checklist: 确认卖出 -> 按顺序考虑买入 -> 观察保护线 -> 完成人工复核
details: 候选榜, 排除项, 行业集中度, 数据来源, API 成本
```

Escape every report-sourced string with existing `escapeHtml`. For an empty timeline stage, render `无` rather than omitting the stage, so the execution sequence remains stable.

- [ ] **Step 5: Add responsive CSS**

Add the following layout rules, using existing color/spacing variables for the remaining visual properties:

```css
.trend-report-body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 280px;
  gap: 20px;
}
.trend-timeline,
.trend-checklist,
.trend-stage,
.trend-report-header {
  min-width: 0;
}
.trend-checklist {
  position: sticky;
  top: 16px;
  align-self: start;
}
.trend-stage li,
.trend-audit p {
  overflow-wrap: anywhere;
}
@media (max-width: 760px) {
  .trend-report-body { grid-template-columns: minmax(0, 1fr); }
  .trend-checklist { position: static; order: 2; }
  .trend-report-entry button,
  .trend-report-header button { min-height: 44px; }
}
```

- [ ] **Step 6: Run frontend tests**

Run: `pytest tests/test_dashboard_web.py -v`

Expected: all pass; tests confirm three entries, exact date labels, timeline order, disabled stale state, collapsed audit details, return behavior, and mobile no-overflow rules.

- [ ] **Step 7: Run acceptance and commit**

Run: `make acceptance`

Expected: `PASS`, including real data, two refresh cycles, desktop/mobile browser flows, process version, and logs.

```bash
git add src/open_trader/dashboard_static/index.html src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: add broker trend report timeline"
```

### Task 6: Final live verification and exact-SHA review deployment

**Files:**
- No source changes.

**Interfaces:**
- Consumes: the completed commits from Tasks 1-5.
- Produces: an accepted and redeployed Dashboard process running the exact accepted SHA.

- [ ] **Step 1: Run the final acceptance gate**

Run: `make acceptance`

Expected: `PASS`. If it returns `FAIL`, diagnose/fix and rerun. If it returns `BLOCKED`, report the environmental blocker and do not substitute curl, fixtures, mocks, screenshots, or unit tests.

- [ ] **Step 2: Record the accepted SHA**

Run: `git rev-parse HEAD`

Expected: one 40-character SHA; save it as `ACCEPTED_SHA` for the deployment verification.

- [ ] **Step 3: Redeploy the exact accepted SHA using the repository's existing Dashboard restart target/workflow**

Before restarting, inspect `Makefile` and the running `screen`/`launchctl` state and use the existing non-destructive restart command. Do not create a second competing Dashboard process.

- [ ] **Step 4: Verify the live process**

Check all of the following and retain the concrete output for handoff:

```text
new process PID differs from the pre-restart PID
process cwd == /Users/ray/projects/open_trader
git SHA reported by process/log == ACCEPTED_SHA
log timestamp is after restart and contains no startup error
review URL returns HTTP 200
```

- [ ] **Step 5: Hand off for review**

Provide the accepted SHA, `make acceptance` result, new PID, cwd, fresh log path/timestamp, and direct review URL. Describe the feature as complete only when every check above succeeds.
