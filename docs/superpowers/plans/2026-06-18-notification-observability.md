# Notification Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make notification delivery attempts visible and add a real test notification CLI command.

**Architecture:** Keep daily runs resilient by logging notification success/failure without raising from `_notify()`. Add a dedicated CLI command that sends a Chinese test message through the configured notifier and returns a non-zero exit code when the notifier raises.

**Tech Stack:** Python, argparse, pytest, existing `Notifier` protocol and `DailyPremarketRunner`.

---

### Task 1: Daily Notification Logging

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Test: `tests/test_daily_premarket.py`

- [ ] **Step 1: Write the failing tests**

Append tests near `CapturingNotifier` in `tests/test_daily_premarket.py`:

```python
class FailingNotifier:
    def notify(self, title: str, message: str) -> None:
        raise RuntimeError("delivery failed")


def test_daily_notify_logs_success(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    notifier = CapturingNotifier()
    runner = DailyPremarketRunner(
        config=DailyPremarketConfig(
            repo=tmp_path,
            python=tmp_path / ".venv/bin/python",
            timezone="Asia/Shanghai",
            deadline="21:10",
            futu_host="127.0.0.1",
            futu_port=11111,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            logs_dir=tmp_path / "logs",
            portfolio=tmp_path / "data/latest/portfolio.csv",
        ),
        notifier=notifier,
    )

    with caplog.at_level("INFO", logger="open_trader.daily_premarket"):
        runner._notify("Open Trader 行动通知", "测试正文")

    assert notifier.messages == [("Open Trader 行动通知", "测试正文")]
    assert "通知已发送：Open Trader 行动通知" in caplog.text


def test_daily_notify_logs_failure_without_raising(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = DailyPremarketRunner(
        config=DailyPremarketConfig(
            repo=tmp_path,
            python=tmp_path / ".venv/bin/python",
            timezone="Asia/Shanghai",
            deadline="21:10",
            futu_host="127.0.0.1",
            futu_port=11111,
            data_dir=tmp_path / "data",
            reports_dir=tmp_path / "reports",
            logs_dir=tmp_path / "logs",
            portfolio=tmp_path / "data/latest/portfolio.csv",
        ),
        notifier=FailingNotifier(),
    )

    with caplog.at_level("WARNING", logger="open_trader.daily_premarket"):
        runner._notify("Open Trader 行动通知", "测试正文")

    assert "通知发送失败：Open Trader 行动通知" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "delivery failed" in caplog.text
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_daily_premarket.py::test_daily_notify_logs_success tests/test_daily_premarket.py::test_daily_notify_logs_failure_without_raising -v
```

Expected: both tests fail because `_notify()` does not log.

- [ ] **Step 3: Implement minimal logging**

In `src/open_trader/daily_premarket.py`, import `logging`, define:

```python
LOGGER = logging.getLogger(__name__)
```

Change `_notify()` to:

```python
def _notify(self, title: str, message: str) -> None:
    try:
        self.notifier.notify(title, message)
    except Exception as exc:
        LOGGER.warning(
            "通知发送失败：%s error_type=%s error=%s",
            title,
            exc.__class__.__name__,
            str(exc),
        )
        return
    LOGGER.info("通知已发送：%s", title)
```

- [ ] **Step 4: Run tests to verify pass**

Run the same pytest command. Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/daily_premarket.py tests/test_daily_premarket.py
git commit -m "feat: log notification delivery results"
```

### Task 2: Test Notification CLI

**Files:**
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_premarket_cli.py`

- [ ] **Step 1: Write the failing CLI tests**

Append tests near the daily CLI tests in `tests/test_premarket_cli.py`:

```python
def test_test_notification_main_sends_chinese_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[tuple[str, str]] = []
    config = SimpleNamespace()

    class FakeNotifier:
        def notify(self, title: str, message: str) -> None:
            sent.append((title, message))

    def fake_load_env_config(path: Path, *, dry_run: bool):
        assert dry_run is False
        return config

    monkeypatch.setattr(cli, "load_env_config", fake_load_env_config)
    monkeypatch.setattr(cli, "build_notifier", lambda loaded: FakeNotifier())

    result = cli.main(["test-notification", "--config", str(tmp_path / "daily.env")])

    assert result == 0
    assert sent == [("Open Trader 测试通知", "这是一条 Open Trader 测试通知。")]
    assert "通知测试已发送" in capsys.readouterr().out


def test_test_notification_main_returns_nonzero_when_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingNotifier:
        def notify(self, title: str, message: str) -> None:
            raise RuntimeError("delivery failed")

    monkeypatch.setattr(cli, "load_env_config", lambda path, dry_run=False: SimpleNamespace())
    monkeypatch.setattr(cli, "build_notifier", lambda config: FailingNotifier())

    result = cli.main(["test-notification", "--config", str(tmp_path / "daily.env")])

    assert result == 1
    assert "通知测试失败" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_premarket_cli.py::test_test_notification_main_sends_chinese_message tests/test_premarket_cli.py::test_test_notification_main_returns_nonzero_when_send_fails -v
```

Expected: both tests fail because `test-notification` does not exist.

- [ ] **Step 3: Implement CLI command**

In `build_parser()`, add:

```python
    test_notification_parser = subparsers.add_parser(
        "test-notification",
        help="Send a test notification using configured notifiers",
    )
    test_notification_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/daily_premarket.env"),
    )
```

In `main()`, before `run-daily-premarket`, add:

```python
    if args.command == "test-notification":
        try:
            config = load_env_config(args.config, dry_run=False)
            notifier = build_notifier(config)
            notifier.notify(
                "Open Trader 测试通知",
                "这是一条 Open Trader 测试通知。",
            )
        except (
            FileNotFoundError,
            ValueError,
            RuntimeError,
            argparse.ArgumentTypeError,
            ZoneInfoNotFoundError,
        ) as exc:
            print(f"通知测试失败：{exc}", file=sys.stderr)
            return 1
        print("通知测试已发送。")
        return 0
```

Also import `sys` at the top of `src/open_trader/cli.py`.

- [ ] **Step 4: Run tests to verify pass**

Run the same pytest command. Expected: both tests pass.

- [ ] **Step 5: Run focused suite and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_daily_premarket.py tests/test_premarket_cli.py tests/test_notifications.py -v
```

Expected: all selected tests pass.

Commit:

```bash
git add src/open_trader/cli.py tests/test_premarket_cli.py
git commit -m "feat: add notification test command"
```

### Task 3: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run full test suite**

```bash
PYTHONPATH=src .venv/bin/python -m pytest
```

Expected: all tests pass.

- [ ] **Step 2: Run local test notification**

```bash
.venv/bin/python -m open_trader test-notification --config config/daily_premarket.env
```

Expected: terminal prints `通知测试已发送。`; user should receive the configured notification.

- [ ] **Step 3: Check git status**

```bash
git status --short
```

Expected: no tracked changes.
