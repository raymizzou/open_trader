# Disable TradingAgents Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop automated TradingAgents daily premarket reports and every notification emitted by that workflow while preserving manual runs, historical artifacts, and trend controllers.

**Architecture:** Reuse the existing launchd uninstaller to remove only the three legacy daily premarket jobs. Disable the workflow-specific notification switch in the shared local configuration; do not clear the shared notifier list or change application source.

**Tech Stack:** Bash, macOS launchd, Python 3.12 configuration loader

## Global Constraints

- Preserve the manual `run-daily-premarket` command.
- Preserve existing reports and run artifacts.
- Preserve the CN, HK, and US `trend-market` controllers and their notifications.
- Do not modify application source code.
- Do not run the Dashboard acceptance gate because no Dashboard source changes.

---

### Task 1: Disable the TradingAgents Daily Automation

**Files:**
- Modify: `/Users/ray/projects/open_trader/config/daily_premarket.env`
- Reuse: `scripts/uninstall_daily_premarket_launchd.sh`

**Interfaces:**
- Consumes: the existing launchd labels `com.open-trader.premarket`, `com.open-trader.premarket.hk`, and `com.open-trader.premarket.us`
- Produces: shared configuration with `DailyPremarketConfig.notify_daily_report == False`

- [x] **Step 1: Capture the pre-change state**

Run:

```bash
ps axww -o pid=,command= | rg -i 'TradingAgents|run-daily-premarket|run-premarket|tradingagents_worker' | rg -v 'rg -i' || true
launchctl list | rg 'com\.open-trader\.premarket(\.(hk|us))?$' || true
awk -F= '$1 == "OPEN_TRADER_NOTIFY_DAILY_REPORT" {print}' /Users/ray/projects/open_trader/config/daily_premarket.env
launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)$'
```

Expected: no TradingAgents process or daily premarket label; notification flag
is `1`; all three trend controllers are listed.

- [x] **Step 2: Remove every legacy daily premarket launchd job**

Run:

```bash
scripts/uninstall_daily_premarket_launchd.sh
```

Expected: the script reports each daily premarket agent as removed or not
installed. It must not remove a trend-controller label.

- [x] **Step 3: Disable all daily premarket notifications**

Apply this exact local configuration change:

```diff
-OPEN_TRADER_NOTIFY_DAILY_REPORT=1
+OPEN_TRADER_NOTIFY_DAILY_REPORT=0
```

Do not change `OPEN_TRADER_NOTIFIERS`; trend controllers share that list.

- [x] **Step 4: Verify the configuration loader sees notifications disabled**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python - <<'PY'
from pathlib import Path
from open_trader.daily_premarket import load_env_config

config = load_env_config(Path("/Users/ray/projects/open_trader/config/daily_premarket.env"))
assert config.notify_daily_report is False
print("notify_daily_report=False")
PY
```

Expected: exit code `0` and `notify_daily_report=False`.

- [x] **Step 5: Verify no scheduler or process can emit another automatic report notification**

Run:

```bash
set -euo pipefail
process_matches="$(
  ps axww -o pid=,command= |
    rg -i 'TradingAgents|run-daily-premarket|run-premarket|tradingagents_worker' |
    rg -v 'rg -i' || true
)"
if [[ -n "$process_matches" ]]; then
  printf 'unexpected TradingAgents process:\n%s\n' "$process_matches" >&2
  exit 1
fi
for domain in "gui/$(id -u)" system; do
  for label in com.open-trader.premarket com.open-trader.premarket.hk com.open-trader.premarket.us; do
    if launchctl print "$domain/$label" >/dev/null 2>&1; then
      echo "unexpected launchd job: $domain/$label" >&2
      exit 1
    fi
  done
done
plist_roots=(
  "$HOME/Library/LaunchAgents"
  /Library/LaunchAgents
  /Library/LaunchDaemons
  /System/Library/LaunchAgents
  /System/Library/LaunchDaemons
)
named_plists="$(
  find "${plist_roots[@]}" -maxdepth 1 -type f \
    -name 'com.open-trader.premarket*.plist' -print 2>/dev/null || true
)"
plist_matches="$(
  find "${plist_roots[@]}" -maxdepth 1 -type f -name '*.plist' -print0 2>/dev/null |
    xargs -0 rg -l -i \
      'com\.open-trader\.premarket|run-daily-premarket|run-premarket|TradingAgents' \
      2>/dev/null || true
)"
if [[ -n "$named_plists" || -n "$plist_matches" ]]; then
  printf 'unexpected TradingAgents plist:\n%s\n%s\n' \
    "$named_plists" "$plist_matches" >&2
  exit 1
fi
cron_matches="$(
  crontab -l 2>&1 |
    rg -i 'run-daily-premarket|run-premarket|TradingAgents' || true
)"
at_matches="$(
  atq 2>&1 |
    rg -i 'run-daily-premarket|run-premarket|TradingAgents' || true
)"
screen_matches="$(
  screen -ls 2>&1 |
    rg -i 'run-daily-premarket|run-premarket|TradingAgents' || true
)"
if [[ -n "$cron_matches" || -n "$at_matches" || -n "$screen_matches" ]]; then
  printf 'unexpected TradingAgents scheduled task:\n%s\n%s\n%s\n' \
    "$cron_matches" "$at_matches" "$screen_matches" >&2
  exit 1
fi
for market in CN HK US; do
  lower="$(printf '%s' "$market" | tr '[:upper:]' '[:lower:]')"
  label="com.open-trader.trend-market-controller.$lower"
  pid="$(launchctl list | awk -v label="$label" '$3 == label {print $1}')"
  if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
    echo "missing live trend controller PID: $label" >&2
    exit 1
  fi
  if ! ps -p "$pid" -o command= |
    rg "open_trader trend-market run --market $market"; then
    echo "unexpected trend controller command: $label pid=$pid" >&2
    exit 1
  fi
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python - "$market" "$pid" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

market, pid = sys.argv[1], int(sys.argv[2])
status = json.loads(
    Path(f"/Users/ray/projects/open_trader/data/trend_controller/{market}/status.json")
    .read_text(encoding="utf-8")
)
heartbeat = datetime.fromisoformat(status["heartbeat_at"])
now = datetime.now(heartbeat.tzinfo)
assert status["pid"] == pid
assert (now - heartbeat).total_seconds() < 120
print(f"{market}: pid={pid} heartbeat={heartbeat.isoformat()}")
PY
done
```

Expected: no matching process, plist, cron entry, or screen session; all three
trend controllers remain listed.

- [x] **Step 6: Record the operational result**

No production commit is required because the only runtime change is in the
Git-ignored local configuration. Commit this implementation plan separately
from the already committed design document.

## Operational Result

Verified at `2026-07-28T19:56:11+0800`:

- `notify_daily_report=False`; shared notifiers remain
  `feishu_app,macos,xiaoai`.
- TradingAgents processes, user/system launchd jobs, scheduler plists, cron
  entries, `at` entries, screen sessions, and run locks: `0`.
- Notification-disabled success, partial, and failure tests: `3 passed`.
- CN, HK, and US trend controllers remained loaded with matching live process
  commands and fresh heartbeats.
