#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
MARKET="all"
TREND_ONLY=0
CONFIG_PATH=""

usage() {
  echo "usage: $0 [--dry-run] [--trend-only] [--market HK|US|CN|all] [--config PATH]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --market)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      MARKET="$2"
      shift 2
      ;;
    --trend-only)
      TREND_ONLY=1
      shift
      ;;
    --config)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      CONFIG_PATH="$2"
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

expand_home_path() {
  local value="$1"
  if [[ "$value" == "~" ]]; then
    printf '%s' "$HOME"
  elif [[ "$value" == "~/"* ]]; then
    printf '%s/%s' "$HOME" "${value#"~/"}"
  else
    printf '%s' "$value"
  fi
}

resolve_config_path() {
  local value repo
  value="$(expand_home_path "$1")"
  repo="$2"
  if [[ "$value" == /* ]]; then
    printf '%s' "$value"
  else
    printf '%s/%s' "$repo" "$value"
  fi
}

if [[ -z "$CONFIG_PATH" ]]; then
  CONFIG_PATH="$REPO_ROOT/config/daily_premarket.env"
else
  CONFIG_PATH="$(resolve_config_path "$CONFIG_PATH" "$REPO_ROOT")"
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "missing required config: $CONFIG_PATH" >&2
  echo "copy config/daily_premarket.env.example to config/daily_premarket.env and fill local values" >&2
  exit 1
fi

read_env_value() {
  local key="$1" value first last
  value="$(awk -v key="$key" '
    function trim(value) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      return value
    }
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    {
      stripped=trim($0)
      equals=index(stripped, "=")
      if (equals == 0) next
      parsed_key=trim(substr(stripped, 1, equals - 1))
      if (parsed_key == key) {
        value=trim(substr(stripped, equals + 1))
        found=1
      }
    }
    END { if (found) print value }
  ' "$CONFIG_PATH")"
  if [[ "${#value}" -ge 2 ]]; then
    first="${value:0:1}"
    last="${value: -1}"
    if [[ "$first" == "$last" && ( "$first" == "'" || "$first" == '"' ) ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s\n' "$value"
}

xml_escape() {
  local value="$1"
  value="${value//&/&amp;}"
  value="${value//</&lt;}"
  value="${value//>/&gt;}"
  value="${value//\"/&quot;}"
  value="${value//\'/&apos;}"
  printf '%s' "$value"
}

sed_replacement_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//&/\\&}"
  value="${value//#/\\#}"
  printf '%s' "$value"
}

OPEN_TRADER_REPO="$(read_env_value OPEN_TRADER_REPO)"
OPEN_TRADER_PYTHON="$(read_env_value OPEN_TRADER_PYTHON)"
if [[ -z "$OPEN_TRADER_REPO" || -z "$OPEN_TRADER_PYTHON" ]]; then
  echo "OPEN_TRADER_REPO and OPEN_TRADER_PYTHON are required in $CONFIG_PATH" >&2
  exit 1
fi
OPEN_TRADER_REPO="$(expand_home_path "$OPEN_TRADER_REPO")"
OPEN_TRADER_PYTHON="$(resolve_config_path "$OPEN_TRADER_PYTHON" "$OPEN_TRADER_REPO")"

PREMARKET_TEMPLATE="$REPO_ROOT/ops/launchd/com.open-trader.premarket.plist.template"
CONTROLLER_TEMPLATE="$REPO_ROOT/ops/launchd/com.open-trader.trend-market-controller.plist.template"
PGREP_BIN="${PGREP_BIN:-pgrep}"

lint_rendered() {
  local rendered="$1" temp_path
  temp_path="$(mktemp "${TMPDIR:-/tmp}/open-trader-launchd.XXXXXX.plist")"
  printf '%s\n' "$rendered" > "$temp_path"
  plutil -lint "$temp_path" >/dev/null
  rm -f "$temp_path"
}

render_premarket() {
  local market="$1" label hour minute
  if [[ "$market" == "HK" ]]; then
    label="com.open-trader.premarket.hk"
    hour="8"
    minute="0"
  else
    label="com.open-trader.premarket.us"
    hour="18"
    minute="30"
  fi
  sed \
    -e "s#OPEN_TRADER_LABEL#$(sed_replacement_escape "$(xml_escape "$label")")#g" \
    -e "s#OPEN_TRADER_MARKET#$(sed_replacement_escape "$(xml_escape "$market")")#g" \
    -e "s#OPEN_TRADER_HOUR#$hour#g" \
    -e "s#OPEN_TRADER_MINUTE#$minute#g" \
    -e "s#OPEN_TRADER_REPO/config/daily_premarket.env#$(sed_replacement_escape "$(xml_escape "$CONFIG_PATH")")#g" \
    -e "s#OPEN_TRADER_REPO#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_REPO")")#g" \
    -e "s#OPEN_TRADER_PYTHON#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_PYTHON")")#g" \
    "$PREMARKET_TEMPLATE"
}

render_controller() {
  local market="$1" lower label
  lower="$(printf '%s' "$market" | tr '[:upper:]' '[:lower:]')"
  label="com.open-trader.trend-market-controller.$lower"
  sed \
    -e "s#OPEN_TRADER_LABEL#$(sed_replacement_escape "$(xml_escape "$label")")#g" \
    -e "s#OPEN_TRADER_MARKET_LOWER#$(sed_replacement_escape "$(xml_escape "$lower")")#g" \
    -e "s#OPEN_TRADER_MARKET#$(sed_replacement_escape "$(xml_escape "$market")")#g" \
    -e "s#OPEN_TRADER_CONFIG#$(sed_replacement_escape "$(xml_escape "$CONFIG_PATH")")#g" \
    -e "s#OPEN_TRADER_REPO#$(sed_replacement_escape "$(xml_escape "$REPO_ROOT")")#g" \
    -e "s#OPEN_TRADER_PYTHON#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_PYTHON")")#g" \
    "$CONTROLLER_TEMPLATE"
}

stop_label() {
  local label="$1" target
  target="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  if [[ -f "$target" ]]; then
    rm "$target"
    echo "removed launchd agent: $target"
  fi
}

verify_absent() {
  local label="$1"
  if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
    echo "legacy launchd job is still loaded: $label" >&2
    return 1
  fi
  echo "verified launchd label absent: $label"
}

legacy_labels() {
  local market="$1" lower
  if [[ "$market" == "CN" ]]; then
    printf '%s\n' \
      "com.open-trader.trend-a-share-report" \
      "com.open-trader.trend-a-share-watch"
  else
    lower="$(printf '%s' "$market" | tr '[:upper:]' '[:lower:]')"
    printf '%s\n' \
      "com.open-trader.trend-$lower-report" \
      "com.open-trader.trend-$lower-watch"
  fi
}

legacy_process_patterns() {
  local market="$1" python_pattern
  python_pattern='^([^[:space:]]*/)?[Pp]ython[^[:space:]]*[[:space:]]+-m[[:space:]]+open_trader[[:space:]]+'
  if [[ "$market" == "CN" ]]; then
    printf '%s\n' \
      "${python_pattern}trend-a-share-report([[:space:]]|$)" \
      "${python_pattern}watch-trend-a-share([[:space:]]|$)"
  else
    printf '%s\n' \
      "${python_pattern}trend-market-report[[:space:]]+--market[[:space:]]+$market([[:space:]]|$)" \
      "${python_pattern}watch-trend-market[[:space:]]+--market[[:space:]]+$market([[:space:]]|$)"
  fi
}

verify_legacy_processes_absent() {
  local market="$1" pattern attempt matches status running
  while IFS= read -r pattern; do
    running=0
    for attempt in 1 2 3 4 5; do
      if matches="$("$PGREP_BIN" -f "$pattern")"; then
        running=1
        if [[ "$attempt" -lt 5 ]]; then
          sleep 1
        fi
      else
        status=$?
        if [[ "$status" -ne 1 ]]; then
          echo "failed to inspect legacy trend processes for $market" >&2
          return 1
        fi
        running=0
        break
      fi
    done
    if [[ "$running" -eq 1 ]]; then
      echo "legacy trend process is still running for $market: $matches" >&2
      return 1
    fi
  done < <(legacy_process_patterns "$market")
}

install_rendered() {
  local label="$1" rendered="$2" target
  target="$HOME/Library/LaunchAgents/$label.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$REPO_ROOT/logs/daily_premarket"
  printf '%s\n' "$rendered" > "$target"
  plutil -lint "$target" >/dev/null
  launchctl load "$target"
  echo "installed launchd agent: $target"
}

if [[ "$TREND_ONLY" -eq 0 ]]; then
  ordinary_markets=()
  if [[ "$MARKET" == "all" ]]; then
    ordinary_markets=("HK" "US")
  elif [[ "$MARKET" == "HK" || "$MARKET" == "US" ]]; then
    ordinary_markets=("$MARKET")
  fi
  if [[ "$DRY_RUN" -eq 0 && "$MARKET" != "CN" ]]; then
    legacy_target="$HOME/Library/LaunchAgents/com.open-trader.premarket.plist"
    if [[ -f "$legacy_target" ]]; then
      launchctl unload "$legacy_target" 2>/dev/null || true
      rm "$legacy_target"
      echo "removed legacy launchd agent: $legacy_target"
    fi
  fi
  for market in "${ordinary_markets[@]}"; do
    rendered="$(render_premarket "$market")"
    lint_rendered "$rendered"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '%s\n' "$rendered"
    else
      label="com.open-trader.premarket.$(printf '%s' "$market" | tr '[:upper:]' '[:lower:]')"
      target="$HOME/Library/LaunchAgents/$label.plist"
      mkdir -p "$HOME/Library/LaunchAgents" "$OPEN_TRADER_REPO/logs/daily_premarket"
      printf '%s\n' "$rendered" > "$target"
      plutil -lint "$target" >/dev/null
      launchctl unload "$target" 2>/dev/null || true
      launchctl load "$target"
      echo "installed launchd agent: $target"
    fi
  done
  exit 0
fi

selected_markets=()
if [[ "$MARKET" == "all" ]]; then
  selected_markets=("CN" "HK" "US")
else
  selected_markets=("$MARKET")
fi

local_host="$(hostname)"
executor_host="$(read_env_value OPEN_TRADER_TREND_EXECUTOR_HOST)"
mode="readonly"
if [[ -n "$executor_host" && "$executor_host" == "$local_host" ]]; then
  mode="execute"
fi
echo "local host: $local_host"
echo "configured executor host: $executor_host"
echo "effective mode: $mode"

if [[ "$DRY_RUN" -eq 1 ]]; then
  if [[ "$mode" == "execute" ]]; then
    for market in "${selected_markets[@]}"; do
      rendered="$(render_controller "$market")"
      lint_rendered "$rendered"
      printf '%s\n' "$rendered"
    done
  fi
  exit 0
fi

cleanup_markets=("${selected_markets[@]}")
if [[ "$mode" == "readonly" ]]; then
  cleanup_markets=("CN" "HK" "US")
fi

for market in "${cleanup_markets[@]}"; do
  while IFS= read -r label; do
    stop_label "$label"
    verify_absent "$label"
  done < <(legacy_labels "$market")
done

for market in "${cleanup_markets[@]}"; do
  lower="$(printf '%s' "$market" | tr '[:upper:]' '[:lower:]')"
  label="com.open-trader.trend-market-controller.$lower"
  stop_label "$label"
  verify_absent "$label"
done

for market in "${cleanup_markets[@]}"; do
  verify_legacy_processes_absent "$market"
done

if [[ "$mode" == "readonly" ]]; then
  echo "readonly host: no trend controller installed"
  exit 0
fi

for market in "${selected_markets[@]}"; do
  lower="$(printf '%s' "$market" | tr '[:upper:]' '[:lower:]')"
  label="com.open-trader.trend-market-controller.$lower"
  rendered="$(render_controller "$market")"
  lint_rendered "$rendered"
  install_rendered "$label" "$rendered"
done
