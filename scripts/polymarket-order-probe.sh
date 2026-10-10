#!/usr/bin/env bash
# One manual standalone process; credentials are paths/backend references only.
set -euo pipefail
: "${OPEN_TRADER_PYTHON:?Set OPEN_TRADER_PYTHON to the absolute locked Python executable}"
if [[ "$OPEN_TRADER_PYTHON" != /* || ! -x "$OPEN_TRADER_PYTHON" ]]; then
  printf '%s\n' 'OPEN_TRADER_PYTHON must be an absolute executable path' >&2
  exit 2
fi
probe_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
export PYTHONPATH="$probe_root/src"
export PYTHONDONTWRITEBYTECODE=1
exec "$OPEN_TRADER_PYTHON" -B -m open_trader.polymarket_order_probe "$@"
