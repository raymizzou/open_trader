# Auto Blocker Feishu Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add automatic Feishu blocker notifications for daily premarket failures and Futu quote abnormalities.

**Architecture:** Keep notification triggering inside `DailyPremarketRunner`, because it already owns run status, Futu check results, trade action counts, dry-run handling, and notifier dispatch. Add a small pure formatter in `daily_premarket.py` so tests can assert exact Chinese output without touching real Feishu.

**Tech Stack:** Python dataclasses and dict payloads, existing `Notifier` protocol, pytest fixtures in `tests/test_daily_premarket.py`.

---

### Task 1: Add Failing Tests

**Files:**
- Modify: `tests/test_daily_premarket.py`

- [ ] **Step 1: Add tests for blocker notifications**

Add tests that instantiate `DailyPremarketRunner` with `notify_daily_report=True`, a `CapturingNotifier`, and existing fake quote clients. Assert:

- `UnavailableQuoteClient` produces title `Open Trader 阻塞通知` and body containing `Futu 行情异常`.
- `MissingQuoteClient` produces title `Open Trader 阻塞通知` and body containing `缺失行情：1`.
- Missing portfolio produces title `Open Trader 阻塞通知` and body containing `运行失败`.
- Existing dry-run and disabled-notification tests still expect no calls.

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'blocker or futu_is_unavailable or futu_quote_is_missing or portfolio_is_missing or skips_failure_notification' -v
```

Expected: the new blocker notification assertions fail because only the old daily/action notifications exist.

### Task 2: Implement Trigger and Message

**Files:**
- Modify: `src/open_trader/daily_premarket.py`

- [ ] **Step 1: Add a pure blocker formatter**

Add `_blocker_notification_message(...)` that accepts `run_date`, `status`, `futu_status`, `trade_actions`, `artifacts`, and optional `error`, then returns Chinese text with concrete cause and next step.

- [ ] **Step 2: Add a blocker trigger helper**

Add `_should_notify_blocker(...)` returning true for `failed`, Futu error, missing quotes, or review rows.

- [ ] **Step 3: Dispatch blocker notifications**

In `_run_locked`, after writing status/report and before normal action notification, call `_notify("Open Trader 阻塞通知", message)` when the helper says true. In `_write_failure`, send the same title/message with the failure payload. Keep `dry_run` and `notify_daily_report` gating.

- [ ] **Step 4: Preserve existing action notification**

Keep the existing `Open Trader 行动通知` message for normal order review output. Do not let blocker notification rendering failures change run status.

### Task 3: Verify

**Files:**
- Test: `tests/test_daily_premarket.py`

- [ ] **Step 1: Run focused tests**

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -v
```

- [ ] **Step 2: Run full suite**

```bash
.venv/bin/python -m pytest
```

- [ ] **Step 3: Review diff**

```bash
git diff -- src/open_trader/daily_premarket.py tests/test_daily_premarket.py docs/superpowers/specs/2026-06-18-auto-blocker-feishu-notification-design.md docs/superpowers/plans/2026-06-18-auto-blocker-feishu-notification.md
```

