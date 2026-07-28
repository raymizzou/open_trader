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

- [ ] **Step 1: Capture the pre-change state**

Run:

```bash
ps axww -o pid=,command= | rg -i 'TradingAgents|run-daily-premarket|run-premarket|tradingagents_worker' | rg -v 'rg -i' || true
launchctl list | rg 'com\.open-trader\.premarket(\.(hk|us))?$' || true
awk -F= '$1 == "OPEN_TRADER_NOTIFY_DAILY_REPORT" {print}' /Users/ray/projects/open_trader/config/daily_premarket.env
launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)$'
```

Expected: no TradingAgents process or daily premarket label; notification flag
is `1`; all three trend controllers are listed.

- [ ] **Step 2: Remove every legacy daily premarket launchd job**

Run:

```bash
scripts/uninstall_daily_premarket_launchd.sh
```

Expected: the script reports each daily premarket agent as removed or not
installed. It must not remove a trend-controller label.

- [ ] **Step 3: Disable all daily premarket notifications**

Apply this exact local configuration change:

```diff
-OPEN_TRADER_NOTIFY_DAILY_REPORT=1
+OPEN_TRADER_NOTIFY_DAILY_REPORT=0
```

Do not change `OPEN_TRADER_NOTIFIERS`; trend controllers share that list.

- [ ] **Step 4: Verify the configuration loader sees notifications disabled**

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

- [ ] **Step 5: Verify no scheduler or process can emit another automatic report notification**

Run:

```bash
ps axww -o pid=,command= | rg -i 'TradingAgents|run-daily-premarket|run-premarket|tradingagents_worker' | rg -v 'rg -i' || true
for label in com.open-trader.premarket com.open-trader.premarket.hk com.open-trader.premarket.us; do
  ! launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1
done
plist_matches="$(
  find "$HOME/Library/LaunchAgents" /Library/LaunchAgents /Library/LaunchDaemons \
    -maxdepth 1 -type f -name '*.plist' -print0 2>/dev/null |
    xargs -0 rg -l -i 'run-daily-premarket|run-premarket|TradingAgents' 2>/dev/null || true
)"
[[ -z "$plist_matches" ]]
crontab -l 2>&1 | rg -i 'run-daily-premarket|run-premarket|TradingAgents' && exit 1 || true
atq 2>&1 | rg -i 'run-daily-premarket|run-premarket|TradingAgents' && exit 1 || true
screen -ls 2>&1 | rg -i 'run-daily-premarket|run-premarket|TradingAgents' && exit 1 || true
launchctl list | rg 'com\.open-trader\.trend-market-controller\.(cn|hk|us)$'
```

Expected: no matching process, plist, cron entry, or screen session; all three
trend controllers remain listed.

- [ ] **Step 6: Record the operational result**

No production commit is required because the only runtime change is in the
Git-ignored local configuration. Commit this implementation plan separately
from the already committed design document.
