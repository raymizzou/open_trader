# Open Trader Native XiaoAI Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Play the existing Open Trader voice allowlist directly through the XiaoAI speaker's native TTS over SSH.

**Architecture:** Replace the Xiaozhi HTTP notifier with one SSH notifier in `notifications.py`. Reuse the existing formatter and watcher delivery flow, rename the channel/configuration cleanly, and serialize independent watcher processes with `fcntl.flock` without adding a daemon or dependency.

**Tech Stack:** Python 3.12 stdlib (`fcntl`, `shlex`, `subprocess`, `pathlib`), pytest, OpenSSH, XiaoAI `/usr/sbin/tts_play.sh`.

## Global Constraints

- Automatic voice output remains limited to manual tests and CN/HK/US protection-line triggers.
- Voice hours remain `08:00` inclusive to `23:00` exclusive in `Asia/Shanghai`.
- One attempt only; failures remain terminal and generate one Feishu-only warning.
- Use `OPEN_TRADER_XIAOAI_HOST` and `OPEN_TRADER_XIAOAI_SSH_KEY`; remove old Xiaozhi HTTP configuration.
- Keep the old repositories unchanged and add no Python dependency.
- Run `make acceptance`; only `PASS` permits deployment or completion language.

---

### Task 1: Native SSH Playback Transport

**Files:**
- Modify: `tests/test_notifications.py`
- Modify: `src/open_trader/notifications.py`

**Interfaces:**
- Produces: `XiaoaiSSHNotifier(host: str, ssh_key: Path, run_command=..., timeout_seconds=30.0, lock_path=Path("/tmp/open_trader_xiaoai_voice.lock"), now_fn=...)`.
- Produces: `XiaoaiVoiceSuppressed`, `render_xiaoai_voice_notification()`, and `xiaoai_voice_allowed()`.

- [ ] **Step 1: Replace the HTTP notifier tests with failing SSH behavior tests**

Cover the exact command shape, one safely quoted remote argument containing a single quote, preserved protection text, quiet-hour no-op, post-lock `XiaoaiVoiceSuppressed`, nonzero exit, timeout, and a two-thread serialization check using a temporary lock path. The command assertion must include:

```python
[
    "ssh", "-i", str(key), "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5", "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "root@192.168.1.107",
    "/usr/sbin/tts_play.sh '测试文本'",
]
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_notifications.py -q`

Expected: collection fails because `XiaoaiSSHNotifier` and the renamed helpers do not exist.

- [ ] **Step 3: Implement the minimum notifier**

Use `shlex.quote(voice_message)` for the remote command, `subprocess.run(..., capture_output=True, text=True, timeout=...)`, and a blocking `fcntl.LOCK_EX` around the second voice-hours check and command. Do not include stderr, spoken text, or the full command in raised errors. Delete `xiaozhi_not_after()`, `_post_xiaozhi_json_with_headers()`, and the old HTTP notifier.

```python
class XiaoaiVoiceSuppressed(RuntimeError):
    pass


class XiaoaiSSHNotifier:
    def __init__(
        self,
        *,
        host: str,
        ssh_key: Path,
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 30.0,
        lock_path: Path = Path("/tmp/open_trader_xiaoai_voice.lock"),
        now_fn: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
    ) -> None:
        self.host = host
        self.ssh_key = ssh_key
        self._run_command = run_command
        self.timeout_seconds = timeout_seconds
        self.lock_path = lock_path
        self._now_fn = now_fn

    def notify(self, title: str, message: str) -> None:
        voice_message = render_xiaoai_voice_notification(title, message)
        if voice_message is None or not xiaoai_voice_allowed(self._now_fn()):
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if not xiaoai_voice_allowed(self._now_fn()):
                raise XiaoaiVoiceSuppressed("quiet hours")
            command = [
                "ssh", "-i", str(self.ssh_key),
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "-o", "HostKeyAlgorithms=+ssh-rsa",
                "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
                f"root@{self.host}",
                f"/usr/sbin/tts_play.sh {shlex.quote(voice_message)}",
            ]
            try:
                result = self._run_command(
                    command, capture_output=True, text=True,
                    timeout=self.timeout_seconds, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise NotificationError("XiaoAI voice command failed") from exc
            if result.returncode != 0:
                raise NotificationError(
                    f"XiaoAI voice command failed with exit code {result.returncode}"
                )
```

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `.venv/bin/python -m pytest tests/test_notifications.py -q`

Expected: all notification tests pass.

### Task 2: Clean Configuration and Routing Cutover

**Files:**
- Modify: `tests/test_daily_premarket.py`
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `config/daily_premarket.env.example`

**Interfaces:**
- Produces: `DailyPremarketConfig.xiaoai_host: str` and `xiaoai_ssh_key: Path | None`.
- Consumes: Task 1's `XiaoaiSSHNotifier`.
- Produces: notifier channel name `xiaoai` and `NotificationAttempt.suppressed: bool`.

- [ ] **Step 1: Write failing configuration and result tests**

Change fixtures to load:

```text
OPEN_TRADER_NOTIFIERS=feishu_app,xiaoai
OPEN_TRADER_XIAOAI_HOST=192.168.1.107
OPEN_TRADER_XIAOAI_SSH_KEY=/tmp/open-trader-xiaoai
```

Assert `build_notifier()` builds `XiaoaiSSHNotifier`, missing host/key names the exact missing variable, `_notifier_channel()` returns `xiaoai`, and `send_notification_with_results()` represents `XiaoaiVoiceSuppressed` as `success=False, suppressed=True`.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_daily_premarket.py -q`

Expected: failures reference absent XiaoAI fields/channel.

- [ ] **Step 3: Implement the configuration cutover**

Replace the three Xiaozhi fields with the two XiaoAI fields, expand the SSH key path, build the SSH notifier for `xiaoai`, add `suppressed: bool = False` to `NotificationAttempt`, and catch only `XiaoaiVoiceSuppressed` as a non-failure suppression result. Update the example environment file and remove old keys.

```python
@dataclass(frozen=True)
class NotificationAttempt:
    channel: str
    success: bool
    error_type: str = ""
    error: str = ""
    suppressed: bool = False


except XiaoaiVoiceSuppressed as exc:
    attempts.append(NotificationAttempt(
        channel=channel,
        success=False,
        error_type=exc.__class__.__name__,
        error=str(exc),
        suppressed=True,
    ))
```

`build_notifier()` must pass `host=config.xiaoai_host` and
`ssh_key=config.xiaoai_ssh_key` only after both values have been validated.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `.venv/bin/python -m pytest tests/test_daily_premarket.py -q`

Expected: all daily premarket tests pass.

### Task 3: Watcher and CLI Semantics

**Files:**
- Modify: `tests/test_a_share_trend_watch.py`
- Modify: `tests/test_premarket_cli.py`
- Modify: `src/open_trader/a_share_trend_watch.py`
- Modify: `src/open_trader/cli.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: channel `xiaoai`, `XiaoaiSSHNotifier`, `xiaoai_voice_allowed()`, and `NotificationAttempt.suppressed`.
- Produces: event suffixes `queued_xiaoai`, `failed_xiaoai`, and `suppressed_quiet_hours_xiaoai`.

- [ ] **Step 1: Rename test doubles and write the post-lock suppression regression**

Update watcher and CLI expectations from `xiaozhi` to `xiaoai`. Add a notifier double that raises `XiaoaiVoiceSuppressed` and assert the watcher writes `suppressed_quiet_hours_xiaoai`, does not write `failed_xiaoai`, and sends no Feishu failure warning.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_a_share_trend_watch.py tests/test_premarket_cli.py -q`

Expected: renamed imports/events and suppression handling fail.

- [ ] **Step 3: Implement the routing rename and suppression branch**

Rename the type checks/helpers/channel filters and event suffixes. Treat `attempt.suppressed` before success/failure logic. Make the CLI's failure list exclude suppressed attempts and retain the existing Chinese quiet-hours output. Record the migration in the changelog.

```python
if attempt.suppressed:
    event_type = "protection_triggered_notification_suppressed_quiet_hours_xiaoai"
    reason = ""
elif attempt.success:
    event_type = "protection_triggered_notification_queued_xiaoai"
    reason = ""
else:
    event_type = "protection_triggered_notification_failed_xiaoai"
    reason = _voice_failure_reason(attempt.error)
```

The CLI failure filter becomes:

```python
failed_attempts = [
    attempt for attempt in attempts
    if not attempt.success and not attempt.suppressed
]
```

- [ ] **Step 4: Run focused tests and type compilation**

Run: `.venv/bin/python -m pytest tests/test_notifications.py tests/test_daily_premarket.py tests/test_a_share_trend_watch.py tests/test_premarket_cli.py -q`

Run: `.venv/bin/python -m compileall -q src`

Expected: both commands exit 0.

### Task 4: Live Configuration, Verification, and Cutover

**Files:**
- Modify outside Git: `config/daily_premarket.env`
- Create outside Git: `~/.ssh/open_trader_xiaoai` and `.pub`
- Modify reversibly on speaker: `/data/init.sh`

**Interfaces:**
- Consumes: `open-trader test-notification --config config/daily_premarket.env`.
- Produces: a live native-TTS deployment with no old voice runtime.

- [ ] **Step 1: Install dedicated SSH access**

Generate an Ed25519 key without overwriting an existing key. Because this Dropbear firmware may reject Ed25519, fall back to a dedicated RSA key only if the live authentication check fails. Install the public key using the documented initial speaker login, then verify a non-interactive `true` command with host-key checking enabled.

- [ ] **Step 2: Update live configuration and run a direct manual notification**

Replace the live notifier entry/keys with `xiaoai`, `192.168.1.107`, and the dedicated key path. Run the CLI during allowed hours and verify audible native speech plus exit code 0. Inspect the command's fresh timestamp/log output.

- [ ] **Step 3: Run full automated and acceptance gates on a committed SHA**

Run: `.venv/bin/python -m pytest -q`

Run: `git diff --check`

Commit only task files, then run: `make acceptance`

Expected: full tests pass, diff check exits 0, and acceptance prints `PASS`.

- [ ] **Step 4: Review the accepted diff**

Invoke `/code-review` against the pre-implementation commit and fix every actionable finding. Any source fix requires a new commit and another `make acceptance` run.

- [ ] **Step 5: Stop old runtimes after the new path passes**

Stop `open-xiaoai-xiaozhi`, quit the `xiaozhi-server` screen, stop the device client, and move `/data/init.sh` to a clearly named disabled backup. Verify Docker, screen, local ports, and the remote process list show no old voice runtime.

- [ ] **Step 6: Redeploy the exact accepted SHA**

Restart the Open Trader dashboard/watch processes from the accepted checkout. Verify PID, working directory, Git SHA, fresh logs, and HTTP 200 from the review URL. Do not rerun acceptance unless source or data changed.
