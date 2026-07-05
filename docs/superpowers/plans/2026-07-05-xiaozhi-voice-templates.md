# Xiaozhi Voice Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add short Xiaoai/Xiaozhi voice templates for existing Open Trader notifications.

**Architecture:** Keep Feishu and UI notification content unchanged. Add a pure formatter in `open_trader.notifications` that converts existing title/message pairs into short voice text, and let `XiaozhiVoiceNotifier` send the formatted voice text. Daily action notifications return no voice text and are skipped intentionally.

**Tech Stack:** Python, pytest, existing notifier interfaces.

---

## File Structure

- Modify `src/open_trader/notifications.py`
  - Add `render_xiaozhi_voice_notification(title, message) -> str | None`.
  - Add small parsing helpers for market, key-value message lines, duration, reason priority, and T-signal fields.
  - Update `XiaozhiVoiceNotifier.notify()` to send the formatted voice text, or return without posting when the formatter returns `None`.
- Modify `tests/test_notifications.py`
  - Add tests for each voice template and the Xiaozhi notifier integration.

### Task 1: Formatter Tests

**Files:**
- Modify: `tests/test_notifications.py`

- [ ] **Step 1: Write failing formatter tests**

Add imports:

```python
from open_trader.notifications import render_xiaozhi_voice_notification
```

Add tests:

```python
def test_render_xiaozhi_voice_daily_start_template() -> None:
    assert render_xiaozhi_voice_notification(
        "Open Trader 美股开始通知",
        "\n".join([
            "Open Trader｜开始通知",
            "日期：2026-07-05｜市场：美股",
            "状态：开始运行｜并发：8",
        ]),
    ) == "Open Trader 提醒：美股盘前流程已开始。正在生成今日交易复核清单，完成后会继续通知。"


def test_render_xiaozhi_voice_daily_blocker_uses_priority_reason() -> None:
    assert render_xiaozhi_voice_notification(
        "Open Trader 港股阻塞通知",
        "\n".join([
            "Open Trader｜阻塞通知",
            "日期：2026-07-05｜状态：部分完成",
            "可用性：需要人工复核",
            "原因：交易动作需要人工复核, Futu 行情异常",
        ]),
    ) == "Open Trader 重要提醒：港股盘前流程遇到阻塞，原因是 Futu 行情异常。请先查看飞书或 UI，处理后再决定是否交易。"


def test_render_xiaozhi_voice_daily_action_is_skipped() -> None:
    assert render_xiaozhi_voice_notification(
        "Open Trader 美股行动通知",
        "Open Trader｜行动通知\n今日结论：有 1 条可采取行动。",
    ) is None


def test_render_xiaozhi_voice_daily_completion_includes_duration() -> None:
    assert render_xiaozhi_voice_notification(
        "Open Trader 美股完成通知",
        "\n".join([
            "Open Trader｜完成通知",
            "日期：2026-07-05｜市场：美股",
            "开始时间：2026-07-05T21:00:00+08:00",
            "完成时间：2026-07-05T21:04:18+08:00",
            "状态：部分完成｜可用性：需要人工复核",
            "交易动作：4 ready，1 review，4 watch",
        ]),
    ) == "Open Trader 完成提醒：美股盘前流程已完成，本次用时 4 分 18 秒，状态是部分完成。今日有4项可复核，1项需人工判断。请先人工复核标记项。"


def test_render_xiaozhi_voice_daily_completion_omits_missing_duration() -> None:
    assert render_xiaozhi_voice_notification(
        "Open Trader 港股完成通知",
        "\n".join([
            "Open Trader｜完成通知",
            "日期：2026-07-05｜市场：港股",
            "状态：成功｜可用性：可复核",
            "交易动作：0 ready，0 review，2 watch",
        ]),
    ) == "Open Trader 完成提醒：港股盘前流程已完成，状态是正常。今日没有需要立即处理的交易动作。可以查看飞书复核清单。"


def test_render_xiaozhi_voice_t_signal_with_ratio() -> None:
    assert render_xiaozhi_voice_notification(
        "Open Trader｜做T提醒｜US ARM｜买入做T",
        "\n".join([
            "动作：买入做T",
            "比例：15%",
            "状态：盘中有效，等待执行确认",
        ]),
    ) == "Open Trader 做 T 提醒：US ARM 触发买入做 T 信号，建议比例15%。当前状态：盘中有效。请确认后再操作。"


def test_render_xiaozhi_voice_unknown_fallback() -> None:
    assert render_xiaozhi_voice_notification(
        "Open Trader 其他通知",
        "长正文",
    ) == "Open Trader 有新通知，请查看飞书或 UI。"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_notifications.py -k xiaozhi_voice -v
```

Expected: FAIL because `render_xiaozhi_voice_notification` does not exist.

### Task 2: Formatter Implementation

**Files:**
- Modify: `src/open_trader/notifications.py`

- [ ] **Step 1: Implement minimal formatter**

Add:

```python
def render_xiaozhi_voice_notification(title: str, message: str) -> str | None:
    ...
```

The function must:

- Return daily start text for titles ending in `开始通知`.
- Return daily blocker text for titles ending in `阻塞通知`, using reason priority.
- Return `None` for titles ending in `行动通知`.
- Return daily completion text for titles ending in `完成通知`, including duration only when start and finish fields can be parsed.
- Return T-signal text for titles containing `｜做T提醒｜`.
- Return `Open Trader 有新通知，请查看飞书或 UI。` for unknown notifications.

- [ ] **Step 2: Run formatter tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_notifications.py -k xiaozhi_voice -v
```

Expected: PASS.

### Task 3: Xiaozhi Notifier Integration

**Files:**
- Modify: `tests/test_notifications.py`
- Modify: `src/open_trader/notifications.py`

- [ ] **Step 1: Add failing integration tests**

Add tests:

```python
def test_xiaozhi_voice_notifier_sends_short_voice_payload() -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url, payload, headers, timeout_seconds):
        calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout_seconds})
        return {"code": 0, "message": "queued"}

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
        timeout_seconds=2.5,
    )

    notifier.notify("Open Trader 美股开始通知", "Open Trader｜开始通知")

    assert calls[0]["payload"] == {
        "device_id": "speaker-1",
        "title": "Open Trader 美股开始通知",
        "message": "Open Trader 提醒：美股盘前流程已开始。正在生成今日交易复核清单，完成后会继续通知。",
    }


def test_xiaozhi_voice_notifier_skips_daily_action_notification() -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url, payload, headers, timeout_seconds):
        calls.append({"payload": payload})
        return {"code": 0}

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
    )

    notifier.notify("Open Trader 美股行动通知", "Open Trader｜行动通知")

    assert calls == []
```

- [ ] **Step 2: Run integration tests to verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_notifications.py -k "xiaozhi_voice_notifier" -v
```

Expected: FAIL because the notifier still sends the original message and does not skip daily action.

- [ ] **Step 3: Update XiaozhiVoiceNotifier**

In `XiaozhiVoiceNotifier.notify()`, call the formatter first:

```python
voice_message = render_xiaozhi_voice_notification(title, message)
if voice_message is None:
    return
payload = {"device_id": self.device_id, "title": title, "message": voice_message}
```

- [ ] **Step 4: Run integration tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_notifications.py -k "xiaozhi_voice_notifier" -v
```

Expected: PASS.

### Task 4: Full Verification And Commit

**Files:**
- Modified tests and implementation from previous tasks.

- [ ] **Step 1: Run focused notification tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: PASS.

- [ ] **Step 2: Run relevant daily notification tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_daily_premarket.py -k "notify or completion or blocker" -v
```

Expected: PASS.

- [ ] **Step 3: Review git diff**

Run:

```bash
git diff --stat
git diff -- src/open_trader/notifications.py tests/test_notifications.py
```

Expected: only formatter, Xiaozhi notifier, and tests changed.

- [ ] **Step 4: Commit**

Run:

```bash
git add src/open_trader/notifications.py tests/test_notifications.py docs/superpowers/plans/2026-07-05-xiaozhi-voice-templates.md
git commit -m "feat: add xiaozhi voice templates"
```

Expected: commit created on `feature/xiaozhi-voice-notifier`.

## Self-Review

- Spec coverage: daily start, blocker, action skip, completion with duration, T signal, fallback, and test coverage are included.
- Placeholder scan: no TODO/TBD placeholders are used.
- Type consistency: formatter accepts `title: str, message: str` and returns `str | None`, matching `XiaozhiVoiceNotifier.notify()`.
