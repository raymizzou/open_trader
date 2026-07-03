from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from zoneinfo import ZoneInfo

from .market_scope import MarketScope, market_run_dir, market_scoped_latest_path
from .t_signal import TSignal


T_SIGNALS_CACHE_SCHEMA_VERSION = "open_trader.t_signals_cache.v1"


@dataclass(frozen=True)
class TSignalsArtifactResult:
    run_path: Path
    latest_path: Path
    records: int


def t_signals_run_path(data_dir: Path, run_date: str, market: str) -> Path:
    return market_run_dir(data_dir, run_date, MarketScope(market)) / "t_signals.json"


def t_signals_latest_path(data_dir: Path, market: str) -> Path:
    return market_scoped_latest_path(data_dir, MarketScope(market), "t_signals.json")


def write_t_signals_artifact(
    *,
    data_dir: Path,
    run_date: str,
    market: str,
    signals: list[TSignal],
    generated_at: str | None = None,
    update_latest: bool = True,
) -> TSignalsArtifactResult:
    normalized_market = MarketScope(market).value
    payload = {
        "schema_version": T_SIGNALS_CACHE_SCHEMA_VERSION,
        "generated_at": generated_at or _now_text(),
        "run_date": run_date,
        "market": normalized_market,
        "records": [signal.to_dict() for signal in signals],
    }
    run_path = t_signals_run_path(data_dir, run_date, normalized_market)
    latest_path = t_signals_latest_path(data_dir, normalized_market)
    _atomic_write_json(run_path, payload)
    if update_latest:
        _atomic_write_json(latest_path, payload)
    return TSignalsArtifactResult(
        run_path=run_path,
        latest_path=latest_path,
        records=len(signals),
    )


def load_t_signals_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def index_t_signals_by_market_symbol(
    cache: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    records = cache.get("records")
    if not isinstance(records, list):
        return {}
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        market = str(record.get("market") or "").strip().upper()
        symbol = str(record.get("symbol") or "").strip().upper()
        if not market or not symbol:
            continue
        indexed[(market, symbol)] = record
    return indexed


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
        temp_file.write("\n")
    try:
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _now_text() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
