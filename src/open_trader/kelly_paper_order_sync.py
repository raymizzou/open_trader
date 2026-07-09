from __future__ import annotations

import copy
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .kelly_lab import PAPER_ORDERS_SCHEMA_VERSION


class PaperOrderClient(Protocol):
    environment: str
    source: str

    def list_orders(self) -> list[dict[str, Any]]:
        """Return paper orders in dashboard artifact shape."""


@dataclass(frozen=True)
class FakeFutuPaperOrderClient:
    orders: tuple[dict[str, Any], ...]
    environment: str = "SIMULATE"
    source: str = "fake_futu_paper_order_client"

    def list_orders(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(order) for order in self.orders]


def default_fake_kelly_paper_orders() -> tuple[dict[str, Any], ...]:
    return (
        {
            "experiment_id": "trend_pullback_20d_exp_20260707",
            "market": "US",
            "symbol": "RAM",
            "side": "buy",
            "submitted_at": "2026-07-08 10:01",
            "order_price": "12.34",
            "order_qty": "800",
            "filled_qty": "800",
            "avg_fill_price": "12.34",
            "status": "filled",
            "order_id": "SIM-10001",
        },
        {
            "experiment_id": "trend_pullback_20d_exp_20260707",
            "market": "US",
            "symbol": "SOXX",
            "side": "sell",
            "submitted_at": "2026-07-08 10:03",
            "order_price": "246.80",
            "order_qty": "20",
            "filled_qty": "0",
            "avg_fill_price": "-",
            "status": "submitted",
            "order_id": "SIM-10002",
        },
        {
            "experiment_id": "breakout_10d_exp_20260707",
            "market": "US",
            "symbol": "MSFT",
            "side": "buy",
            "submitted_at": "2026-07-08 10:04",
            "order_price": "498.20",
            "order_qty": "40",
            "filled_qty": "40",
            "avg_fill_price": "498.10",
            "status": "partial_filled",
            "order_id": "SIM-20001",
        },
    )


def sync_kelly_paper_orders(
    data_dir: Path,
    client: PaperOrderClient,
    *,
    synced_at: str | None = None,
) -> dict[str, Any]:
    environment = client.environment.strip().upper()
    if environment != "SIMULATE":
        raise ValueError("Kelly paper order sync only supports SIMULATE environment")

    orders = _validated_orders(client.list_orders())
    payload = {
        "schema_version": PAPER_ORDERS_SCHEMA_VERSION,
        "environment": environment,
        "source": client.source,
        "synced_at": synced_at or _current_timestamp(),
        "orders": orders,
    }
    _write_json_atomic(data_dir / "latest" / "kelly_paper_orders.json", payload)
    return payload


def _validated_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(orders, list):
        raise ValueError("paper order client must return an orders list")

    validated: list[dict[str, Any]] = []
    for index, order in enumerate(orders):
        if not isinstance(order, dict):
            raise ValueError(f"paper order {index} must be an object")
        experiment_id = order.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            raise ValueError(f"paper order {index} has invalid experiment_id")
        normalized = copy.deepcopy(order)
        normalized["experiment_id"] = experiment_id.strip()
        for key in ("market", "symbol", "side", "status"):
            if isinstance(normalized.get(key), str):
                normalized[key] = normalized[key].strip()
        validated.append(normalized)
    return validated


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _atomic_temp_path(path)
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _atomic_temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _current_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
