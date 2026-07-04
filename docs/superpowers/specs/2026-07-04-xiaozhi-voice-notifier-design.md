# Xiaozhi Voice Notifier Design

## Goal

Add a voice notification channel so every Open Trader notification that is sent
to Feishu is also spoken through the already-connected Xiaoai speaker path.

The first version does not filter, summarize, prioritize, or suppress voice
events. If Open Trader sends a notification through the existing notifier path,
the same title and message are also sent to the voice channel when `xiaozhi` is
configured.

## Confirmed Scope

- Add a new Open Trader notifier named `xiaozhi`.
- Reuse the existing `Notifier.notify(title, message)` interface.
- Keep Feishu notification rendering as the source of truth for voice text.
- Send all existing notification types that flow through Open Trader notifiers:
  daily premarket start, blocker, action, completion, test notification, and
  `watch-t` 做T alerts.
- Add a small Xiaozhi HTTP API that accepts a title and message, finds an online
  device connection, and queues the text into the existing TTS pipeline.
- Keep notification failures best-effort. Voice failure must not fail a trading
  run or prevent Feishu delivery.

## Out Of Scope

- Event filtering, priority filtering, quiet hours, or per-event routing.
- Voice-only message templates that diverge from Feishu content.
- Direct Open Trader WebSocket connection to Xiaoai or Xiaozhi devices.
- Reimplementing TTS, Opus encoding, MQTT audio packets, or device protocol in
  Open Trader.
- Placing trades or changing trading decision logic.

## Current Xiaozhi Voice Path

Xiaozhi voice output is tied to an online device `ConnectionHandler`.

When a device connects, `ConnectionHandler.handle_connection()` stores the
request headers, `device_id`, `websocket`, event loop, and audio parameters.
During background initialization it creates or loads a TTS provider and calls
`tts.open_audio_channels(conn)`.

`open_audio_channels()` starts two daemon threads:

- `tts_text_priority_thread`: consumes `TTSMessageDTO` objects from
  `tts_text_queue`, converts text to audio, and pushes generated audio into
  `tts_audio_queue`.
- `_audio_play_priority_thread`: consumes `tts_audio_queue` and schedules
  `sendAudioMessage()` on the connection event loop.

`sendAudioMessage()` sends TTS status JSON and audio packets back to the device.
For normal WebSocket devices it sends Opus packets directly. For MQTT gateway
connections it wraps audio packets with the existing MQTT gateway packet header.

The current Xiaozhi HTTP server does not expose a general "speak this text on a
device" route. It exposes OTA, vision, wake-word persona audio, and story audio
routes. Therefore this design adds a narrow local API rather than trying to make
Open Trader speak the Xiaozhi device protocol.

## Architecture

```text
Open Trader event
-> existing notification rendering
-> Notifier.notify(title, message)
-> CompositeNotifier
   -> FeishuAppNotifier or FeishuWebhookNotifier
   -> XiaozhiVoiceNotifier
-> Xiaozhi POST /xiaozhi/notify/speak
-> online connection registry
-> target ConnectionHandler.tts.tts_text_queue
-> existing Xiaozhi TTS and audio sender
-> Xiaoai speaker output
```

Open Trader owns notification configuration and delivery attempts. Xiaozhi owns
device connection lookup and audio playback. The interface between them is a
small JSON-over-HTTP contract.

## Open Trader Changes

### `src/open_trader/notifications.py`

Add `XiaozhiVoiceNotifier`.

Constructor inputs:

- `speak_url`: local Xiaozhi HTTP endpoint.
- `device_id`: target Xiaozhi device id.
- `token`: shared bearer token for the local speak API.
- `timeout_seconds`: default `10.0`.
- injectable `post_json_with_headers` helper for tests.

`notify(title, message)` posts:

```json
{
  "device_id": "configured-device-id",
  "title": "Open Trader｜做T提醒｜HK.02840｜卖出做T",
  "message": "动作：卖出做T\n比例：10%\n状态：盘中有效..."
}
```

Headers:

```text
Authorization: Bearer <OPEN_TRADER_XIAOZHI_TOKEN>
```

Expected successful response:

```json
{
  "code": 0,
  "message": "queued"
}
```

If the response is not an object, omits `code`, or has a non-zero code, raise
`NotificationError`. Transport failures also raise `NotificationError`.

### `src/open_trader/daily_premarket.py`

Extend `DailyPremarketConfig` with:

- `xiaozhi_speak_url`
- `xiaozhi_device_id`
- `xiaozhi_token`

Extend `load_env_config()` to read:

```text
OPEN_TRADER_XIAOZHI_SPEAK_URL
OPEN_TRADER_XIAOZHI_DEVICE_ID
OPEN_TRADER_XIAOZHI_TOKEN
```

Extend `build_notifier()` to recognize `xiaozhi`.

Required fields for `xiaozhi`:

- `OPEN_TRADER_XIAOZHI_SPEAK_URL`
- `OPEN_TRADER_XIAOZHI_DEVICE_ID`
- `OPEN_TRADER_XIAOZHI_TOKEN`

The channel name returned by `_notifier_channel()` is `xiaozhi`.

### `config/daily_premarket.env.example`

Add commented example configuration:

```text
# Optional Xiaozhi/Xiaoai voice notifier:
# OPEN_TRADER_NOTIFIERS=feishu_app,xiaozhi
# OPEN_TRADER_XIAOZHI_SPEAK_URL=http://127.0.0.1:8003/xiaozhi/notify/speak
# OPEN_TRADER_XIAOZHI_DEVICE_ID=replace-device-id
# OPEN_TRADER_XIAOZHI_TOKEN=replace-shared-token
```

## Xiaozhi Changes

The changes live in `/Users/ray/projects/xiaozhi-esp32-server/main/xiaozhi-server`.

### Online Connection Registry

Add a small registry module:

```text
core/connection_registry.py
```

Responsibilities:

- Register a `ConnectionHandler` by `device_id` when the device connection is
  ready for routing.
- Unregister only if the stored handler is the same object that is closing.
- Return a handler by `device_id`.
- List online device ids for diagnostics and tests.

The registry should be protected by a threading lock because Xiaozhi uses both
asyncio tasks and worker threads.

Registration point:

- After `ConnectionHandler` has `device_id`, `websocket`, and `loop`.

Unregistration point:

- In connection cleanup after the device disconnects.

If `device_id` is missing, do not register the connection.

### Speak Handler

Add a handler module:

```text
core/api/speak_handler.py
```

Route:

```text
POST /xiaozhi/notify/speak
```

Authentication:

- Require `Authorization: Bearer <token>`.
- Token comes from Xiaozhi config key `server.notify_api_token`.
- If the token is missing from config, the route should reject requests with
  `503 notify_api_disabled`.
- If the request token is wrong, return `401 unauthorized`.

Request body:

```json
{
  "device_id": "xx:xx:xx",
  "title": "Open Trader 测试通知",
  "message": "这是一条 Open Trader 测试通知。"
}
```

Validation:

- `device_id` is required and must be a non-empty string.
- `title` is required and must be a non-empty string.
- `message` is required and must be a string. Empty message is allowed only for
  test cases where the title carries the whole spoken text.

Response codes:

- `200 {"code": 0, "message": "queued"}` when the text is queued.
- `400 {"code": 400, "message": "bad_request"}` for malformed JSON or invalid
  fields.
- `401 {"code": 401, "message": "unauthorized"}` for a bad token.
- `404 {"code": 404, "message": "device_offline"}` when no online connection
  exists for `device_id`.
- `409 {"code": 409, "message": "tts_not_ready"}` when the device is online but
  its TTS pipeline is not initialized yet.
- `503 {"code": 503, "message": "notify_api_disabled"}` when the API token is not
  configured.

### Speak Queueing

The handler should build one spoken text from the title and message:

```text
<title>

<message>
```

Apply only minimal text cleanup:

- Normalize CRLF to LF.
- Trim leading and trailing whitespace.
- Collapse runs of more than two blank lines to two blank lines.

Do not filter by event type, priority, market, symbol, or notification kind.
Do not summarize or shorten the content in the first version.

Queue the text using the existing TTS path:

```python
sentence_id = uuid.uuid4().hex
conn.sentence_id = sentence_id
conn.client_abort = False
conn.tts.store_tts_text(sentence_id, voice_text)
conn.tts.tts_text_queue.put(TTSMessageDTO(
    sentence_id=sentence_id,
    sentence_type=SentenceType.FIRST,
    content_type=ContentType.ACTION,
))
conn.tts.tts_text_queue.put(TTSMessageDTO(
    sentence_id=sentence_id,
    sentence_type=SentenceType.MIDDLE,
    content_type=ContentType.TEXT,
    content_detail=voice_text,
))
conn.tts.tts_text_queue.put(TTSMessageDTO(
    sentence_id=sentence_id,
    sentence_type=SentenceType.LAST,
    content_type=ContentType.ACTION,
))
```

The handler must not call the LLM or intent pipeline. It is a notification
playback command, not a user conversation.

### HTTP Route Wiring

Add `SpeakHandler` to `core/http_server.py` and register:

```text
web.post("/xiaozhi/notify/speak", self.speak_handler.handle_post)
web.options("/xiaozhi/notify/speak", self.speak_handler.handle_options)
```

## Error Handling

Open Trader notification semantics remain best-effort.

- If Feishu succeeds and Xiaozhi fails, the notification attempt list records one
  success and one failure.
- `CompositeNotifier.notify()` continues trying all channels.
- `send_notification_with_results()` reports channel-level results.
- Daily premarket workflow and `watch-t` dedupe state must not be changed by
  Xiaozhi delivery failure.

Xiaozhi speak API should fail fast and return structured errors rather than
blocking while trying to reconnect a device.

## Security

The speak API should be bound to the existing Xiaozhi HTTP server, which is
expected to run on a trusted local network or localhost. The route still requires
a bearer token because it causes a physical speaker to output arbitrary text.

Do not log the bearer token or Open Trader config secrets.

Open Trader should not print the token in CLI errors. Error messages may name
the `xiaozhi` channel and the structured response message.

## Testing

### Open Trader Unit Tests

Add tests in `tests/test_notifications.py`:

- `XiaozhiVoiceNotifier` sends the expected JSON payload and bearer header.
- Non-zero API code raises `NotificationError`.
- Missing response code raises `NotificationError`.
- Transport failure raises `NotificationError`.
- `send_notification_with_results()` reports channel `xiaozhi` for the new
  notifier.

Add tests for config loading/building where existing tests cover notification
configuration:

- `build_notifier()` creates `XiaozhiVoiceNotifier` when `xiaozhi` is configured.
- Missing Xiaozhi fields produce clear `ValueError`s.

### Xiaozhi Unit Tests

Add focused tests under `main/xiaozhi-server/tests`:

- Registry registers and unregisters a handler by `device_id`.
- Unregistering an old handler does not remove a newer handler for the same
  device.
- Speak API rejects missing or invalid bearer token.
- Speak API returns `404 device_offline` when no connection exists.
- Speak API returns `409 tts_not_ready` when connection exists without TTS.
- Speak API queues exactly `FIRST`, `MIDDLE`, `LAST` messages for an online fake
  connection and does not call LLM or ASR code.

Use fake connection and fake TTS queue objects. Do not require a real Xiaoai
speaker or real TTS provider in unit tests.

## Acceptance Criteria

- Setting `OPEN_TRADER_NOTIFIERS=feishu_app,xiaozhi` sends the same Open Trader
  notification to Feishu and Xiaozhi voice.
- `open-trader test-notification --config config/daily_premarket.env` causes the
  target speaker to speak the Open Trader test notification when the device is
  online.
- Daily premarket notifications and `watch-t` 做T notifications use the same
  voice channel without event-specific code.
- Voice delivery failure is visible in notification results/logs and does not
  fail the trading workflow.
- No event filtering is implemented in the first version.
