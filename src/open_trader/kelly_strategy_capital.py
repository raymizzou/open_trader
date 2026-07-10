from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


STRATEGY_CAPITAL_SCHEMA_VERSION = "open_trader.kelly_strategy_capital.v1"

_OPEN_BUY_STATUSES = {
    "pending",
    "submitted",
    "submitting",
    "partially_filled",
    "partial_filled",
}


def build_kelly_strategy_capital_payload(
    experiments: list[dict[str, Any]],
    *,
    paper_orders_payload: dict[str, Any] | None = None,
    order_executions_payload: dict[str, Any] | None = None,
    calculated_at: str | None = None,
) -> dict[str, Any]:
    del order_executions_payload
    timestamp = calculated_at or _current_timestamp()
    orders_by_experiment = _capital_usage_by_experiment(paper_orders_payload or {})

    strategies: list[dict[str, Any]] = []
    for experiment in experiments:
        experiment_id = _field_text(experiment.get("experiment_id"))
        budget = _parse_decimal(experiment.get("experiment_budget"))
        usage = orders_by_experiment.get(experiment_id, _empty_usage())
        occupied = usage["reserved_order_notional"] + usage["position_notional"]
        available = budget - occupied
        symbol_occupancy = [
            {
                "market": market,
                "symbol": symbol,
                "notional": _decimal_text(notional),
            }
            for (market, symbol), notional in sorted(usage["symbol_occupancy"].items())
        ]
        strategies.append(
            {
                "experiment_id": experiment_id,
                "experiment_name": _field_text(experiment.get("experiment_name")),
                "market": _field_text(experiment.get("market")).upper(),
                "currency": _field_text(experiment.get("budget_currency")).upper(),
                "budget": _decimal_text(budget),
                "occupied_notional": _decimal_text(occupied),
                "position_notional": _decimal_text(usage["position_notional"]),
                "reserved_order_notional": _decimal_text(
                    usage["reserved_order_notional"]
                ),
                "available_notional": _decimal_text(available),
                "utilization_pct": _utilization_pct_text(occupied, budget),
                "open_buy_order_count": usage["open_buy_order_count"],
                "realized_pnl": "0",
                "updated_at": timestamp,
                "symbol_occupancy": symbol_occupancy,
                "next_order_impact": {},
            }
        )

    return {
        "schema_version": STRATEGY_CAPITAL_SCHEMA_VERSION,
        "calculated_at": timestamp,
        "strategy_count": len(strategies),
        "strategies": strategies,
    }


def write_kelly_strategy_capital(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / "latest" / "kelly_strategy_capital.json"
    _write_json_atomic(path, payload)
    return path


def load_kelly_strategy_capital(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "latest" / "kelly_strategy_capital.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if payload.get("schema_version") != STRATEGY_CAPITAL_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version must be {STRATEGY_CAPITAL_SCHEMA_VERSION!r}",
        )
    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        raise ValueError(f"{path.name} must contain a strategies list")
    return payload


def _capital_usage_by_experiment(
    paper_orders_payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    raw_orders = paper_orders_payload.get("orders", [])
    if not isinstance(raw_orders, list):
        return {}

    usage_by_experiment: dict[str, dict[str, Any]] = {}
    for order in raw_orders:
        if not isinstance(order, dict):
            continue
        if _field_text(order.get("side")).lower() != "buy":
            continue

        status = _field_text(order.get("status")).lower()
        experiment_id = _field_text(order.get("experiment_id"))
        usage = usage_by_experiment.setdefault(experiment_id, _empty_usage())
        market_symbol = (
            _field_text(order.get("market")).upper(),
            _field_text(order.get("symbol")).upper(),
        )

        if status in _OPEN_BUY_STATUSES:
            notional = _open_buy_order_notional(order)
            usage["reserved_order_notional"] += notional
            usage["open_buy_order_count"] += 1
        elif status == "filled":
            notional = _filled_buy_order_notional(order)
            usage["position_notional"] += notional
        else:
            continue

        if notional:
            usage["symbol_occupancy"][market_symbol] = (
                usage["symbol_occupancy"].get(market_symbol, Decimal("0")) + notional
            )

    return usage_by_experiment


def _empty_usage() -> dict[str, Any]:
    return {
        "reserved_order_notional": Decimal("0"),
        "position_notional": Decimal("0"),
        "open_buy_order_count": 0,
        "symbol_occupancy": {},
    }


def _open_buy_order_notional(order: dict[str, Any]) -> Decimal:
    price = _first_decimal(order, ("limit_price", "order_price", "price"))
    quantity = _first_decimal(order, ("quantity", "order_qty"))
    return price * quantity


def _filled_buy_order_notional(order: dict[str, Any]) -> Decimal:
    price = _first_decimal(
        order,
        (
            "filled_avg_price",
            "avg_fill_price",
            "avg_price",
            "limit_price",
            "order_price",
            "price",
        ),
    )
    quantity = _first_decimal(order, ("filled_qty", "quantity", "order_qty"))
    return price * quantity


def _first_decimal(order: dict[str, Any], keys: tuple[str, ...]) -> Decimal:
    for key in keys:
        value = order.get(key)
        if _field_text(value):
            parsed = _parse_optional_decimal(value)
            if parsed is not None:
                return parsed
    return Decimal("0")


def _parse_decimal(value: object) -> Decimal:
    parsed = _parse_optional_decimal(value)
    return parsed if parsed is not None else Decimal("0")


def _parse_optional_decimal(value: object) -> Decimal | None:
    text = _field_text(value)
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _field_text(value: object) -> str:
    return str(value or "").strip()


def _utilization_pct_text(occupied: Decimal, budget: Decimal) -> str:
    if budget == 0:
        return "0"
    pct = (occupied / budget * Decimal("100")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return _decimal_text(pct)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


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
