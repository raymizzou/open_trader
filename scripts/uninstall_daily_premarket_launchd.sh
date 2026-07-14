#!/usr/bin/env bash
set -euo pipefail

MARKET="all"
MARKET_REQUESTED=0

usage() {
  echo "usage: $0 [--market HK|US|CN|all]" >&2
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

if [[ "$MARKET" == "all" || "$MARKET" == "HK" ]]; then
  remove_target "$HOME/Library/LaunchAgents/com.open-trader.premarket.hk.plist"
fi

if [[ "$MARKET" == "all" || "$MARKET" == "US" ]]; then
  remove_target "$HOME/Library/LaunchAgents/com.open-trader.premarket.us.plist"
fi

if [[ "$MARKET" == "all" ]]; then
  remove_target "$HOME/Library/LaunchAgents/com.open-trader.premarket.plist"
fi

if [[ "$MARKET_REQUESTED" -eq 1 && ( "$MARKET" == "CN" || "$MARKET" == "all" ) ]]; then
  remove_target "$HOME/Library/LaunchAgents/com.open-trader.trend-a-share-report.plist"
  remove_target "$HOME/Library/LaunchAgents/com.open-trader.trend-a-share-watch.plist"
fi
