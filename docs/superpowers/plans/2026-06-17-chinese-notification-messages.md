# Chinese Notification Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace English notification message bodies with compact Chinese mobile-friendly daily and trigger summaries.

**Architecture:** Keep the existing `Notifier` interface and Feishu text delivery unchanged. Update only the shared renderers in `src/open_trader/notifications.py` so daily and trigger messages use the same Chinese field contract: `标的`, `方向`, `仓位`. Add focused renderer tests that lock the exact mobile-readable content.

**Tech Stack:** Python 3.12, pytest, existing stdlib-only notification module.

---

## File Structure

- Modify `tests/test_notifications.py`: update renderer assertions to the new Chinese message contract.
- Modify `src/open_trader/notifications.py`: update `render_daily_trade_action_message()`, `render_trigger_message()`, and small formatting helpers.

## Task 1: Daily Notification Renderer

**Files:**
- Modify: `tests/test_notifications.py`
- Modify: `src/open_trader/notifications.py`

- [ ] **Step 1: Write the failing daily renderer test**

Update `test_render_daily_trade_action_message_groups_rows()` in `tests/test_notifications.py` so it asserts:

```python
assert "【Open Trader 日报】2026-06-17" in message
assert "汇总：可执行 1｜需复核 1｜观察 1" in message
assert "标的｜方向｜仓位" in message
assert "US.MSFT｜买入｜3股" in message
assert "US.TSLA｜复核｜暂无，需人工确认" in message
assert "US.AAPL｜观察｜不操作" in message
assert "报告：" in message
assert "reports/trade_actions/2026-06-17.md" in message
```

- [ ] **Step 2: Run the daily renderer test to verify it fails**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py::test_render_daily_trade_action_message_groups_rows -v
```

Expected: FAIL because the message still uses English headings such as `Summary`, `Ready`, and `Watch`.

- [ ] **Step 3: Implement the daily renderer**

In `src/open_trader/notifications.py`, update `render_daily_trade_action_message()` to produce:

```text
【Open Trader 日报】<run_date>

汇总：可执行 <ready>｜需复核 <review>｜观察 <watch>

标的｜方向｜仓位
<futu_symbol>｜<中文方向>｜<仓位>

报告：
<trade_actions_report_path>
```

Add helper functions:

```python
def _direction_text(action: str) -> str:
    ...

def _position_text(row: Mapping[str, str]) -> str:
    ...
```

`_position_text()` returns:

- `"<quantity>股 / <currency> <notional>"` when both quantity and notional exist.
- `"<quantity>股"` when only quantity exists.
- `"<currency> <notional>"` when only notional exists.
- `"暂无，需人工确认"` for `REVIEW`.
- `"不操作"` for `HOLD` or watch rows.
- `"暂无"` otherwise.

- [ ] **Step 4: Run the daily renderer test to verify it passes**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py::test_render_daily_trade_action_message_groups_rows -v
```

Expected: PASS.

## Task 2: Trigger Notification Renderer

**Files:**
- Modify: `tests/test_notifications.py`
- Modify: `src/open_trader/notifications.py`

- [ ] **Step 1: Write the failing trigger renderer test**

Update `test_render_trigger_message_contains_action_detail()` in `tests/test_notifications.py` so it asserts:

```python
assert "【价格触发】US.MSFT" in message
assert "标的：US.MSFT" in message
assert "方向：买入" in message
assert "仓位：3股 / USD 1197" in message
assert "价格：399" in message
assert "原因：entered entry zone" in message
```

- [ ] **Step 2: Run the trigger renderer test to verify it fails**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py::test_render_trigger_message_contains_action_detail -v
```

Expected: FAIL because the trigger message still uses English labels.

- [ ] **Step 3: Implement the trigger renderer**

In `src/open_trader/notifications.py`, update `render_trigger_message()` to produce:

```text
【价格触发】<futu_symbol>

标的：<futu_symbol>
方向：<中文方向>
仓位：<仓位>
价格：<last_price>
原因：<trimmed reason>
报告：<report_path>
```

Reuse `_direction_text()` and `_position_text()` from Task 1.

- [ ] **Step 4: Run notification tests to verify both renderers pass**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: PASS.

## Task 3: Completion And Preview

**Files:**
- No additional source files.

- [ ] **Step 1: Run the full test suite**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest
```

Expected: PASS.

- [ ] **Step 2: Send two real Feishu preview messages**

Use the existing `config/daily_premarket.env` and `build_notifier_from_values()` to send:

- One simulated daily message rendered by `render_daily_trade_action_message()`.
- One simulated trigger message rendered by `render_trigger_message()`.

Both messages must include `[模拟消息，不会交易]` at the top.

- [ ] **Step 3: Commit**

Run:

```bash
git add src/open_trader/notifications.py tests/test_notifications.py docs/superpowers/plans/2026-06-17-chinese-notification-messages.md
git commit -m "feat: localize notification messages"
```
