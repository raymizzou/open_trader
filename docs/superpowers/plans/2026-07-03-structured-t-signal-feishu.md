# Structured T Signal Feishu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render 做T Feishu alerts as structured Chinese text with one message per symbol.

**Architecture:** Keep the existing notification send path and dedupe state. Change only `_notification_title()` and `_notification_message()` in `src/open_trader/t_signal_runner.py`, backed by focused tests in `tests/test_t_signal_runner.py`.

**Tech Stack:** Python, pytest, existing Feishu text notifier.

---

### Task 1: Structured Title And Body

**Files:**
- Modify: `src/open_trader/t_signal_runner.py`
- Test: `tests/test_t_signal_runner.py`

- [ ] **Step 1: Write the failing test**

Add a test that builds a BUY_T and SELL_T signal and asserts:

```text
Open Trader｜做T提醒｜HK.02840｜卖出做T
动作：卖出做T
比例：10%
状态：盘中有效，等待执行确认
结论：
依据：
1. ...
时间：...
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_t_signal_runner.py::test_t_signal_notification_uses_structured_chinese_template -q
```

Expected: fail because the current template still renders raw `BUY_T`/`SELL_T`.

- [ ] **Step 3: Implement the template helpers**

Add small helpers for action label, status text, timestamp text, and evidence numbering. Use them from `_notification_title()` and `_notification_message()`.

- [ ] **Step 4: Run focused and related tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_t_signal_runner.py -q
```

- [ ] **Step 5: Verify with real Feishu path**

Run `watch-t --market HK --once` with the real config only after tests pass, then inspect the latest signal notification state.
