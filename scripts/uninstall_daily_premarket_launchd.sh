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
      [[ $# -ge 2 ]] || { usage; exit 2; }
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

remove_label() {
  local label="$1" target
  target="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
    echo "launchd job is still loaded: $label; preserving $target" >&2
    return 1
  fi
  if [[ -f "$target" ]]; then
    rm "$target"
    echo "removed launchd agent: $target"
  else
    echo "launchd agent not installed: $target"
  fi
}

remove_trend_market() {
  local market="$1" lower
  lower="$(printf '%s' "$market" | tr '[:upper:]' '[:lower:]')"
  remove_label "com.open-trader.trend-market-controller.$lower"
  if [[ "$market" == "CN" ]]; then
    remove_label "com.open-trader.trend-a-share-report"
    remove_label "com.open-trader.trend-a-share-watch"
  else
    remove_label "com.open-trader.trend-$lower-report"
    remove_label "com.open-trader.trend-$lower-watch"
  fi
}

if [[ "$TREND_ONLY" -eq 0 ]]; then
  if [[ "$MARKET" == "all" || "$MARKET" == "HK" ]]; then
    remove_label "com.open-trader.premarket.hk"
  fi
  if [[ "$MARKET" == "all" || "$MARKET" == "US" ]]; then
    remove_label "com.open-trader.premarket.us"
  fi
  if [[ "$MARKET" == "all" ]]; then
    remove_label "com.open-trader.premarket"
  fi
fi

if [[ "$TREND_ONLY" -eq 1 || "$MARKET_REQUESTED" -eq 1 ]]; then
  if [[ "$MARKET" == "all" ]]; then
    trend_markets=("CN" "HK" "US")
  else
    trend_markets=("$MARKET")
  fi
  for market in "${trend_markets[@]}"; do
    remove_trend_market "$market"
  done
fi
