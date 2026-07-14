#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
MARKET="all"
MARKET_REQUESTED=0

usage() {
  echo "usage: $0 [--dry-run] [--market HK|US|CN|all]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/config/daily_premarket.env"
TEMPLATE="$REPO_ROOT/ops/launchd/com.open-trader.premarket.plist.template"
CN_REPORT_TEMPLATE="$REPO_ROOT/ops/launchd/com.open-trader.trend-a-share-report.plist.template"
CN_WATCH_TEMPLATE="$REPO_ROOT/ops/launchd/com.open-trader.trend-a-share-watch.plist.template"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing required config: $ENV_FILE" >&2
  echo "copy config/daily_premarket.env.example to config/daily_premarket.env and fill local values" >&2
  exit 1
fi

read_env_value() {
  local key="$1"
  awk -v key="$key" '
    function trim(value) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      return value
    }
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    {
      stripped=trim($0)
      equals=index(stripped, "=")
      if (equals == 0) {
        next
      }
      parsed_key=trim(substr(stripped, 1, equals - 1))
      if (parsed_key == key) {
        value=trim(substr(stripped, equals + 1))
        if ((substr(value, 1, 1) == "\"" && substr(value, length(value), 1) == "\"") ||
            (substr(value, 1, 1) == "'"'"'" && substr(value, length(value), 1) == "'"'"'")) {
          value=substr(value, 2, length(value) - 2)
        }
        found=1
      }
    }
    END {
      if (found) {
        print value
      }
    }
  ' "$ENV_FILE"
}

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
  local value="$1"
  local repo="$2"
  value="$(expand_home_path "$value")"
  if [[ "$value" == /* ]]; then
    printf '%s' "$value"
  else
    printf '%s/%s' "$repo" "$value"
  fi
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
  echo "OPEN_TRADER_REPO and OPEN_TRADER_PYTHON are required in $ENV_FILE" >&2
  exit 1
fi

OPEN_TRADER_REPO="$(expand_home_path "$OPEN_TRADER_REPO")"
OPEN_TRADER_PYTHON="$(resolve_config_path "$OPEN_TRADER_PYTHON" "$OPEN_TRADER_REPO")"

markets=()
if [[ "$MARKET" == "all" ]]; then
  markets=("HK" "US")
elif [[ "$MARKET" == "CN" ]]; then
  markets=()
else
  markets=("$MARKET")
fi

render_market() {
  local market="$1"
  local label hour minute
  if [[ "$market" == "HK" ]]; then
    label="com.open-trader.premarket.hk"
    hour="8"
    minute="0"
  elif [[ "$market" == "US" ]]; then
    label="com.open-trader.premarket.us"
    hour="18"
    minute="30"
  else
    usage
    exit 2
  fi

  sed \
    -e "s#OPEN_TRADER_LABEL#$(sed_replacement_escape "$(xml_escape "$label")")#g" \
    -e "s#OPEN_TRADER_MARKET#$(sed_replacement_escape "$(xml_escape "$market")")#g" \
    -e "s#OPEN_TRADER_HOUR#$hour#g" \
    -e "s#OPEN_TRADER_MINUTE#$minute#g" \
    -e "s#OPEN_TRADER_REPO#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_REPO")")#g" \
    -e "s#OPEN_TRADER_PYTHON#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_PYTHON")")#g" \
    "$TEMPLATE"
}

lint_rendered() {
  local rendered="$1"
  local temp_path
  temp_path="$(mktemp "${TMPDIR:-/tmp}/open-trader-launchd.XXXXXX.plist")"
  printf '%s\n' "$rendered" > "$temp_path"
  plutil -lint "$temp_path" >/dev/null
  rm -f "$temp_path"
}

remove_legacy_agent() {
  local legacy_target="$HOME/Library/LaunchAgents/com.open-trader.premarket.plist"
  if [[ -f "$legacy_target" ]]; then
    launchctl unload "$legacy_target" 2>/dev/null || true
    rm "$legacy_target"
    echo "removed legacy launchd agent: $legacy_target"
  fi
}

render_cn_template() {
  local template="$1"
  sed \
    -e "s#OPEN_TRADER_REPO#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_REPO")")#g" \
    -e "s#OPEN_TRADER_PYTHON#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_PYTHON")")#g" \
    "$template"
}

render_cn_jobs() {
  local index label template rendered target
  local labels=(
    "com.open-trader.trend-a-share-report"
    "com.open-trader.trend-a-share-watch"
  )
  local templates=("$CN_REPORT_TEMPLATE" "$CN_WATCH_TEMPLATE")

  for index in 0 1; do
    label="${labels[$index]}"
    template="${templates[$index]}"
    rendered="$(render_cn_template "$template")"
    lint_rendered "$rendered"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '%s\n' "$rendered"
      continue
    fi

    target="$HOME/Library/LaunchAgents/$label.plist"
    mkdir -p "$HOME/Library/LaunchAgents" "$OPEN_TRADER_REPO/logs/daily_premarket"
    printf '%s\n' "$rendered" > "$target"
    plutil -lint "$target" >/dev/null
    launchctl unload "$target" 2>/dev/null || true
    launchctl load "$target"
    echo "installed launchd agent: $target"
  done
}

if [[ "$DRY_RUN" -eq 0 ]]; then
  remove_legacy_agent
fi

if [[ "$MARKET" != "CN" ]]; then
  for market in "${markets[@]}"; do
    rendered="$(render_market "$market")"
    lint_rendered "$rendered"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '%s\n' "$rendered"
      continue
    fi

    label="com.open-trader.premarket.$(printf '%s' "$market" | tr '[:upper:]' '[:lower:]')"
    target="$HOME/Library/LaunchAgents/$label.plist"
    mkdir -p "$HOME/Library/LaunchAgents" "$OPEN_TRADER_REPO/logs/daily_premarket"
    printf '%s\n' "$rendered" > "$target"
    plutil -lint "$target" >/dev/null
    launchctl unload "$target" 2>/dev/null || true
    launchctl load "$target"
    echo "installed launchd agent: $target"
  done
fi

if [[ "$MARKET_REQUESTED" -eq 1 && ( "$MARKET" == "CN" || "$MARKET" == "all" ) ]]; then
  render_cn_jobs
fi
