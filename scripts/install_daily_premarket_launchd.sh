#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi

if [[ $# -ne 0 ]]; then
  echo "usage: $0 [--dry-run]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/config/daily_premarket.env"
TEMPLATE="$REPO_ROOT/ops/launchd/com.open-trader.premarket.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.open-trader.premarket.plist"

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
        print value
        exit
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

RENDERED="$(
  sed \
    -e "s#OPEN_TRADER_REPO#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_REPO")")#g" \
    -e "s#OPEN_TRADER_PYTHON#$(sed_replacement_escape "$(xml_escape "$OPEN_TRADER_PYTHON")")#g" \
    "$TEMPLATE"
)"

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf '%s\n' "$RENDERED"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$OPEN_TRADER_REPO/logs/daily_premarket"
printf '%s\n' "$RENDERED" > "$TARGET"
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"
echo "installed launchd agent: $TARGET"
