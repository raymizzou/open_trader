from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .kelly_lab import load_kelly_lab_state


ORDER_INTENTS_SCHEMA_VERSION = "open_trader.kelly_order_intents.v1"

_PENDING_STATUS_TO_INTENT = {
    "pending_entry_order": ("entry", "buy"),
    "pending_exit_order": ("exit", "sell"),
}


def build_kelly_order_intents(
    data_dir: Path,
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    state = load_kelly_lab_state(
        data_dir,
        include_strategy_capital=False,
    )
    if not state.available:
        raise ValueError(state.error or "Kelly Lab data is not available")
    return build_kelly_order_intents_payload(
        state.experiments,
        created_at=created_at,
    )


def build_kelly_order_intents_payload(
    experiments: list[dict[str, Any]],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    timestamp = created_at or _current_timestamp()
    intents: list[dict[str, Any]] = []

    for experiment in experiments:
        if not isinstance(experiment, dict):
            continue
        if str(experiment.get("status", "")).strip().lower() != "running":
            continue

        participants_by_key = _participants_by_key(experiment.get("participants"))
        experiment_market = str(experiment.get("market", "")).strip().upper()
        market_capital_pool = experiment.get("market_capital_pool")
        if not isinstance(market_capital_pool, dict):
            market_capital_pool = {}
        stats = experiment.get("stats")
        if not isinstance(stats, dict):
            stats = {}
        lifecycle_states = experiment.get("lifecycle_states")
        if not isinstance(lifecycle_states, list):
            continue

        for lifecycle_state in lifecycle_states:
            if not isinstance(lifecycle_state, dict):
                continue
            source_status = str(lifecycle_state.get("status", "")).strip()
            mapping = _PENDING_STATUS_TO_INTENT.get(source_status)
            if mapping is None:
                continue
            intent_type, side = mapping
            market = str(lifecycle_state.get("market", "")).strip().upper()
            symbol = str(lifecycle_state.get("symbol", "")).strip().upper()
            if not market or not symbol:
                continue
            if experiment_market and market != experiment_market:
                continue

            participant = participants_by_key.get((market, symbol), {})
            experiment_id = str(experiment.get("experiment_id", "")).strip()
            intents.append(
                {
                    "intent_id": f"{experiment_id}:{market}:{symbol}:{intent_type}",
                    "experiment_id": experiment_id,
                    "experiment_name": str(
                        experiment.get("experiment_name", "")
                    ).strip(),
                    "strategy_id": str(experiment.get("strategy_id", "")).strip(),
                    "strategy_version": str(
                        experiment.get("strategy_version", "")
                    ).strip(),
                    "experiment_market": experiment_market,
                    "market_capital_pool": copy.deepcopy(market_capital_pool),
                    "market": market,
                    "symbol": symbol,
                    "intent_type": intent_type,
                    "side": side,
                    "execution_status": "pending",
                    "risk_status": "not_checked",
                    "created_at": timestamp,
                    "source": "kelly_lifecycle",
                    "source_status": source_status,
                    "reason": str(lifecycle_state.get("reason", "")).strip(),
                    "action": str(lifecycle_state.get("action", "")).strip(),
                    "suggested_position_pct": str(
                        stats.get("suggested_position_pct", "")
                    ).strip(),
                    "parameter_source": str(
                        stats.get("parameter_source", "")
                    ).strip(),
                    "strategy_stats_generated_at": str(
                        stats.get("last_recomputed_at", "")
                    ).strip(),
                    "strategy_stats_source_samples_generated_at": str(
                        stats.get("source_trade_samples_generated_at", "")
                    ).strip(),
                    "per_symbol_budget": str(
                        participant.get("per_symbol_budget", "")
                    ).strip(),
                    "budget_currency": str(
                        participant.get("budget_currency")
                        or experiment.get("budget_currency", "")
                    ).strip(),
                }
            )

    return {
        "schema_version": ORDER_INTENTS_SCHEMA_VERSION,
        "created_at": timestamp,
        "intent_count": len(intents),
        "intents": intents,
    }


def write_kelly_order_intents(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / "latest" / "kelly_order_intents.json"
    _write_json_atomic(path, payload)
    return path


def _participants_by_key(value: object) -> dict[tuple[str, str], dict[str, Any]]:
    if not isinstance(value, list):
        return {}

    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        market = str(item.get("market", "")).strip().upper()
        symbol = str(item.get("symbol", "")).strip().upper()
        if market and symbol:
            indexed[(market, symbol)] = item
    return indexed


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _current_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
