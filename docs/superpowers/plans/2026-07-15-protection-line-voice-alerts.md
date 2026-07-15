# Protection-Line Voice Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Speak one non-interrupting Xiaozhi alert when an A-share, Hong Kong, or US holding first touches its active protection line.

**Architecture:** Extend the existing Xiaozhi speak API with a non-interrupting queue marker and a `not_after` deadline. Narrow Open Trader voice output to protection triggers plus manual tests, then reuse `watch_a_share_protection()` for routing, deduplication, and Feishu-only failure warnings.

**Tech Stack:** Python 3.12, stdlib datetime/zoneinfo/queue, aiohttp, Xiaozhi `TTSMessageDTO`, Open Trader notifier protocol, JSONL watch events, pytest, launchd, screen, and `make acceptance`.

## Global Constraints

- Automatic voice is allowlisted to protection-line triggers only; keep manual test voice.
- 做 T, premarket, trend-report, trend-signal, and unknown notifications do not speak.
- Voice hours are `08:00:00 <= Asia/Shanghai time < 23:00:00`.
- Quiet suppression, failed submission, and process restart never retry or replay voice.
- Submit at most once per symbol per market trading date.
- Queue without interrupting current TTS. There is no ordinary wait timeout.
- Drop a queued item only when playback has not begun by its 23:00 `not_after`.
- HTTP `code=0` is success; physical playback acknowledgement is out of scope.
- Voice failure sends one Feishu-only warning; Feishu failure never recurses.
- Add no dependency or background process. Never expose or commit tokens.
- Preserve unrelated dirty work in both repositories. Stage only feature hunks.
- After each Open Trader source slice, run focused tests and `make acceptance`; only `PASS` permits continuation.
- After final `PASS`, redeploy the exact accepted SHA and verify PID, cwd, SHA, fresh logs, and HTTP 200.

## File Map

Xiaozhi repository `/Users/ray/projects/xiaozhi-esp32-server`:

- `main/xiaozhi-server/core/providers/tts/dto/dto.py`: queue metadata.
- `main/xiaozhi-server/core/api/speak_handler.py`: API validation and queue submission.
- `main/xiaozhi-server/core/providers/tts/base.py`: non-interruption and expiry.
- `main/xiaozhi-server/app.py`: secret environment override.
- `main/xiaozhi-server/tests/test_speak_handler.py`: API contract.
- `main/xiaozhi-server/tests/test_notification_queue.py`: queue decisions.
- `main/xiaozhi-server/tests/test_notify_api_config.py`: environment override.

Open Trader repository `/Users/ray/projects/open_trader`:

- `src/open_trader/notifications.py`: allowlist, template, hours, payload.
- `src/open_trader/a_share_trend_watch.py`: watcher routing and event outcomes.
- `src/open_trader/cli.py`: manual-test quiet output.
- `tests/test_notifications.py`: voice policy.
- `tests/test_a_share_trend_watch.py`: shared watcher delivery.
- `tests/test_market_trend_watch.py`: HK/US coverage.
- `tests/test_premarket_cli.py`: manual-test output.

---

### Task 1: Add the Xiaozhi External-Notification API Contract

**Files:**

- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/providers/tts/dto/dto.py`
- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/api/speak_handler.py`
- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/app.py`
- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_speak_handler.py`
- Create: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_notify_api_config.py`

**Interfaces:**

- Consumes: existing `POST /xiaozhi/notify/speak`, registry, and TTS queue.
- Produces: `TTSMessageDTO.external_notification`, `TTSMessageDTO.not_after`, and `apply_notify_api_token_env()`.

- [ ] **Step 1: Write failing speak-contract tests**

Replace the existing successful queue test with this behavior and add the invalid deadline case:

```python
async def test_queues_non_interrupting_message_with_deadline(self):
    conn = _Conn()
    conn.sentence_id = "active-conversation"
    conn.client_abort = True
    register_connection("speaker-1", conn)
    handler = SpeakHandler({"server": {"notify_api_token": "voice-token"}})

    response = await handler.handle_post(_Request({
        "device_id": "speaker-1",
        "title": "A股保护线触发 · 600000",
        "message": "Open Trader 紧急提醒：A股浦发银行。",
        "not_after": "2026-07-15T23:00:00+08:00",
    }))

    self.assertEqual(response.status, 200)
    self.assertEqual(conn.sentence_id, "active-conversation")
    self.assertTrue(conn.client_abort)
    sentence_id, stored_text = conn.stored[0]
    self.assertEqual(stored_text, "Open Trader 紧急提醒：A股浦发银行。")
    queued = [conn.tts.tts_text_queue.get_nowait() for _ in range(3)]
    self.assertTrue(all(item.external_notification for item in queued))
    self.assertTrue(all(item.not_after == 1784127600.0 for item in queued))
    self.assertTrue(all(item.sentence_id == sentence_id for item in queued))

async def test_rejects_naive_not_after(self):
    register_connection("speaker-1", _Conn())
    handler = SpeakHandler({"server": {"notify_api_token": "voice-token"}})
    response = await handler.handle_post(_Request({
        "device_id": "speaker-1",
        "title": "t",
        "message": "m",
        "not_after": "2026-07-15T23:00:00",
    }))
    self.assertEqual(response.status, 400)
    self.assertEqual(json.loads(response.text)["message"], "bad_request")
```

Add a timezone-aware `not_after` to other valid request fixtures so they still isolate offline, not-ready, and blank-message behavior.

- [ ] **Step 2: Write failing environment override tests**

Create `tests/test_notify_api_config.py`:

```python
import unittest
from app import apply_notify_api_token_env

class NotifyApiConfigTest(unittest.TestCase):
    def test_environment_token_overrides_a_copy(self):
        original = {"server": {"notify_api_token": ""}}
        configured = apply_notify_api_token_env(
            original, {"XIAOZHI_NOTIFY_API_TOKEN": "secret-token"}
        )
        self.assertEqual(configured["server"]["notify_api_token"], "secret-token")
        self.assertEqual(original["server"]["notify_api_token"], "")

    def test_missing_environment_keeps_configured_value(self):
        original = {"server": {"notify_api_token": "configured-token"}}
        configured = apply_notify_api_token_env(original, {})
        self.assertEqual(configured["server"]["notify_api_token"], "configured-token")
```

- [ ] **Step 3: Verify the red state**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest tests/test_speak_handler.py tests/test_notify_api_config.py -q
```

Expected: fail for missing DTO fields, deadline validation, title-free text, non-interruption, and environment helper.

- [ ] **Step 4: Extend the DTO compatibly**

Append to `TTSMessageDTO.__init__`:

```python
external_notification: bool = False,
not_after: Optional[float] = None,
```

Assign both attributes. Defaults preserve every existing caller.

- [ ] **Step 5: Validate and queue message-only speech**

In `speak_handler.py` add:

```python
from datetime import datetime

def parse_not_after(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("not_after is required")
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("not_after must include a timezone")
    return parsed.timestamp()
```

Parse it inside the bad-request boundary. Normalize only `message`; keep `title` as required metadata but do not prepend it. Do not mutate `conn.sentence_id` or `conn.client_abort`. Queue:

```python
for sentence_type, content_type, detail in (
    (SentenceType.FIRST, ContentType.ACTION, None),
    (SentenceType.MIDDLE, ContentType.TEXT, voice_text),
    (SentenceType.LAST, ContentType.ACTION, None),
):
    tts_text_queue.put(TTSMessageDTO(
        sentence_id=sentence_id,
        sentence_type=sentence_type,
        content_type=content_type,
        content_detail=detail,
        external_notification=True,
        not_after=not_after,
    ))
```

Delete the now-unused `build_voice_text()` helper and update its unit test to assert the handler's stored message-only text. Confirm with `rg -n "build_voice_text"` that no production caller remains.

- [ ] **Step 6: Add the secret-free environment override**

In `app.py` import `os` and add:

```python
def apply_notify_api_token_env(config, environ=os.environ):
    server = dict(config.get("server") or {})
    token = str(environ.get("XIAOZHI_NOTIFY_API_TOKEN") or "").strip()
    if token:
        server["notify_api_token"] = token
    return {**config, "server": server}
```

Call it immediately after `config = load_config()`. Do not put a real token in `config.yaml`.

- [ ] **Step 7: Verify and commit only feature hunks**

Run the Step 3 tests again; expect PASS. Then inspect/stage only feature hunks because `app.py` is already dirty:

```bash
git -C /Users/ray/projects/xiaozhi-esp32-server add -p -- \
  main/xiaozhi-server/app.py \
  main/xiaozhi-server/core/api/speak_handler.py \
  main/xiaozhi-server/core/providers/tts/dto/dto.py \
  main/xiaozhi-server/tests/test_speak_handler.py
git -C /Users/ray/projects/xiaozhi-esp32-server add \
  main/xiaozhi-server/tests/test_notify_api_config.py
git -C /Users/ray/projects/xiaozhi-esp32-server diff --cached --check
git -C /Users/ray/projects/xiaozhi-esp32-server diff --cached
git -C /Users/ray/projects/xiaozhi-esp32-server commit -m \
  "feat: queue external voice notifications"
```

The cached diff must exclude pre-existing story, model, audio-close, and config changes.

### Task 2: Preserve FIFO Playback and Enforce `not_after`

**Files:**

- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/providers/tts/base.py`
- Create: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_notification_queue.py`

**Interfaces:**

- Consumes: Task 1 DTO metadata.
- Produces: `_should_process_text_message()`, `_should_send_audio()`, and terminal queue cleanup.

- [ ] **Step 1: Write failing queue-decision tests**

Create a concrete test provider and assert these five decisions:

```python
class _Provider(TTSProviderBase):
    async def text_to_speak(self, text, output_file):
        return b""

class NotificationQueueTest(unittest.TestCase):
    def setUp(self):
        self.provider = _Provider({}, delete_audio_file=True)
        self.provider.conn = SimpleNamespace(
            client_abort=True, sentence_id="active-conversation"
        )

    def message(self, deadline, external=True):
        return TTSMessageDTO(
            "notification-1", SentenceType.FIRST, ContentType.ACTION,
            external_notification=external, not_after=deadline,
        )

    def test_external_bypasses_conversation_filters(self):
        self.assertTrue(self.provider._should_process_text_message(
            self.message(101), now=100
        ))

    def test_ordinary_keeps_conversation_filters(self):
        self.assertFalse(self.provider._should_process_text_message(
            self.message(None, external=False), now=100
        ))

    def test_text_expires_at_deadline(self):
        self.assertFalse(self.provider._should_process_text_message(
            self.message(100), now=100
        ))

    def test_audio_expires_before_first_packet(self):
        self.provider._external_notification_deadlines["notification-1"] = 100
        self.assertFalse(self.provider._should_send_audio(
            SentenceType.FIRST, "notification-1", now=100
        ))

    def test_started_audio_finishes_after_deadline(self):
        self.provider._external_notification_deadlines["notification-1"] = 100
        self.assertTrue(self.provider._should_send_audio(
            SentenceType.FIRST, "notification-1", now=99
        ))
        self.assertTrue(self.provider._should_send_audio(
            SentenceType.MIDDLE, "notification-1", now=101
        ))
```

- [ ] **Step 2: Verify the red state**

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest tests/test_notification_queue.py -q
```

Expected: missing state and decision methods.

- [ ] **Step 3: Add minimal deadline state and decisions**

In `TTSProviderBase.__init__` add four sets/maps:

```python
self._external_text_started = set()
self._external_audio_started = set()
self._external_notification_deadlines = {}
self._expired_external_notifications = set()
```

Import `time`. Implement:

```python
def _should_process_text_message(self, message, now=None):
    if not getattr(message, "external_notification", False):
        return not self.conn.client_abort and message.sentence_id == self.conn.sentence_id
    current = time.time() if now is None else now
    sentence_id = message.sentence_id
    if message.sentence_type == SentenceType.FIRST:
        self._external_notification_deadlines[sentence_id] = message.not_after
        if message.not_after is not None and current >= message.not_after:
            self._expired_external_notifications.add(sentence_id)
            logger.bind(tag=TAG).info("expired_quiet_hours sentence_id={}", sentence_id)
            return False
        self._external_text_started.add(sentence_id)
    return sentence_id in self._external_text_started and sentence_id not in self._expired_external_notifications

def _should_send_audio(self, sentence_type, sentence_id, now=None):
    deadline = self._external_notification_deadlines.get(sentence_id)
    if deadline is None:
        return not self.conn.client_abort
    current = time.time() if now is None else now
    if sentence_id in self._expired_external_notifications:
        return False
    if sentence_type == SentenceType.FIRST:
        if current >= deadline:
            self._expired_external_notifications.add(sentence_id)
            logger.bind(tag=TAG).info("expired_quiet_hours sentence_id={}", sentence_id)
            return False
        self._external_audio_started.add(sentence_id)
    return sentence_id in self._external_audio_started

def _finish_external_notification(self, sentence_id):
    self._external_text_started.discard(sentence_id)
    self._external_audio_started.discard(sentence_id)
    self._expired_external_notifications.discard(sentence_id)
    self._external_notification_deadlines.pop(sentence_id, None)
```

- [ ] **Step 4: Use the decisions in both worker threads**

In `tts_text_priority_thread`, replace current abort/id filters with `_should_process_text_message(message)`. When an expired external LAST is consumed, enqueue an audio LAST sentinel so audio cleanup still runs. After a processed external LAST, discard `_external_text_started` only.

In `_audio_play_priority_thread`, call `_should_send_audio(sentence_type, sentence_id)` before sending. Skip expired items; on every external LAST call `_finish_external_notification(sentence_id)`. Keep ordinary message behavior unchanged.

- [ ] **Step 5: Verify regression coverage**

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest \
  tests/test_notification_queue.py \
  tests/test_speak_handler.py \
  tests/test_story_playback.py \
  tests/test_ping_message_handler.py -q
```

Expected: PASS, including normal story playback and abort behavior.

- [ ] **Step 6: Commit only feature hunks**

`base.py` already contains an unrelated audio-close edit. Use `git add -p`, inspect `git diff --cached`, and exclude that hunk:

```bash
git -C /Users/ray/projects/xiaozhi-esp32-server add -p -- \
  main/xiaozhi-server/core/providers/tts/base.py
git -C /Users/ray/projects/xiaozhi-esp32-server add \
  main/xiaozhi-server/tests/test_notification_queue.py
git -C /Users/ray/projects/xiaozhi-esp32-server diff --cached --check
git -C /Users/ray/projects/xiaozhi-esp32-server diff --cached
git -C /Users/ray/projects/xiaozhi-esp32-server commit -m \
  "feat: preserve queued voice notification order"
```

### Task 3: Narrow Open Trader Voice Policy and Add Quiet-Hour Payloads

**Files:**

- Modify: `src/open_trader/notifications.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_notifications.py`
- Modify: `tests/test_premarket_cli.py`

**Interfaces:**

- Consumes: Task 1's required ISO `not_after`.
- Produces: `xiaozhi_voice_allowed(now)`, `xiaozhi_not_after(now)`, and protection-only rendering.

- [ ] **Step 1: Replace obsolete template expectations**

Replace daily/做 T/fallback voice assertions with protection cases:

```python
@pytest.mark.parametrize(("title", "message", "expected"), [
    (
        "A股保护线触发 · 600000",
        "名称：浦发银行\n最新价 9.98 <= 活动保护线 10.01",
        "Open Trader 紧急提醒：A股浦发银行，代码600000，最新价9.98，已触及活动保护线10.01。建议全部卖出，请查看飞书确认并人工执行。",
    ),
    (
        "港股保护线触发 · 00700",
        "名称：腾讯控股\n最新价 399.8 <= 活动保护线 400",
        "Open Trader 紧急提醒：港股腾讯控股，代码00700，最新价399.8，已触及活动保护线400。建议全部卖出，请查看飞书确认并人工执行。",
    ),
    (
        "美股保护线触发 · NVDA",
        "名称：\n最新价 150.25 <= 活动保护线 151.00",
        "Open Trader 紧急提醒：美股代码NVDA，最新价150.25，已触及活动保护线151.00。建议全部卖出，请查看飞书确认并人工执行。",
    ),
])
def test_render_xiaozhi_protection_template(title, message, expected):
    assert render_xiaozhi_voice_notification(title, message) == expected

@pytest.mark.parametrize("title", [
    "Open Trader 美股开始通知",
    "Open Trader 港股阻塞通知",
    "Open Trader 美股行动通知",
    "Open Trader 港股完成通知",
    "Open Trader｜做T提醒｜US ARM｜买入做T",
    "A股趋势操作计划 · 2026-07-15",
    "Open Trader 其他通知",
])
def test_render_xiaozhi_skips_non_protection_business_events(title):
    assert render_xiaozhi_voice_notification(title, "正文") is None
```

Keep the explicit manual test-notification assertion.

- [ ] **Step 2: Add hours and payload tests**

```python
@pytest.mark.parametrize(("value", "allowed"), [
    ("2026-07-15T07:59:59+08:00", False),
    ("2026-07-15T08:00:00+08:00", True),
    ("2026-07-15T22:59:59+08:00", True),
    ("2026-07-15T23:00:00+08:00", False),
])
def test_xiaozhi_voice_hours(value, allowed):
    assert xiaozhi_voice_allowed(datetime.fromisoformat(value)) is allowed

def test_xiaozhi_notifier_adds_not_after():
    calls = []
    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=lambda url, payload, headers, timeout: calls.append(payload) or {"code": 0},
        now_fn=lambda: datetime.fromisoformat("2026-07-15T22:59:59+08:00"),
    )
    notifier.notify(
        "A股保护线触发 · 600000",
        "名称：浦发银行\n最新价 9.98 <= 活动保护线 10.01",
    )
    assert calls[0]["not_after"] == "2026-07-15T23:00:00+08:00"

def test_xiaozhi_notifier_skips_quiet_hours():
    calls = []
    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=lambda *args: calls.append(args) or {"code": 0},
        now_fn=lambda: datetime.fromisoformat("2026-07-15T23:00:00+08:00"),
    )
    notifier.notify("Open Trader 测试通知", "测试")
    assert calls == []
```

- [ ] **Step 3: Verify the red state**

```bash
.venv/bin/python -m pytest tests/test_notifications.py -q
```

Expected: protection alerts use the generic fallback, old event types still speak, and hours/deadline APIs are missing.

- [ ] **Step 4: Implement the allowlist and template**

Import `time` from `datetime` and `ZoneInfo`, define `SHANGHAI = ZoneInfo("Asia/Shanghai")`, then add:

```python
def xiaozhi_voice_allowed(now: datetime) -> bool:
    local = now.astimezone(SHANGHAI).time()
    return time(8) <= local < time(23)

def xiaozhi_not_after(now: datetime) -> str:
    local = now.astimezone(SHANGHAI)
    return local.replace(hour=23, minute=0, second=0, microsecond=0).isoformat()
```

Replace `render_xiaozhi_voice_notification`:

```python
def render_xiaozhi_voice_notification(title: str, message: str) -> str | None:
    title, message = title.strip(), message.strip()
    if "测试通知" in title:
        return message or title
    match = re.fullmatch(r"(A股|港股|美股)保护线触发 · ([^·]+)", title)
    if match is None:
        return None
    prices = re.search(
        r"最新价\s+([^\s]+)\s*<=\s*活动保护线\s+([^\s]+)", message
    )
    if prices is None:
        raise NotificationError("Xiaozhi protection voice fields missing")
    market, symbol = match.groups()
    name = _voice_field(message, "名称")
    subject = f"{market}{name}，代码{symbol.strip()}" if name else f"{market}代码{symbol.strip()}"
    last_price, active_line = prices.groups()
    return (
        f"Open Trader 紧急提醒：{subject}，最新价{last_price}，"
        f"已触及活动保护线{active_line}。建议全部卖出，"
        "请查看飞书确认并人工执行。"
    )
```

Use `rg` to confirm obsolete daily/做 T helper functions have no callers, then delete them instead of retaining dead compatibility code.

- [ ] **Step 5: Gate and timestamp the notifier**

Add `now_fn: Callable[[], datetime] = lambda: datetime.now(SHANGHAI)` to `XiaozhiVoiceNotifier.__init__`. In `notify`:

```python
now = self._now_fn()
if not xiaozhi_voice_allowed(now):
    return
payload = {
    "device_id": self.device_id,
    "title": title,
    "message": voice_message,
    "not_after": xiaozhi_not_after(now),
}
```

- [ ] **Step 6: Report manual-test suppression explicitly**

Add a CLI test whose config has `notifiers=("feishu_app", "xiaozhi")`, monkeypatches `cli.xiaozhi_voice_allowed` to `False`, and asserts `语音已跳过：静默时段`.

In the command, compute:

```python
voice_suppressed = (
    "xiaozhi" in getattr(config, "notifiers", ())
    and not xiaozhi_voice_allowed(datetime.now(SHANGHAI))
)
```

Print `通知测试已发送；语音已跳过：静默时段。` when true; retain the existing success text otherwise.

- [ ] **Step 7: Verify, commit, restart, and accept**

```bash
.venv/bin/python -m pytest tests/test_notifications.py tests/test_premarket_cli.py -q
git add src/open_trader/notifications.py src/open_trader/cli.py \
  tests/test_notifications.py tests/test_premarket_cli.py
git diff --cached --check
git commit -m "feat: limit voice to protection alerts"
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
make acceptance
```

Expected: focused tests pass and acceptance ends with JSON status `PASS`. Fix `FAIL`; report `BLOCKED` without substitutes.

### Task 4: Route Shared Protection Triggers to Xiaozhi Once

**Files:**

- Modify: `src/open_trader/a_share_trend_watch.py`
- Modify: `tests/test_a_share_trend_watch.py`
- Modify: `tests/test_market_trend_watch.py`

**Interfaces:**

- Consumes: Task 3 hours helper and `send_notification_with_results()`.
- Produces: terminal event types ending in `queued_xiaozhi`, `failed_xiaozhi`, or `suppressed_quiet_hours_xiaozhi`.

- [ ] **Step 1: Add a channel-recognizable recording double**

```python
class RecordingXiaozhiNotifier(XiaozhiVoiceNotifier):
    def __init__(self, fail=False):
        self.messages, self.fail, self.attempt_count = [], fail, 0

    def notify(self, title, message):
        self.attempt_count += 1
        if self.fail:
            raise RuntimeError("device_offline")
        self.messages.append((title, message))
```

- [ ] **Step 2: Write failing success, failure, and restart tests**

```python
def test_trigger_queues_one_voice_alert_with_name(tmp_path):
    voice = RecordingXiaozhiNotifier()
    events = tmp_path / "events.jsonl"
    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        events_path=events,
        notifier=CompositeNotifier([RecordingNotifier(), voice]),
    )
    assert voice.messages == [(
        "A股保护线触发 · 600900",
        "名称：长江电力\n最新价 27.30 <= 活动保护线 27.31\n建议动作：全部卖出（人工执行）",
    )]
    assert read_events(events)[-1]["event_type"].endswith("queued_xiaozhi")

def test_voice_failure_is_terminal_and_warns_feishu(tmp_path):
    voice, feishu = RecordingXiaozhiNotifier(fail=True), RecordingNotifier()
    events = tmp_path / "events.jsonl"
    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        events_path=events,
        notifier=CompositeNotifier([feishu, voice]),
    )
    assert voice.attempt_count == 1
    assert sum("语音播报失败" in title for title, _ in feishu.messages) == 1
    assert read_events(events)[-1]["reason"] == "设备离线"

def test_restart_never_replays_voice(tmp_path):
    events = tmp_path / "events.jsonl"
    first = RecordingXiaozhiNotifier(fail=True)
    run_once(tmp_path, quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
             events_path=events, notifier=CompositeNotifier([RecordingNotifier(), first]))
    restarted = RecordingXiaozhiNotifier()
    run_once(tmp_path, quote=SequenceQuote([{"SH.600900": Decimal("27.20")}]),
             events_path=events, notifier=CompositeNotifier([RecordingNotifier(), restarted]))
    assert first.attempt_count == 1
    assert restarted.attempt_count == 0
```

Update the existing two-poll `test_watcher_alerts_once_per_symbol_per_day` to include `RecordingXiaozhiNotifier` in its composite and assert `voice.attempt_count == 1`. Add a second run whose clock/trading calendar is `2026-07-16`; assert a fresh voice notifier is attempted once on that new trading date.

Add the terminal Feishu-failure case:

```python
def test_voice_and_feishu_failure_never_recurse(tmp_path):
    voice = RecordingXiaozhiNotifier(fail=True)
    feishu = FlakyNotifier(failures=10)
    run_once(
        tmp_path,
        quote=SequenceQuote([{"SH.600900": Decimal("27.30")}]),
        notifier=CompositeNotifier([feishu, voice]),
    )
    assert voice.attempt_count == 1
    assert feishu.attempt_count == 2  # original protection alert + one terminal warning
```

Also directly test `_deliver_trigger_notification` at `23:00+08:00`: no voice call, a `suppressed_quiet_hours_xiaozhi` event, and no voice-failure Feishu warning.

```python
def test_trigger_quiet_hours_suppresses_voice_without_failure_warning(tmp_path):
    voice, feishu = RecordingXiaozhiNotifier(), RecordingNotifier()
    events = tmp_path / "events.jsonl"
    _deliver_trigger_notification(
        events_path=events,
        notifier=CompositeNotifier([feishu, voice]),
        trading_date="2026-07-15",
        now=datetime.fromisoformat("2026-07-15T23:00:00+08:00"),
        symbol="600900",
        position_name="长江电力",
        last_price=Decimal("27.30"),
        active_line=Decimal("27.31"),
        delivered_feishu=set(),
        delivered_macos=set(),
        replay=False,
    )
    assert voice.messages == []
    assert not any("语音播报失败" in title for title, _ in feishu.messages)
    assert read_events(events)[-1]["event_type"].endswith(
        "suppressed_quiet_hours_xiaozhi"
    )
```

- [ ] **Step 3: Verify the red state**

```bash
.venv/bin/python -m pytest tests/test_a_share_trend_watch.py -q
```

Expected: Xiaozhi receives nothing and terminal voice events are absent.

- [ ] **Step 4: Extend event metadata without changing existing shapes**

Add optional `market: str = ""` and `reason: str = ""` parameters to `append_watch_event`; only insert those keys when non-empty. Existing exact-key tests remain unchanged.

```python
if market:
    event["market"] = market
if reason:
    event["reason"] = reason
```

- [ ] **Step 5: Add safe failure categories**

```python
def _voice_failure_reason(error: str) -> str:
    if "device_offline" in error:
        return "设备离线"
    if "tts_not_ready" in error:
        return "播放队列未就绪"
    if "unauthorized" in error:
        return "语音服务鉴权失败"
    if "notify_api_disabled" in error:
        return "语音服务未启用"
    return "语音服务不可用"
```

Add a local presence check so an installation without a Xiaozhi notifier produces no voice outcome at all:

```python
def _has_xiaozhi_notifier(notifier: Notifier) -> bool:
    targets = notifier._notifiers if isinstance(notifier, CompositeNotifier) else [notifier]
    return any(isinstance(target, XiaozhiVoiceNotifier) for target in targets)
```

- [ ] **Step 6: Append one voice attempt after existing channels**

Extend `_deliver_trigger_notification` with `position_name`. Preserve its Feishu/macOS loop. After it, return immediately when `replay=True` or `_has_xiaozhi_notifier(notifier)` is false; otherwise:

```python
voice_message = "\n".join([
    f"名称：{position_name}",
    f"最新价 {last_price} <= 活动保护线 {active_line}",
    "建议动作：全部卖出（人工执行）",
])
if not xiaozhi_voice_allowed(now):
    append_watch_event(
        events_path, symbol=symbol, trading_date=trading_date,
        event_type="protection_triggered_notification_suppressed_quiet_hours_xiaozhi",
        occurred_at=now.isoformat(timespec="seconds"), last_price=last_price,
        active_line=active_line, market=market_label,
    )
    return
attempts = send_notification_with_results(
    notifier, f"{market_label}保护线触发 · {symbol}", voice_message,
    channels={"xiaozhi"},
)
if not attempts:
    return
attempt = attempts[0]
reason = "" if attempt.success else _voice_failure_reason(attempt.error)
append_watch_event(
    events_path, symbol=symbol, trading_date=trading_date,
    event_type=(
        "protection_triggered_notification_queued_xiaozhi"
        if attempt.success else "protection_triggered_notification_failed_xiaozhi"
    ),
    occurred_at=now.isoformat(timespec="seconds"), last_price=last_price,
    active_line=active_line, market=market_label, reason=reason,
)
if not attempt.success:
    send_notification_with_results(
        notifier,
        "Open Trader 语音播报失败",
        "\n".join([
            f"市场：{market_label}",
            f"标的：{position_name or symbol}（{symbol}）",
            "原事件：活动保护线触发",
            f"失败原因：{reason}",
            "处理：语音不重试，请按原保护线通知人工确认。",
        ]),
        channels={"feishu", "feishu_app"},
    )
```

Pass `position_name=str(getattr(positions[symbol], "name", ""))` from both call sites. Replay never attempts voice, even if a crash occurred before outcome persistence.

- [ ] **Step 7: Prove HK and US share the same behavior**

In `tests/test_market_trend_watch.py`, define the same local `RecordingXiaozhiNotifier` subclass so the test module has no cross-test import. Replace the HK `NullNotifier` with a composite containing it, then assert:

```python
assert voice.messages == [(
    "港股保护线触发 · 00700",
    "名称：腾讯\n最新价 10 <= 活动保护线 11\n建议动作：全部卖出（人工执行）",
)]
```

Add `_write_us_details()` beside `_write_hk_details()` using broker `futu`, market `US`, symbol/name `NVDA/NVIDIA`, currency `USD`, positive quantity/value, and a `2026-07-15` statement id. Run `watch_market_protection(market="US", ...)` at `2026-07-15T22:00:00+08:00` with price below its saved active line and assert:

```python
assert voice.messages[0][0] == "美股保护线触发 · NVDA"
assert voice.messages[0][1].startswith("名称：NVIDIA\n最新价 ")
```

- [ ] **Step 8: Verify, commit, and accept**

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py \
  tests/test_notifications.py -q
.venv/bin/python -m pytest -q
git add src/open_trader/a_share_trend_watch.py \
  tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py
git diff --cached --check
git commit -m "feat: speak protection line triggers"
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
make acceptance
```

Expected: all tests pass and acceptance ends `PASS`.

### Task 5: Live Verification and Exact-SHA Deployment

**Files/Processes:**

- Configure without committing: Open Trader env and Xiaozhi process environment.
- Restart: Xiaozhi, CN/HK/US trend watchers, installed launchd definitions, and Dashboard.
- Verify: PIDs, cwd, SHAs, fresh logs, real speaker, and review URL.

**Interfaces:**

- Consumes: committed Tasks 1-4.
- Produces: real audio evidence and an accepted/redeployed Open Trader SHA.

- [ ] **Step 1: Audit both worktrees before deployment**

```bash
cd /Users/ray/projects/open_trader
git status --short
git log -3 --oneline
git -C /Users/ray/projects/xiaozhi-esp32-server status --short
git -C /Users/ray/projects/xiaozhi-esp32-server log -4 --oneline
```

Expected: Open Trader shows only the user's pre-existing unrelated untracked files. Xiaozhi may remain dirty only for pre-existing user changes; feature hunks are committed.

- [ ] **Step 2: Run both repositories' automated verification**

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest \
  tests/test_speak_handler.py tests/test_notification_queue.py \
  tests/test_notify_api_config.py tests/test_story_playback.py \
  tests/test_ping_message_handler.py -q
cd /Users/ray/projects/open_trader
.venv/bin/python -m pytest -q
```

Expected: both commands pass. Record exact pass counts.

- [ ] **Step 3: Start fresh Xiaozhi code without exposing the token**

Read the token inside the screen shell so it never appears in argv, tool output, or Git:

```bash
screen -S xiaozhi-server -X quit 2>/dev/null || true
rm -f /tmp/xiaozhi-server.log
screen -dmS xiaozhi-server zsh -lc '
  cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
  export XIAOZHI_NOTIFY_API_TOKEN="$(awk -F= '\''$1=="OPEN_TRADER_XIAOZHI_TOKEN" {print substr($0,index($0,"=")+1)}'\'' /Users/ray/projects/open_trader/config/daily_premarket.env)"
  exec .venv/bin/python -u app.py >> /tmp/xiaozhi-server.log 2>&1
'
```

Verify without printing the process environment:

```bash
screen -ls | rg 'xiaozhi-server'
lsof -nP -iTCP:8003 -sTCP:LISTEN
ps -axo pid,lstart,command | rg '[p]ython.*app.py'
tail -n 80 /tmp/xiaozhi-server.log
```

Expected: one fresh PID listens on 8003 and logs contain no traceback.

- [ ] **Step 4: Run the real manual notification during allowed hours**

```bash
TZ=Asia/Shanghai date '+%Y-%m-%dT%H:%M:%S%z'
cd /Users/ray/projects/open_trader
.venv/bin/python -m open_trader test-notification \
  --config config/daily_premarket.env
```

Expected: exit 0, Feishu succeeds, Xiaozhi returns queued, and the physical speaker speaks once. Outside 08:00-23:00 or with an offline device, report live verification as blocked; do not substitute curl or mocks.

- [ ] **Step 5: Repair and reinstall all launchd definitions**

The current premarket jobs reference the removed `.worktrees/dashboard-source-completeness`. Regenerate every relevant job from current config:

```bash
cd /Users/ray/projects/open_trader
./scripts/install_daily_premarket_launchd.sh --market all
./scripts/install_daily_premarket_launchd.sh --trend-only --market all
```

Verify definitions:

```bash
for label in \
  com.open-trader.premarket.hk \
  com.open-trader.premarket.us \
  com.open-trader.trend-a-share-watch \
  com.open-trader.trend-hk-watch \
  com.open-trader.trend-us-watch
do
  launchctl print "gui/$(id -u)/$label" | rg \
    'state =|program =|working directory =|last exit code =|pid ='
done
```

Expected: every program and cwd resolves under `/Users/ray/projects/open_trader`; no removed worktree remains.

- [ ] **Step 6: Restart watcher code and inspect fresh logs**

```bash
launchctl kickstart -k "gui/$(id -u)/com.open-trader.trend-a-share-watch"
launchctl kickstart -k "gui/$(id -u)/com.open-trader.trend-hk-watch"
launchctl kickstart -k "gui/$(id -u)/com.open-trader.trend-us-watch"
ps -axo pid,lstart,command | rg '[o]pen_trader watch-trend-(a-share|market)'
stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%S%z' \
  logs/daily_premarket/launchd-CN-watch.out.log \
  logs/daily_premarket/launchd-trend-HK-watch.out.log \
  logs/daily_premarket/launchd-trend-US-watch.out.log
tail -n 80 logs/daily_premarket/launchd-CN-watch.err.log
tail -n 80 logs/daily_premarket/launchd-trend-HK-watch.err.log
tail -n 80 logs/daily_premarket/launchd-trend-US-watch.err.log
```

Expected: no pre-change watcher PID remains. In-session jobs have fresh PIDs/logs. Out-of-session jobs may exit cleanly; report that honestly with loaded launchd state rather than claiming a persistent PID.

- [ ] **Step 7: Run the final acceptance gate**

```bash
cd /Users/ray/projects/open_trader
make acceptance
```

Expected: final JSON status `PASS`. Fix and rerun `FAIL`; stop and report `BLOCKED`.

- [ ] **Step 8: Redeploy the exact accepted SHA**

```bash
ACCEPTED_SHA="$(git rev-parse HEAD)"
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
rm -f /tmp/open_trader_dashboard_8766.log
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
PID="$(lsof -tiTCP:8766 -sTCP:LISTEN)"
test -n "$PID"
lsof -a -p "$PID" -d cwd -Fn
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
ps -p "$PID" -o pid=,lstart=,command=
stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%S%z' /tmp/open_trader_dashboard_8766.log
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8766
```

Expected: one new PID, cwd `/Users/ray/projects/open_trader`, exact accepted SHA, fresh traceback-free logs, and HTTP 200.

- [ ] **Step 9: Handoff evidence**

Report exact Xiaozhi/Open Trader test counts, real speaker result, Xiaozhi PID, watcher PIDs/states and fresh log times, accepted SHA, `make acceptance` PASS result, Dashboard PID/cwd/SHA, and `http://127.0.0.1:8766`.

Do not claim completion if physical audio, background processes, or acceptance were not verified.
