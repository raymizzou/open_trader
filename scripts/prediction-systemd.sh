#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$repo_root/src"
exec "${OPEN_TRADER_PYTHON:-python3.12}" -m open_trader.prediction_cloud "$@"
