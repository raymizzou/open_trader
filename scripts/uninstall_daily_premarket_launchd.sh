#!/usr/bin/env bash
set -euo pipefail

MARKET="all"
MARKET_REQUESTED=0
TREND_ONLY=0

usage() {
  echo "usage: $0 [--trend-only] [--market HK|US|CN|all]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --market)
      if [[ $# -lt 2 ]]; then
        usage
        exit 2
      fi
      MARKET="$2"
      MARKET_REQUESTED=1
      shift 2
      ;;
    --trend-only)
      TREND_ONLY=1
      shift
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ "$MARKET" != "HK" && "$MARKET" != "US" && "$MARKET" != "CN" && "$MARKET" != "all" ]]; then
  usage
  exit 2
fi
if [[ "$TREND_ONLY" -eq 1 && "$MARKET" == "CN" ]]; then
  usage
  exit 2
fi

remove_target() {
  local target="$1"
  if [[ -f "$target" ]]; then
    launchctl unload "$target" 2>/dev/null || true
    rm "$target"
    echo "removed launchd agent: $target"
  else
    echo "launchd agent not installed: $target"
  fi
}

if [[ "$TREND_ONLY" -eq 0 && ( "$MARKET" == "all" || "$MARKET" == "HK" ) ]]; then
  remove_target "$HOME/Library/LaunchAgents/com.open-trader.premarket.hk.plist"
fi

if [[ "$TREND_ONLY" -eq 0 && ( "$MARKET" == "all" || "$MARKET" == "US" ) ]]; then
  remove_target "$HOME/Library/LaunchAgents/com.open-trader.premarket.us.plist"
fi

if [[ "$TREND_ONLY" -eq 0 && "$MARKET" == "all" ]]; then
  remove_target "$HOME/Library/LaunchAgents/com.open-trader.premarket.plist"
fi

if [[ "$TREND_ONLY" -eq 0 && "$MARKET_REQUESTED" -eq 1 && ( "$MARKET" == "CN" || "$MARKET" == "all" ) ]]; then
  remove_target "$HOME/Library/LaunchAgents/com.open-trader.trend-a-share-report.plist"
  remove_target "$HOME/Library/LaunchAgents/com.open-trader.trend-a-share-watch.plist"
fi

if [[ "$TREND_ONLY" -eq 1 ]]; then
  for market in HK US; do
    if [[ "$MARKET" != "all" && "$MARKET" != "$market" ]]; then
      continue
    fi
    lower="$(printf '%s' "$market" | tr '[:upper:]' '[:lower:]')"
    remove_target "$HOME/Library/LaunchAgents/com.open-trader.trend-$lower-report.plist"
    remove_target "$HOME/Library/LaunchAgents/com.open-trader.trend-$lower-watch.plist"
  done
fi
