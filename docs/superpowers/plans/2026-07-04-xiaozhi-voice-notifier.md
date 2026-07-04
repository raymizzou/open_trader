# Xiaozhi Voice Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send every Open Trader notification that goes to Feishu to the already-connected Xiaoai voice path through Xiaozhi.

**Architecture:** Open Trader adds a `XiaozhiVoiceNotifier` that posts the existing notification title and message to Xiaozhi. Xiaozhi adds a local authenticated speak API, an online connection registry, and a queueing helper that reuses the existing per-device TTS queue and audio sender.

**Tech Stack:** Python 3, pytest, aiohttp, urllib stdlib, existing Open Trader notifier protocol, existing Xiaozhi `TTSMessageDTO` queue.

---

## Scope Check

This is a cross-repository feature with one end-to-end behavior. It is not split into separate specs because neither side is useful alone: Open Trader needs the Xiaozhi HTTP contract, and Xiaozhi needs Open Trader to call it. The implementation is still decomposed into independently testable tasks.

## File Structure

Open Trader files:

- Modify: `src/open_trader/notifications.py`
  - Owns `XiaozhiVoiceNotifier` and channel-specific HTTP delivery errors.
- Modify: `src/open_trader/daily_premarket.py`
  - Owns env parsing, notifier construction, and channel naming.
- Modify: `config/daily_premarket.env.example`
  - Documents the `xiaozhi` notifier env vars.
- Modify: `tests/test_notifications.py`
  - Tests Xiaozhi notifier payloads, auth headers, API failures, and transport failures.
- Modify: `tests/test_daily_premarket.py`
  - Tests config parsing and `build_notifier()` wiring.

Xiaozhi files:

- Create: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/connection_registry.py`
  - Owns online `device_id -> ConnectionHandler` lookup.
- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/connection.py`
  - Registers and unregisters online device connections.
- Create: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/api/speak_handler.py`
  - Owns request auth, validation, text cleanup, device lookup, and TTS queueing.
- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/http_server.py`
  - Wires `/xiaozhi/notify/speak`.
- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/config.yaml`
  - Adds documented empty `server.notify_api_token` config.
- Create: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_connection_registry.py`
  - Tests registry behavior.
- Create: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_speak_handler.py`
  - Tests speak API auth, validation, offline behavior, and queueing.

## Task 1: Add Open Trader Xiaozhi Notifier

**Files:**
- Modify: `tests/test_notifications.py`
- Modify: `src/open_trader/notifications.py`

- [ ] **Step 1: Write failing notifier tests**

In `tests/test_notifications.py`, update the imports:

```python
from open_trader.notifications import (
    CompositeNotifier,
    FeishuAppNotifier,
    FeishuWebhookNotifier,
    NotificationError,
    XiaozhiVoiceNotifier,
    render_feishu_order_review,
)
```

Add these tests after the Feishu app notifier tests:

```python
def test_xiaozhi_voice_notifier_sends_payload_and_bearer_header() -> None:
    calls: list[dict[str, object]] = []

    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        calls.append(
            {
                "url": url,
                "payload": payload,
                "headers": headers,
                "timeout": timeout_seconds,
            }
        )
        return {"code": 0, "message": "queued"}

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
        timeout_seconds=2.5,
    )

    notifier.notify("Open Trader 测试通知", "这是一条测试通知。")

    assert calls == [
        {
            "url": "http://127.0.0.1:8003/xiaozhi/notify/speak",
            "payload": {
                "device_id": "speaker-1",
                "title": "Open Trader 测试通知",
                "message": "这是一条测试通知。",
            },
            "headers": {"Authorization": "Bearer voice-token"},
            "timeout": 2.5,
        }
    ]


def test_xiaozhi_voice_notifier_raises_on_api_error() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {"code": 404, "message": "device_offline"}

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
    )

    with pytest.raises(NotificationError, match="Xiaozhi voice error 404: device_offline"):
        notifier.notify("title", "message")


def test_xiaozhi_voice_notifier_raises_when_response_omits_code() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {"message": "queued"}

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
    )

    with pytest.raises(NotificationError, match="Xiaozhi voice error missing: code"):
        notifier.notify("title", "message")


def test_xiaozhi_voice_notifier_wraps_transport_failure() -> None:
    def fake_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        raise TimeoutError("timed out")

    notifier = XiaozhiVoiceNotifier(
        speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        device_id="speaker-1",
        token="voice-token",
        post_json=fake_post,
    )

    with pytest.raises(NotificationError, match="Xiaozhi voice request failed: timed out"):
        notifier.notify("title", "message")
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: FAIL during collection with `ImportError` or `AttributeError` because `XiaozhiVoiceNotifier` does not exist.

- [ ] **Step 3: Implement `XiaozhiVoiceNotifier`**

In `src/open_trader/notifications.py`, add this class after `FeishuAppNotifier`:

```python
class XiaozhiVoiceNotifier:
    def __init__(
        self,
        *,
        speak_url: str,
        device_id: str,
        token: str,
        post_json: PostJsonWithHeaders | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.speak_url = speak_url
        self.device_id = device_id
        self.token = token
        self._post_json = post_json or _post_json_with_headers
        self.timeout_seconds = timeout_seconds

    def notify(self, title: str, message: str) -> None:
        payload: dict[str, object] = {
            "device_id": self.device_id,
            "title": title,
            "message": message,
        }
        try:
            response = self._post_json(
                self.speak_url,
                payload,
                {"Authorization": f"Bearer {self.token}"},
                self.timeout_seconds,
            )
        except Exception as exc:
            raise NotificationError(f"Xiaozhi voice request failed: {exc}") from exc

        if "code" not in response:
            raise NotificationError("Xiaozhi voice error missing: code")
        code = response.get("code")
        if code not in {0, "0"}:
            message_text = response.get("message") or response.get("msg") or ""
            raise NotificationError(f"Xiaozhi voice error {code}: {message_text}")
```

- [ ] **Step 4: Run notifier tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_notifications.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Open Trader notifier**

Run:

```bash
git add src/open_trader/notifications.py tests/test_notifications.py
git commit -m "feat: add xiaozhi voice notifier"
```

## Task 2: Wire Xiaozhi Notifier Into Open Trader Config

**Files:**
- Modify: `tests/test_daily_premarket.py`
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `config/daily_premarket.env.example`

- [ ] **Step 1: Write failing config and builder tests**

In `tests/test_daily_premarket.py`, update the notification imports:

```python
from open_trader.notifications import (
    CompositeNotifier,
    FeishuAppNotifier,
    FeishuWebhookNotifier,
    XiaozhiVoiceNotifier,
)
```

In `test_load_env_config_parses_required_values`, add these env lines:

```python
"OPEN_TRADER_XIAOZHI_SPEAK_URL=http://127.0.0.1:8003/xiaozhi/notify/speak",
"OPEN_TRADER_XIAOZHI_DEVICE_ID=speaker-1",
"OPEN_TRADER_XIAOZHI_TOKEN=voice-token",
```

In the same test, add these assertions:

```python
assert config.xiaozhi_speak_url == "http://127.0.0.1:8003/xiaozhi/notify/speak"
assert config.xiaozhi_device_id == "speaker-1"
assert config.xiaozhi_token == "voice-token"
```

Add this test after `test_build_notifier_uses_configured_feishu_app_and_macos`:

```python
def test_build_notifier_uses_configured_feishu_app_and_xiaozhi(tmp_path: Path) -> None:
    config = DailyPremarketConfig(
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
        notifiers=("feishu_app", "xiaozhi"),
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_receive_id_type="email",
        feishu_receive_id="ray@example.com",
        xiaozhi_speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        xiaozhi_device_id="speaker-1",
        xiaozhi_token="voice-token",
    )

    notifier = build_notifier(config)

    assert isinstance(notifier, CompositeNotifier)
    inner_notifiers = notifier._notifiers
    assert len(inner_notifiers) == 2
    assert isinstance(inner_notifiers[0], FeishuAppNotifier)
    assert isinstance(inner_notifiers[1], XiaozhiVoiceNotifier)
    assert inner_notifiers[1].speak_url == config.xiaozhi_speak_url
    assert inner_notifiers[1].device_id == config.xiaozhi_device_id
    assert inner_notifiers[1].token == config.xiaozhi_token
```

Add this test after `test_build_notifier_requires_feishu_app_config`:

```python
def test_build_notifier_requires_xiaozhi_config(tmp_path: Path) -> None:
    config = DailyPremarketConfig(
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
        notifiers=("xiaozhi",),
        xiaozhi_speak_url="http://127.0.0.1:8003/xiaozhi/notify/speak",
        xiaozhi_device_id="speaker-1",
    )

    with pytest.raises(ValueError, match="OPEN_TRADER_XIAOZHI_TOKEN is required"):
        build_notifier(config)
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -v
```

Expected: FAIL because `DailyPremarketConfig` does not define the Xiaozhi fields and `build_notifier()` does not recognize `xiaozhi`.

- [ ] **Step 3: Extend config and notifier builder**

In `src/open_trader/daily_premarket.py`, add `XiaozhiVoiceNotifier` to the imports from `.notifications`:

```python
from .notifications import (
    CompositeNotifier,
    FeishuAppNotifier,
    FeishuWebhookNotifier,
    MacOSNotifier,
    Notifier,
    NullNotifier,
    XiaozhiVoiceNotifier,
    render_feishu_order_review,
)
```

Add fields to `DailyPremarketConfig` after `feishu_message_format`:

```python
    xiaozhi_speak_url: str = ""
    xiaozhi_device_id: str = ""
    xiaozhi_token: str = ""
```

In `load_env_config()`, add these values to the `DailyPremarketConfig(...)` constructor after `feishu_message_format`:

```python
        xiaozhi_speak_url=values.get("OPEN_TRADER_XIAOZHI_SPEAK_URL", ""),
        xiaozhi_device_id=values.get("OPEN_TRADER_XIAOZHI_DEVICE_ID", ""),
        xiaozhi_token=values.get("OPEN_TRADER_XIAOZHI_TOKEN", ""),
```

In `build_notifier()`, add this branch before the final unknown notifier error:

```python
        if name == "xiaozhi":
            for field_name, value in [
                ("OPEN_TRADER_XIAOZHI_SPEAK_URL", config.xiaozhi_speak_url),
                ("OPEN_TRADER_XIAOZHI_DEVICE_ID", config.xiaozhi_device_id),
                ("OPEN_TRADER_XIAOZHI_TOKEN", config.xiaozhi_token),
            ]:
                if not value:
                    raise ValueError(f"{field_name} is required")
            notifiers.append(
                XiaozhiVoiceNotifier(
                    speak_url=config.xiaozhi_speak_url,
                    device_id=config.xiaozhi_device_id,
                    token=config.xiaozhi_token,
                )
            )
            continue
```

In `_notifier_channel()`, add this branch before `MacOSNotifier`:

```python
    if isinstance(notifier, XiaozhiVoiceNotifier):
        return "xiaozhi"
```

- [ ] **Step 4: Document env vars**

In `config/daily_premarket.env.example`, add this block after the Feishu webhook example:

```text
# Optional Xiaozhi/Xiaoai voice notifier:
# OPEN_TRADER_NOTIFIERS=feishu_app,xiaozhi
# OPEN_TRADER_XIAOZHI_SPEAK_URL=http://127.0.0.1:8003/xiaozhi/notify/speak
# OPEN_TRADER_XIAOZHI_DEVICE_ID=replace-device-id
# OPEN_TRADER_XIAOZHI_TOKEN=replace-shared-token
```

- [ ] **Step 5: Run Open Trader notification tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_notifications.py tests/test_daily_premarket.py tests/test_premarket_cli.py tests/test_t_signal_runner.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit Open Trader config wiring**

Run:

```bash
git add src/open_trader/daily_premarket.py config/daily_premarket.env.example tests/test_daily_premarket.py
git commit -m "feat: wire xiaozhi notifier config"
```

## Task 3: Add Xiaozhi Online Connection Registry

**Files:**
- Create: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_connection_registry.py`
- Create: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/connection_registry.py`
- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/connection.py`

- [ ] **Step 1: Write failing registry tests**

Create `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_connection_registry.py`:

```python
import unittest

from core.connection_registry import (
    clear_connections_for_tests,
    get_connection,
    list_device_ids,
    register_connection,
    unregister_connection,
)


class ConnectionRegistryTest(unittest.TestCase):
    def tearDown(self):
        clear_connections_for_tests()

    def test_register_get_list_and_unregister_connection(self):
        handler = object()

        register_connection("speaker-1", handler)

        self.assertIs(get_connection("speaker-1"), handler)
        self.assertEqual(list_device_ids(), ["speaker-1"])

        unregister_connection("speaker-1", handler)

        self.assertIsNone(get_connection("speaker-1"))
        self.assertEqual(list_device_ids(), [])

    def test_unregister_old_handler_does_not_remove_new_handler(self):
        old_handler = object()
        new_handler = object()

        register_connection("speaker-1", old_handler)
        register_connection("speaker-1", new_handler)
        unregister_connection("speaker-1", old_handler)

        self.assertIs(get_connection("speaker-1"), new_handler)

    def test_blank_device_id_is_ignored(self):
        handler = object()

        register_connection("", handler)
        register_connection(None, handler)

        self.assertEqual(list_device_ids(), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run registry tests and verify they fail**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest tests/test_connection_registry.py -v
```

Expected: FAIL because `core.connection_registry` does not exist.

- [ ] **Step 3: Implement registry module**

Create `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/connection_registry.py`:

```python
from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.RLock()
_CONNECTIONS: dict[str, Any] = {}


def register_connection(device_id: str | None, handler: Any) -> None:
    if not device_id:
        return
    with _LOCK:
        _CONNECTIONS[device_id] = handler


def unregister_connection(device_id: str | None, handler: Any) -> None:
    if not device_id:
        return
    with _LOCK:
        if _CONNECTIONS.get(device_id) is handler:
            del _CONNECTIONS[device_id]


def get_connection(device_id: str | None) -> Any | None:
    if not device_id:
        return None
    with _LOCK:
        return _CONNECTIONS.get(device_id)


def list_device_ids() -> list[str]:
    with _LOCK:
        return sorted(_CONNECTIONS)


def clear_connections_for_tests() -> None:
    with _LOCK:
        _CONNECTIONS.clear()
```

- [ ] **Step 4: Wire registry into `ConnectionHandler`**

In `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/connection.py`, add imports near the other `core.*` imports:

```python
from core.connection_registry import register_connection, unregister_connection
```

After `self.websocket = ws` in `handle_connection()`, add:

```python
            register_connection(self.device_id, self)
```

At the start of the `finally:` block in `handle_connection()`, before `_save_and_close(ws)`, add:

```python
            unregister_connection(self.device_id, self)
```

- [ ] **Step 5: Run registry tests**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest tests/test_connection_registry.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit Xiaozhi registry**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server
git add main/xiaozhi-server/core/connection_registry.py main/xiaozhi-server/core/connection.py main/xiaozhi-server/tests/test_connection_registry.py
git commit -m "feat: track online xiaozhi connections"
```

## Task 4: Add Xiaozhi Speak Handler

**Files:**
- Create: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_speak_handler.py`
- Create: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/api/speak_handler.py`

- [ ] **Step 1: Write failing speak handler tests**

Create `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_speak_handler.py`:

```python
import json
import queue
import unittest
from types import SimpleNamespace

from core.connection_registry import clear_connections_for_tests, register_connection
from core.providers.tts.dto.dto import ContentType, SentenceType
from core.api.speak_handler import SpeakHandler, build_voice_text


class _Request:
    def __init__(self, payload, token="voice-token"):
        self.headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _TTS:
    def __init__(self):
        self.tts_text_queue = queue.Queue()
        self.stored = []

    def store_tts_text(self, sentence_id, text):
        self.stored.append((sentence_id, text))


class _Conn:
    def __init__(self, with_tts=True):
        self.tts = _TTS() if with_tts else None
        self.sentence_id = None
        self.client_abort = True
        self.dialogue = SimpleNamespace(messages=[])
        self.asr = object()
        self.llm = object()


class SpeakHandlerTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        clear_connections_for_tests()

    async def test_rejects_missing_config_token(self):
        handler = SpeakHandler({"server": {}})

        response = await handler.handle_post(_Request({}))

        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.text)["message"], "notify_api_disabled")

    async def test_rejects_invalid_bearer_token(self):
        handler = SpeakHandler({"server": {"notify_api_token": "voice-token"}})

        response = await handler.handle_post(
            _Request({"device_id": "speaker-1", "title": "t", "message": "m"}, token="bad")
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(response.text)["message"], "unauthorized")

    async def test_returns_404_when_device_is_offline(self):
        handler = SpeakHandler({"server": {"notify_api_token": "voice-token"}})

        response = await handler.handle_post(
            _Request({"device_id": "speaker-1", "title": "t", "message": "m"})
        )

        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(response.text)["message"], "device_offline")

    async def test_returns_409_when_tts_is_not_ready(self):
        register_connection("speaker-1", _Conn(with_tts=False))
        handler = SpeakHandler({"server": {"notify_api_token": "voice-token"}})

        response = await handler.handle_post(
            _Request({"device_id": "speaker-1", "title": "t", "message": "m"})
        )

        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(response.text)["message"], "tts_not_ready")

    async def test_queues_first_middle_last_for_online_device(self):
        conn = _Conn()
        register_connection("speaker-1", conn)
        handler = SpeakHandler({"server": {"notify_api_token": "voice-token"}})

        response = await handler.handle_post(
            _Request(
                {
                    "device_id": "speaker-1",
                    "title": "Open Trader 测试通知",
                    "message": "第一行\r\n\r\n\r\n第二行",
                }
            )
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text), {"code": 0, "message": "queued"})
        self.assertFalse(conn.client_abort)
        self.assertEqual(len(conn.stored), 1)
        sentence_id, stored_text = conn.stored[0]
        self.assertEqual(conn.sentence_id, sentence_id)
        self.assertEqual(stored_text, "Open Trader 测试通知\n\n第一行\n\n第二行")

        queued = [
            conn.tts.tts_text_queue.get_nowait(),
            conn.tts.tts_text_queue.get_nowait(),
            conn.tts.tts_text_queue.get_nowait(),
        ]
        self.assertEqual([item.sentence_type for item in queued], [SentenceType.FIRST, SentenceType.MIDDLE, SentenceType.LAST])
        self.assertEqual([item.content_type for item in queued], [ContentType.ACTION, ContentType.TEXT, ContentType.ACTION])
        self.assertEqual(queued[1].content_detail, stored_text)

    def test_build_voice_text_normalizes_blank_lines(self):
        self.assertEqual(build_voice_text(" 标题 ", "一\r\n\r\n\r\n二 "), "标题\n\n一\n\n二")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run speak handler tests and verify they fail**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest tests/test_speak_handler.py -v
```

Expected: FAIL because `core.api.speak_handler` does not exist.

- [ ] **Step 3: Implement speak handler**

Create `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/api/speak_handler.py`:

```python
import json
import re
import uuid

from aiohttp import web

from core.api.base_handler import BaseHandler
from core.connection_registry import get_connection
from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType

TAG = __name__


def build_voice_text(title: str, message: str) -> str:
    text = f"{title}\n\n{message}".replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class SpeakHandler(BaseHandler):
    def _json_response(self, status: int, code: int, message: str) -> web.Response:
        response = web.Response(
            text=json.dumps({"code": code, "message": message}, ensure_ascii=False),
            content_type="application/json",
            status=status,
        )
        self._add_cors_headers(response)
        return response

    def _configured_token(self) -> str:
        server_config = self.config.get("server", {})
        if not isinstance(server_config, dict):
            return ""
        return str(server_config.get("notify_api_token") or "")

    def _authorized(self, request, expected_token: str) -> bool:
        auth_header = request.headers.get("Authorization", "")
        return auth_header == f"Bearer {expected_token}"

    async def handle_post(self, request):
        expected_token = self._configured_token()
        if not expected_token:
            return self._json_response(503, 503, "notify_api_disabled")
        if not self._authorized(request, expected_token):
            return self._json_response(401, 401, "unauthorized")

        try:
            payload = await request.json()
        except Exception:
            return self._json_response(400, 400, "bad_request")
        if not isinstance(payload, dict):
            return self._json_response(400, 400, "bad_request")

        device_id = payload.get("device_id")
        title = payload.get("title")
        message = payload.get("message")
        if (
            not isinstance(device_id, str)
            or not device_id.strip()
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(message, str)
        ):
            return self._json_response(400, 400, "bad_request")

        conn = get_connection(device_id.strip())
        if conn is None:
            return self._json_response(404, 404, "device_offline")
        tts = getattr(conn, "tts", None)
        if tts is None or not hasattr(tts, "tts_text_queue"):
            return self._json_response(409, 409, "tts_not_ready")

        voice_text = build_voice_text(title, message)
        sentence_id = uuid.uuid4().hex
        conn.sentence_id = sentence_id
        conn.client_abort = False
        if hasattr(tts, "store_tts_text"):
            tts.store_tts_text(sentence_id, voice_text)
        tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
        tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail=voice_text,
            )
        )
        tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )
        return self._json_response(200, 0, "queued")
```

- [ ] **Step 4: Run speak handler tests**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest tests/test_speak_handler.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Xiaozhi speak handler**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server
git add main/xiaozhi-server/core/api/speak_handler.py main/xiaozhi-server/tests/test_speak_handler.py
git commit -m "feat: add xiaozhi speak api handler"
```

## Task 5: Wire Xiaozhi Speak Route And Config

**Files:**
- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/http_server.py`
- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/config.yaml`
- Modify: `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_speak_handler.py`

- [ ] **Step 1: Add failing route registration test**

Append this test class to `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/tests/test_speak_handler.py`:

```python
from core.http_server import SimpleHttpServer


class SpeakRouteWiringTest(unittest.TestCase):
    def test_http_server_registers_speak_route(self):
        app = SimpleHttpServer({"server": {"notify_api_token": "voice-token"}}).create_app()

        routes = {
            (route.method, route.resource.canonical)
            for route in app.router.routes()
        }

        self.assertIn(("POST", "/xiaozhi/notify/speak"), routes)
        self.assertIn(("OPTIONS", "/xiaozhi/notify/speak"), routes)
```

- [ ] **Step 2: Run route test and verify it fails**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest tests/test_speak_handler.py::SpeakRouteWiringTest -v
```

Expected: FAIL because the route is not registered.

- [ ] **Step 3: Wire handler into HTTP server**

In `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/core/http_server.py`, add the import:

```python
from core.api.speak_handler import SpeakHandler
```

In `SimpleHttpServer.__init__`, add:

```python
        self.speak_handler = SpeakHandler(config)
```

In `create_app()`, add these routes to the always-registered route list:

```python
                web.post("/xiaozhi/notify/speak", self.speak_handler.handle_post),
                web.options("/xiaozhi/notify/speak", self.speak_handler.handle_options),
```

- [ ] **Step 4: Document disabled default config**

In `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/config.yaml`, under `server:` after `http_port: 8003`, add:

```yaml
  # Open Trader voice notification API token. Leave empty to disable /xiaozhi/notify/speak.
  # Put the real token in data/.config.yaml instead of committing it here.
  notify_api_token: ""
```

- [ ] **Step 5: Run Xiaozhi speak and startup tests**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest tests/test_connection_registry.py tests/test_speak_handler.py tests/test_app_startup.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit route wiring**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server
git add main/xiaozhi-server/core/http_server.py main/xiaozhi-server/config.yaml main/xiaozhi-server/tests/test_speak_handler.py
git commit -m "feat: expose xiaozhi speak route"
```

## Task 6: Cross-Repository Verification

**Files:**
- Verify only; no planned source edits.

- [ ] **Step 1: Run Open Trader focused tests**

Run:

```bash
cd /Users/ray/projects/open_trader
.venv/bin/python -m pytest tests/test_notifications.py tests/test_daily_premarket.py tests/test_premarket_cli.py tests/test_t_signal_runner.py -v
```

Expected: PASS.

- [ ] **Step 2: Run Xiaozhi focused tests**

Run:

```bash
cd /Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server
.venv/bin/python -m pytest tests/test_connection_registry.py tests/test_speak_handler.py tests/test_app_startup.py tests/test_ping_message_handler.py -v
```

Expected: PASS.

- [ ] **Step 3: Configure local Xiaozhi token**

In `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server/data/.config.yaml`, add:

```yaml
server:
  notify_api_token: replace-with-local-shared-token
```

Do not commit `data/.config.yaml`.

- [ ] **Step 4: Configure Open Trader local env**

In `/Users/ray/projects/open_trader/config/daily_premarket.env`, set:

```text
OPEN_TRADER_NOTIFIERS=feishu_app,xiaozhi
OPEN_TRADER_XIAOZHI_SPEAK_URL=http://127.0.0.1:8003/xiaozhi/notify/speak
OPEN_TRADER_XIAOZHI_DEVICE_ID=replace-with-online-device-id
OPEN_TRADER_XIAOZHI_TOKEN=replace-with-local-shared-token
```

Do not commit `config/daily_premarket.env`.

- [ ] **Step 5: Manually verify the Xiaozhi speak endpoint**

Run with the real values:

```bash
curl -sS \
  -H "Authorization: Bearer replace-with-local-shared-token" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"replace-with-online-device-id","title":"Open Trader 测试通知","message":"这是一条 Open Trader 语音测试通知。"}' \
  http://127.0.0.1:8003/xiaozhi/notify/speak
```

Expected response:

```json
{"code": 0, "message": "queued"}
```

Expected device behavior: the target speaker speaks the title and message.

- [ ] **Step 6: Manually verify Open Trader test notification**

Run:

```bash
cd /Users/ray/projects/open_trader
.venv/bin/python -m open_trader test-notification --config config/daily_premarket.env
```

Expected terminal output:

```text
通知测试已发送。
```

Expected behavior: Feishu receives the Open Trader test notification and the target speaker speaks the same notification.

- [ ] **Step 7: Final status check**

Run:

```bash
cd /Users/ray/projects/open_trader
git status --short
cd /Users/ray/projects/xiaozhi-esp32-server
git status --short
```

Expected: only intentionally untracked or local secret config files remain. Source and test changes should already be committed.

## Self-Review Notes

- Spec coverage: Task 1 and Task 2 cover Open Trader notifier, env config, channel naming, and best-effort result reporting. Task 3 covers Xiaozhi online device lookup. Task 4 covers auth, validation, text cleanup, and queueing into existing TTS. Task 5 covers HTTP route and disabled default config. Task 6 covers real endpoint and Open Trader test-notification verification.
- Placeholder scan: no task contains placeholder markers or unspecified implementation work.
- Type consistency: Open Trader uses `XiaozhiVoiceNotifier`, env keys `OPEN_TRADER_XIAOZHI_*`, and channel name `xiaozhi` consistently. Xiaozhi uses `server.notify_api_token`, `SpeakHandler`, `build_voice_text`, and `connection_registry` consistently.
