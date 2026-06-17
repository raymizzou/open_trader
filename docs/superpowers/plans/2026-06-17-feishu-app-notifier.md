# Feishu App Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Feishu enterprise app-bot notification channel without removing the existing WeCom, macOS, daily summary, or `watch-actions` behavior.

**Architecture:** Extend `src/open_trader/notifications.py` with a Feishu app client and notifier that obtains a tenant access token and sends text messages through Feishu's message API. The existing notifier factory will accept `feishu_app` alongside `wecom` and `macos`, so the daily runner and watcher keep using the generic `Notifier` interface unchanged.

**Tech Stack:** Python 3.12, stdlib `urllib.request`, stdlib `json`, pytest.

---

## File Structure

- Modify `src/open_trader/notifications.py`: add `FeishuAppClient`, `FeishuAppNotifier`, Feishu request helpers, and `feishu_app` config support.
- Modify `tests/test_notifications.py`: add Feishu token/send payload tests, factory tests, and config validation tests.
- Modify `tests/test_premarket_cli.py`: add CLI env wiring test for `feishu_app`.
- Modify `config/daily_premarket.env.example`: add Feishu app-bot settings without removing WeCom settings.
- Modify `docs/monthly_portfolio_import.md`: document Feishu app-bot setup and test command.

## Task 1: Feishu Client And Notifier

**Files:**
- Modify: `src/open_trader/notifications.py`
- Modify: `tests/test_notifications.py`

- [ ] **Step 1: Write failing Feishu notifier tests**

Append to `tests/test_notifications.py`:

```python
from open_trader.notifications import FeishuAppNotifier


def test_feishu_app_notifier_fetches_token_and_sends_text_message() -> None:
    calls = []

    def sender(url: str, payload: dict[str, object], timeout: float, headers: dict[str, str] | None = None) -> dict[str, object]:
        calls.append((url, payload, timeout, headers or {}))
        if url.endswith("/open-apis/auth/v3/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "tenant-token"}
        return {"code": 0, "data": {"message_id": "om_xxx"}}

    notifier = FeishuAppNotifier(
        app_id="cli_xxx",
        app_secret="secret",
        receive_id_type="email",
        receive_id="you@example.com",
        sender=sender,
        timeout_seconds=3.0,
    )

    notifier.notify("Open Trader", "hello")

    assert calls[0] == (
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": "cli_xxx", "app_secret": "secret"},
        3.0,
        {},
    )
    assert calls[1] == (
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=email",
        {
            "receive_id": "you@example.com",
            "msg_type": "text",
            "content": "{\"text\": \"hello\"}",
        },
        3.0,
        {"Authorization": "Bearer tenant-token"},
    )


def test_feishu_app_notifier_raises_on_nonzero_response() -> None:
    def sender(url: str, payload: dict[str, object], timeout: float, headers: dict[str, str] | None = None) -> dict[str, object]:
        return {"code": 99991663, "msg": "missing permission"}

    notifier = FeishuAppNotifier(
        app_id="cli_xxx",
        app_secret="secret",
        receive_id_type="email",
        receive_id="you@example.com",
        sender=sender,
    )

    try:
        notifier.notify("Open Trader", "hello")
    except NotificationSendError as exc:
        assert "99991663" in str(exc)
        assert "missing permission" in str(exc)
    else:
        raise AssertionError("expected NotificationSendError")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: FAIL because `FeishuAppNotifier` does not exist.

- [ ] **Step 3: Implement Feishu client and notifier**

In `src/open_trader/notifications.py`:

- Import `urllib.parse`.
- Add `FeishuSender = Callable[[str, dict[str, object], float, dict[str, str] | None], dict[str, object]]`.
- Add `FeishuAppClient` with:
  - `get_tenant_access_token()`
  - `send_text(receive_id_type, receive_id, text)`
- Add `FeishuAppNotifier` with `notify(title, message)`.
- Add `_send_feishu_json(url, payload, timeout_seconds, headers=None)`:
  - POST JSON.
  - Return decoded JSON object.
  - Raise `NotificationSendError` for non-2xx and network errors.
- Add `_check_feishu_response(payload, operation)`:
  - Feishu success code is `0`.
  - Raise `NotificationSendError(f"Feishu {operation} error {code}: {msg}")` for non-zero code.
- Send-message endpoint must include `receive_id_type` query parameter.
- Message payload must be:
  ```python
  {
      "receive_id": receive_id,
      "msg_type": "text",
      "content": json.dumps({"text": text}, ensure_ascii=False),
  }
  ```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/notifications.py tests/test_notifications.py
git commit -m "feat: add Feishu app notifier"
```

## Task 2: Feishu Config Factory

**Files:**
- Modify: `src/open_trader/notifications.py`
- Modify: `tests/test_notifications.py`
- Modify: `tests/test_premarket_cli.py`

- [ ] **Step 1: Write failing factory tests**

Add to `tests/test_notifications.py`:

```python
def test_build_notifier_from_values_supports_feishu_app_and_macos() -> None:
    notifier = build_notifier_from_values(
        {
            "OPEN_TRADER_NOTIFIERS": "feishu_app,macos",
            "OPEN_TRADER_FEISHU_APP_ID": "cli_xxx",
            "OPEN_TRADER_FEISHU_APP_SECRET": "secret",
            "OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE": "email",
            "OPEN_TRADER_FEISHU_RECEIVE_ID": "you@example.com",
        }
    )

    assert notifier.__class__.__name__ == "CompositeNotifier"
    assert [child.__class__.__name__ for child in notifier.notifiers] == [
        "FeishuAppNotifier",
        "MacOSNotifier",
    ]


def test_build_notifier_from_values_rejects_incomplete_feishu_config() -> None:
    try:
        build_notifier_from_values({"OPEN_TRADER_NOTIFIERS": "feishu_app"})
    except ValueError as exc:
        message = str(exc)
        assert "OPEN_TRADER_FEISHU_APP_ID" in message
        assert "OPEN_TRADER_FEISHU_APP_SECRET" in message
        assert "OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE" in message
        assert "OPEN_TRADER_FEISHU_RECEIVE_ID" in message
    else:
        raise AssertionError("expected ValueError")
```

Add to `tests/test_premarket_cli.py`:

```python
def test_run_daily_premarket_builds_feishu_notifier_from_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                "OPEN_TRADER_NOTIFIERS=feishu_app",
                "OPEN_TRADER_FEISHU_APP_ID=cli_xxx",
                "OPEN_TRADER_FEISHU_APP_SECRET=secret",
                "OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE=email",
                "OPEN_TRADER_FEISHU_RECEIVE_ID=you@example.com",
            ]
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, *, config: object, notifier: object) -> None:
            captured["notifier"] = notifier

        def run(self, *, run_date: str, dry_run: bool):
            return type(
                "DailyRunResult",
                (),
                {
                    "status": "success",
                    "status_path": tmp_path / "status.json",
                    "report_path": tmp_path / "report.md",
                    "log_path": tmp_path / "run.log",
                },
            )()

    monkeypatch.setattr(cli, "DailyPremarketRunner", FakeRunner)

    result = cli.main(
        [
            "run-daily-premarket",
            "--date",
            "2026-06-17",
            "--config",
            str(env),
        ]
    )

    assert result == 0
    notifier = captured["notifier"]
    assert notifier.__class__.__name__ == "CompositeNotifier"
    assert notifier.notifiers[0].__class__.__name__ == "FeishuAppNotifier"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py tests/test_premarket_cli.py -v
```

Expected: FAIL because `build_notifier_from_values()` does not yet support `feishu_app`.

- [ ] **Step 3: Implement factory support**

In `build_notifier_from_values()`:

- Add `elif name == "feishu_app"`.
- Require these values:
  - `OPEN_TRADER_FEISHU_APP_ID`
  - `OPEN_TRADER_FEISHU_APP_SECRET`
  - `OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE`
  - `OPEN_TRADER_FEISHU_RECEIVE_ID`
- If any are missing, raise one `ValueError` listing all missing keys.
- Construct `FeishuAppNotifier`.
- Accept `OPEN_TRADER_FEISHU_MESSAGE_FORMAT`; first version only supports `text`, so reject any non-empty non-`text` value.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_notifications.py tests/test_premarket_cli.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/notifications.py tests/test_notifications.py tests/test_premarket_cli.py
git commit -m "feat: wire Feishu notifier config"
```

## Task 3: Env Example And Docs

**Files:**
- Modify: `config/daily_premarket.env.example`
- Modify: `docs/monthly_portfolio_import.md`

- [ ] **Step 1: Update env example**

Add Feishu settings next to notification settings while keeping WeCom examples:

```bash
OPEN_TRADER_FEISHU_APP_ID=cli_replace_me
OPEN_TRADER_FEISHU_APP_SECRET=replace-me
OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE=email
OPEN_TRADER_FEISHU_RECEIVE_ID=you@example.com
OPEN_TRADER_FEISHU_MESSAGE_FORMAT=text
```

- [ ] **Step 2: Update docs**

In `docs/monthly_portfolio_import.md`, add a "Feishu App Notifications" subsection:

- Explain that this is for Feishu enterprise custom app bot.
- Show config:
  ```bash
  OPEN_TRADER_NOTIFIERS=feishu_app,macos
  OPEN_TRADER_FEISHU_APP_ID=cli_xxx
  OPEN_TRADER_FEISHU_APP_SECRET=...
  OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE=email
  OPEN_TRADER_FEISHU_RECEIVE_ID=you@example.com
  OPEN_TRADER_FEISHU_MESSAGE_FORMAT=text
  ```
- Mention `open_id`, `user_id`, `union_id`, and `chat_id` as alternatives.
- State that Feishu's message API does not accept `mobile` directly.
- State that the app must be published/installed and have message-send permission.
- Keep existing WeCom documentation.

- [ ] **Step 3: Run full test suite**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest
```

Expected: PASS.

- [ ] **Step 4: Commit docs**

```bash
git add config/daily_premarket.env.example docs/monthly_portfolio_import.md
git commit -m "docs: document Feishu app notifications"
```

## Task 4: Completion Audit

**Files:**
- No code changes expected.

- [ ] **Step 1: Verify status and tests**

Run:

```bash
git status --short
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest
```

Expected:

- `git status --short` is empty.
- Full suite passes.

- [ ] **Step 2: Verify requirements**

Check:

- Existing WeCom notifier class and env vars still exist.
- `FeishuAppNotifier` exists.
- `OPEN_TRADER_NOTIFIERS=feishu_app,macos` is documented and tested.
- Missing Feishu config produces clear `ValueError`.
- Feishu API non-zero code raises `NotificationSendError`.
- No daily runner or watcher public workflow changes were required.
