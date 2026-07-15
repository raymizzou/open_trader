# Open Trader Native XiaoAI Voice Design

## Goal

Move Open Trader's existing one-way voice alerts into Open Trader so playback
no longer depends on the `open-xiaoai` or `xiaozhi-esp32-server` runtimes.

## Scope

- Preserve the current voice allowlist: manual test notifications and A-share,
  Hong Kong, or US protection-line triggers.
- Preserve the existing spoken text, per-symbol/per-trading-date deduplication,
  `08:00` inclusive to `23:00` exclusive Shanghai voice window, and Feishu-only
  failure warning.
- Use the XiaoAI speaker's native TTS voice.
- Support the one configured OH2P speaker on the trusted local network.
- Keep the old repositories unchanged after cutover.

Conversation, wake words, recording, ASR, story playback, interruption, external
TTS providers, Opus encoding, and general audio streaming are out of scope.

## Architecture

Replace `XiaozhiVoiceNotifier`'s HTTP queue submission with an
`XiaoaiSSHNotifier` owned by Open Trader. It invokes the system `ssh` client and
runs `/usr/sbin/tts_play.sh` on the speaker. A dedicated SSH key stored outside
Git authenticates the call; OpenSSH verifies the speaker through the user's
known-hosts file.

The notifier uses a macOS/POSIX file lock to serialize calls from independent
watcher processes. After acquiring the lock, it checks the Shanghai voice
window again. If the deadline has passed, it skips playback. No daemon, durable
queue, retry loop, third-party Python dependency, or copied Open-XiaoAI module
is added.

Two larger alternatives were rejected: retaining the existing HTTP/TTS stack
would not make Open Trader independent, while copying its Rust, WebSocket,
external TTS, and Opus pipeline would migrate conversation infrastructure that
one-way alerts do not need. Direct SSH is the smallest complete playback path.

## Configuration

The notification channel is renamed from `xiaozhi` to `xiaoai`. Open Trader
loads only:

- `OPEN_TRADER_XIAOAI_HOST`: the speaker host or IP address.
- `OPEN_TRADER_XIAOAI_SSH_KEY`: the dedicated private-key path.

The remote user remains `root`, the port remains `22`, the native TTS command
remains `/usr/sbin/tts_play.sh`, and the lock path is an Open Trader-owned file
at `/tmp/open_trader_xiaoai_voice.lock`. These fixed values are not configurable
until a second real deployment requires it.

The old `OPEN_TRADER_XIAOZHI_SPEAK_URL`, `OPEN_TRADER_XIAOZHI_DEVICE_ID`, and
`OPEN_TRADER_XIAOZHI_TOKEN` settings are removed rather than supported through
a compatibility layer.

## Playback Flow

1. Existing notification routing renders a permitted voice message.
2. Existing quiet-hours logic rejects an ineligible message before transport.
3. The notifier opens the shared lock file and waits for an exclusive lock.
4. It checks quiet hours again; a message that waited until `23:00` is reported
   as quiet-hours-suppressed and skipped without a failure warning.
5. It invokes SSH once with safely shell-quoted text and a bounded timeout.
6. Exit status zero records the existing successful voice outcome.
7. Any timeout or nonzero exit becomes `NotificationError`; existing watcher
   handling records failure and sends one Feishu-only warning without retry.

Waiting watcher processes may block while a short alert is playing. Process
exit releases the file lock automatically. Playback is intentionally not
durable: a process that dies before speaking does not replay the alert.

## Security

- Generate a dedicated SSH key; never commit its private half.
- Install only its public key on the speaker.
- Do not put a password, spoken text, token, or full SSH command in errors.
- Use OpenSSH host-key verification; do not enable `StrictHostKeyChecking=no`.
- Quote the entire spoken message as one remote argument so notification text
  cannot become a shell command.

## Cutover

First prove the new path with a real manual voice notification. Then stop the
`open-xiaoai-xiaozhi` Docker container, the `xiaozhi-server` screen session,
and the speaker's Open-XiaoAI client. Disable its boot entry reversibly while
retaining the old source repositories and device binary for rollback.

## Verification

- Unit tests cover command construction, safe quoting, serialization, timeout
  and nonzero exits, the post-lock quiet-hours check, allowlist preservation,
  and clean configuration migration.
- Run the focused tests, type/lint checks, and full test suite.
- Send a real manual notification and hear it on the configured speaker.
- Inspect fresh logs and process state; confirm the old services and device
  client are stopped after cutover.
- Run `make acceptance` after all Open Trader changes. Only `PASS` is accepted.
- Commit the accepted source, redeploy that exact Git SHA, and verify the new
  PID, working directory, SHA, fresh logs, and HTTP 200 review URL.
