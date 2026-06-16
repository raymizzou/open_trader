# WeCom Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Enterprise WeChat webhook notifications for daily trade-action reports and intraday action triggers.

**Architecture:** Add a focused `open_trader.notifications` module for notifier implementations, message rendering, and notification state. Extend the daily runner to generate dated `trade_actions.csv` before sending a daily summary, and add a `watch-actions` CLI command that long-polls Futu quotes, sends trigger messages, and records same-day silence state.

**Tech Stack:** Python 3.12, stdlib `urllib.request` for webhook POSTs, existing CSV/dataclass patterns, pytest.

---

## File Structure

- Create `src/open_trader/notifications.py`: notifier protocol and implementations, config parsing, WeCom payload sending, daily and trigger message rendering, trigger state load/save.
- Modify `src/open_trader/daily_premarket.py`: import shared notifier classes from `notifications`, add config fields for notification settings, add trade-action generation inside the daily run, record notification errors.
- Modify `src/open_trader/cli.py`: build notifiers from env config for `run-daily-premarket`; add `watch-actions` parser and command handler.
- Modify `config/daily_premarket.env.example`: document notification env vars with placeholder webhook.
- Modify `docs/monthly_portfolio_import.md`: document daily WeCom setup and `watch-actions`.
- Add `tests/test_notifications.py`: notifier payloads, rendering, config, state dedupe.
- Modify `tests/test_daily_premarket.py`: daily runner trade-action generation and notification behavior.
- Modify `tests/test_premarket_cli.py`: CLI config wires notifier; `watch-actions --help`.
- Add `tests/test_action_watch_cli.py`: `watch-actions --once` trigger, dry-run, and dedupe behavior.

## Task 1: Notification Module

**Files:**
- Create: `src/open_trader/notifications.py`
- Test: `tests/test_notifications.py`

- [ ] **Step 1: Write failing tests for WeCom payload, composite behavior, daily rendering, trigger rendering, and state dedupe**

Add tests that import the wished-for API:

```python
from pathlib import Path

from open_trader.notifications import (
    CompositeNotifier,
    NotificationSendError,
    NotificationState,
    RecordingNotifier,
    WeComWebhookNotifier,
    render_daily_trade_action_message,
    render_trigger_message,
)


def test_wecom_webhook_notifier_sends_markdown_payload() -> None:
    calls = []
    notifier = WeComWebhookNotifier(
        webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
        sender=lambda url, payload, timeout: calls.append((url, payload, timeout)),
        timeout_seconds=3.0,
    )

    notifier.notify("Open Trader", "# Report")

    assert calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
            {"msgtype": "markdown", "markdown": {"content": "# Report"}},
            3.0,
        )
    ]


def test_composite_notifier_continues_after_child_failure() -> None:
    class FailingNotifier:
        def notify(self, title: str, message: str) -> None:
            raise RuntimeError("webhook failed")

    recording = RecordingNotifier()
    composite = CompositeNotifier([FailingNotifier(), recording])

    try:
        composite.notify("Title", "Message")
    except NotificationSendError as exc:
        assert "webhook failed" in str(exc)
    else:
        raise AssertionError("expected NotificationSendError")

    assert recording.messages == [("Title", "Message")]


def test_render_daily_trade_action_message_groups_rows() -> None:
    message = render_daily_trade_action_message(
        run_date="2026-06-17",
        status="success",
        premarket={"ok": 12, "fallback": 0, "error": 0},
        futu_status={"checked": 13, "missing": 0, "triggered": 2},
        action_rows=[
            {
                "futu_symbol": "US.MSFT",
                "action": "BUY",
                "priority": "high",
                "last_price": "399",
                "suggested_quantity": "3",
                "status": "ready",
                "reason": "entered entry zone",
            },
            {
                "futu_symbol": "US.TSLA",
                "action": "REVIEW",
                "priority": "medium",
                "last_price": "",
                "suggested_quantity": "",
                "status": "review",
                "reason": "missing_quote",
            },
            {
                "futu_symbol": "US.AAPL",
                "action": "HOLD",
                "priority": "low",
                "last_price": "210",
                "suggested_quantity": "",
                "status": "watch",
                "reason": "wait",
            },
        ],
        daily_report_path=Path("reports/daily_runs/2026-06-17.md"),
        trade_actions_report_path=Path("reports/trade_actions/2026-06-17.md"),
    )

    assert "# Open Trader 2026-06-17: success" in message
    assert "- Actions: 1 ready, 1 review, 1 watch" in message
    assert "- US.MSFT BUY high @ 399, qty 3, entered entry zone" in message
    assert "- US.TSLA REVIEW medium, missing_quote" in message
    assert "- US.AAPL HOLD low @ 210, wait" in message


def test_render_trigger_message_contains_action_detail() -> None:
    message = render_trigger_message(
        run_date="2026-06-17",
        row={
            "futu_symbol": "US.MSFT",
            "action": "BUY",
            "last_price": "399",
            "suggested_quantity": "3",
            "suggested_notional": "1197",
            "notional_currency": "USD",
            "reason": "entered entry zone",
            "trigger_status": "entry_zone",
        },
        report_path=Path("reports/trade_actions/2026-06-17.md"),
    )

    assert "# Open Trader Trigger" in message
    assert "US.MSFT BUY triggered" in message
    assert "- Price: 399" in message
    assert "- Quantity: 3" in message
    assert "- Notional: USD 1197" in message


def test_notification_state_records_sent_keys(tmp_path: Path) -> None:
    path = tmp_path / "notification_state.json"
    state = NotificationState.load(path)

    assert state.was_sent("2026-06-17", "US.MSFT", "entry_zone") is False
    state.record_sent("2026-06-17", "US.MSFT", "entry_zone")
    state.save()

    reloaded = NotificationState.load(path)
    assert reloaded.was_sent("2026-06-17", "US.MSFT", "entry_zone") is True
    assert reloaded.was_sent("2026-06-17", "US.MSFT", "stop_loss_hit") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: FAIL because `open_trader.notifications` does not exist.

- [ ] **Step 3: Implement notification module**

Create `src/open_trader/notifications.py` with:

- `Notifier` protocol
- `NullNotifier`, `MacOSNotifier`, `RecordingNotifier`
- `NotificationSendError`
- `CompositeNotifier`
- `WeComWebhookNotifier`
- `_send_wecom_payload` using `urllib.request`
- `render_daily_trade_action_message`
- `render_trigger_message`
- `NotificationState`
- `load_trade_action_rows`

Key behavior:

- WeCom payload defaults to `{"msgtype": "markdown", "markdown": {"content": message}}`.
- `CompositeNotifier` calls every child, collects child exceptions, then raises one `NotificationSendError` if any failed.
- `NotificationState.save()` writes JSON atomically under `data/runs/<date>/notification_state.json`.
- `render_daily_trade_action_message()` groups rows by `ready`, `review`, `watch`.
- Message renderers cap individual reason strings so one row cannot make the WeCom message unwieldy.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/notifications.py tests/test_notifications.py
git commit -m "feat: add notification primitives"
```

## Task 2: Daily Runner Trade Actions And Daily Notification

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `tests/test_daily_premarket.py`

- [ ] **Step 1: Write failing tests for daily trade-action generation and notification errors**

Extend `tests/test_daily_premarket.py`:

```python
class RecordingDailyNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class FailingDailyNotifier:
    def notify(self, title: str, message: str) -> None:
        raise RuntimeError("webhook failed")


def test_daily_runner_generates_trade_actions_and_sends_daily_notification(tmp_path: Path) -> None:
    config = make_daily_config(tmp_path)
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text(
        "market,asset_class,symbol,currency,total_quantity,market_value,fx_to_hkd,market_value_hkd,portfolio_weight_hkd\n"
        "US,stock,MSFT,USD,2,798,7.8,6224.4,1.0%\n"
        "US,cash,USD,USD,0,10000,7.8,78000,12.0%\n",
        encoding="utf-8",
    )
    notifier = RecordingDailyNotifier()

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=notifier,
    )

    result = runner.run("2026-06-17")

    assert result.status == "success"
    assert (tmp_path / "data/runs/2026-06-17/trade_actions.csv").exists()
    assert (tmp_path / "reports/trade_actions/2026-06-17.md").exists()
    assert notifier.messages
    assert "# Open Trader 2026-06-17: success" in notifier.messages[-1][1]
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert status["artifacts"]["trade_actions"] == str(
        tmp_path / "data/runs/2026-06-17/trade_actions.csv"
    )


def test_daily_runner_records_notification_error_without_failing_run(tmp_path: Path) -> None:
    config = make_daily_config(tmp_path)
    config.portfolio.parent.mkdir(parents=True, exist_ok=True)
    config.portfolio.write_text(
        "market,asset_class,symbol,currency,total_quantity,market_value,fx_to_hkd,market_value_hkd,portfolio_weight_hkd\n"
        "US,stock,MSFT,USD,2,798,7.8,6224.4,1.0%\n"
        "US,cash,USD,USD,0,10000,7.8,78000,12.0%\n",
        encoding="utf-8",
    )

    runner = DailyPremarketRunner(
        config=config,
        premarket_runner=FakePremarket(),
        plan_builder=FakePlanBuilder(),
        quote_client_factory=FakeQuoteClient,
        notifier=FailingDailyNotifier(),
    )

    result = runner.run("2026-06-17")

    assert result.status == "success"
    status = json.loads(result.status_path.read_text(encoding="utf-8"))
    assert "webhook failed" in status["notification_error"]
```

If `make_daily_config()` does not already exist, add a test helper returning the existing `DailyPremarketConfig` used throughout the file.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_daily_premarket.py -v
```

Expected: FAIL because the runner does not generate `trade_actions.csv` and does not record notification errors in status.

- [ ] **Step 3: Implement daily runner changes**

Modify `src/open_trader/daily_premarket.py`:

- Import shared notifier classes from `open_trader.notifications`.
- Add `notify_daily_report: bool = True` to `DailyPremarketConfig`.
- Inside `_run_locked`, after plan generation and Futu snapshots, call `generate_trade_actions()` with the same snapshots and dated plan/portfolio paths.
- Include `trade_actions`, `latest_trade_actions`, and `trade_actions_report` in `artifacts`.
- Build the daily WeCom message from dated action rows and status payload.
- Replace `_notify()` swallowing behavior with a helper that returns an error string. Add the error to status payload as `notification_error` when non-empty.
- Keep notification failure from changing the run's trading `status`.
- Keep dry-run from promoting latest trade actions.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_daily_premarket.py tests/test_notifications.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/daily_premarket.py tests/test_daily_premarket.py
git commit -m "feat: notify daily trade actions"
```

## Task 3: CLI Config And WeCom Wiring

**Files:**
- Modify: `src/open_trader/cli.py`
- Modify: `config/daily_premarket.env.example`
- Modify: `tests/test_premarket_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Add tests:

```python
def test_run_daily_premarket_builds_wecom_notifier_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "daily.env"
    env.write_text(
        "\n".join(
            [
                f"OPEN_TRADER_REPO={tmp_path}",
                f"OPEN_TRADER_PYTHON={tmp_path / '.venv/bin/python'}",
                "OPEN_TRADER_TIMEZONE=Asia/Shanghai",
                "OPEN_TRADER_DEADLINE=21:10",
                "OPEN_TRADER_FUTU_HOST=127.0.0.1",
                "OPEN_TRADER_FUTU_PORT=11111",
                "DEEPSEEK_API_KEY=secret",
                "OPEN_TRADER_NOTIFIERS=wecom",
                "OPEN_TRADER_WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
            ]
        ),
        encoding="utf-8",
    )
    captured = {}

    class FakeRunner:
        def __init__(self, *, config, notifier):
            captured["config"] = config
            captured["notifier"] = notifier

        def run(self, *, run_date, dry_run):
            return type("Result", (), {
                "status": "success",
                "status_path": tmp_path / "status.json",
                "report_path": tmp_path / "report.md",
                "log_path": tmp_path / "run.log",
            })()

    monkeypatch.setattr(cli, "DailyPremarketRunner", FakeRunner)

    result = cli.main(["run-daily-premarket", "--date", "2026-06-17", "--config", str(env)])

    assert result == 0
    assert captured["notifier"].__class__.__name__ == "CompositeNotifier"


def test_watch_actions_help_is_registered() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["watch-actions", "--help"])
    assert exc_info.value.code == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_premarket_cli.py -v
```

Expected: FAIL because config does not build WeCom notifier and `watch-actions` is not registered.

- [ ] **Step 3: Implement CLI config**

Modify `load_env_config()` or add a `build_notifier_from_env(values, dry_run)` helper in `notifications.py`, then wire it from `cli.py`:

- `OPEN_TRADER_NOTIFIERS` comma list supports `wecom`, `macos`, and empty/`none`.
- `OPEN_TRADER_WECOM_WEBHOOK_URL` is required when `wecom` is enabled unless dry-run disables sending.
- `--dry-run` uses `NullNotifier` or a recording/logging no-op for WeCom.
- `run-daily-premarket` passes `notifier=...` into `DailyPremarketRunner`.
- Add `watch-actions` parser now; handler can return parser error until Task 4 implements behavior if needed, but `--help` must work.

Update `config/daily_premarket.env.example` with the notification env vars from the spec.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_premarket_cli.py tests/test_notifications.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/cli.py src/open_trader/notifications.py config/daily_premarket.env.example tests/test_premarket_cli.py tests/test_notifications.py
git commit -m "feat: wire notification config"
```

## Task 4: Intraday Watch-Actions Command

**Files:**
- Modify: `src/open_trader/cli.py`
- Modify: `src/open_trader/notifications.py`
- Add or modify: `tests/test_action_watch_cli.py`

- [ ] **Step 1: Write failing watcher tests**

Create `tests/test_action_watch_cli.py`:

```python
from decimal import Decimal
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.futu_watch import QuoteSnapshot


class FakeQuoteClient:
    def __init__(self, *, host: str, port: int) -> None:
        self.closed = False

    def get_snapshots(self, futu_symbols: list[str]) -> dict[str, QuoteSnapshot]:
        return {"US.MSFT": QuoteSnapshot(futu_symbol="US.MSFT", last_price=Decimal("399"))}

    def close(self) -> None:
        self.closed = True


def write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "run_date,symbol,market,source_status,fallback_reason,fallback_from_date,rating,entry_zone_low,entry_zone_high,add_price,stop_loss,target_1,target_2,max_weight,catalyst,time_horizon,plan_text,status,error\n"
        "2026-06-17,MSFT,US,ok,,,Overweight,380,400,,350,410,430,3%,fake,1 week,fake,active,\n",
        encoding="utf-8",
    )


def write_actions(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "run_date,symbol,market,futu_symbol,action,priority,last_price,trigger_status,suggested_quantity,suggested_notional,notional_currency,current_quantity,current_weight,target_max_weight,cash_available,limit_price,stop_price,reason,source_plan,status,error\n"
        "2026-06-17,MSFT,US,US.MSFT,BUY,high,399,entry_zone,3,1197,USD,2,1%,3%,10000,399,350,entered entry zone,plan,ready,\n",
        encoding="utf-8",
    )


def test_watch_actions_once_sends_trigger_and_records_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = tmp_path / "data/runs/2026-06-17/trading_plan.csv"
    actions = tmp_path / "data/runs/2026-06-17/trade_actions.csv"
    write_plan(plan)
    write_actions(actions)
    sent = []

    monkeypatch.setattr(cli, "FutuQuoteClient", FakeQuoteClient)
    monkeypatch.setattr(cli, "build_notifier_from_values", lambda values, dry_run=False: type("N", (), {"notify": lambda self, title, message: sent.append((title, message))})())

    result = cli.main([
        "watch-actions",
        "--date",
        "2026-06-17",
        "--plan",
        str(plan),
        "--actions",
        str(actions),
        "--data-dir",
        str(tmp_path / "data"),
        "--reports-dir",
        str(tmp_path / "reports"),
        "--once",
    ])

    assert result == 0
    assert len(sent) == 1
    assert "US.MSFT BUY triggered" in sent[0][1]
    assert (tmp_path / "data/runs/2026-06-17/notification_state.json").exists()


def test_watch_actions_does_not_send_duplicate_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = tmp_path / "data/runs/2026-06-17/trading_plan.csv"
    actions = tmp_path / "data/runs/2026-06-17/trade_actions.csv"
    write_plan(plan)
    write_actions(actions)
    sent = []

    monkeypatch.setattr(cli, "FutuQuoteClient", FakeQuoteClient)
    monkeypatch.setattr(cli, "build_notifier_from_values", lambda values, dry_run=False: type("N", (), {"notify": lambda self, title, message: sent.append((title, message))})())

    args = [
        "watch-actions", "--date", "2026-06-17", "--plan", str(plan), "--actions", str(actions),
        "--data-dir", str(tmp_path / "data"), "--reports-dir", str(tmp_path / "reports"), "--once",
    ]
    assert cli.main(args) == 0
    assert cli.main(args) == 0

    assert len(sent) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_action_watch_cli.py -v
```

Expected: FAIL because `watch-actions` behavior does not exist.

- [ ] **Step 3: Implement watcher behavior**

Implement in `cli.py` or a focused helper in `notifications.py`:

- Load active `TradingPlanRow` values with `load_trading_plan_rows`.
- Load action rows with `load_trade_action_rows`.
- Fetch snapshots from `FutuQuoteClient`.
- Evaluate each active plan using `evaluate_plan_quote`.
- Find the matching action row for `(run_date, futu_symbol, trigger_status)`.
- Skip `watch` and `missing_quote`.
- Load `NotificationState` from `data/runs/<date>/notification_state.json`.
- If key is new, render trigger message, call notifier, then record/save state.
- Support `--once`; otherwise sleep `--poll-seconds` and repeat.
- Close the quote client in `finally`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_action_watch_cli.py tests/test_premarket_cli.py tests/test_notifications.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/cli.py src/open_trader/notifications.py tests/test_action_watch_cli.py tests/test_premarket_cli.py
git commit -m "feat: add action trigger watcher"
```

## Task 5: Documentation And Full Verification

**Files:**
- Modify: `docs/monthly_portfolio_import.md`
- Modify: `docs/superpowers/specs/2026-06-17-wecom-notifier-design.md` only if implementation intentionally diverges from the approved design.

- [ ] **Step 1: Write docs update**

Add a "WeCom Notifications" subsection documenting:

- `OPEN_TRADER_NOTIFIERS=wecom,macos`
- `OPEN_TRADER_WECOM_WEBHOOK_URL=...`
- daily dry-run does not send webhook
- `watch-actions --date today --once` manual test
- long-running `watch-actions` command for market hours
- `notification_state.json` same-day silence behavior

- [ ] **Step 2: Run focused tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py tests/test_daily_premarket.py tests/test_premarket_cli.py tests/test_action_watch_cli.py -v
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest
```

Expected: PASS with all tests passing.

- [ ] **Step 4: Commit docs and any final fixes**

```bash
git add docs/monthly_portfolio_import.md docs/superpowers/specs/2026-06-17-wecom-notifier-design.md
git commit -m "docs: document WeCom notifications"
```

- [ ] **Step 5: Completion audit**

Verify:

- Work occurred in `/Users/ray/projects/open_trader/.worktrees/wecom-notifier`.
- Branch is `feature/wecom-notifier`.
- Spec requirements are represented in code/tests/docs.
- Full suite passes.
- `git status --short` is clean.
