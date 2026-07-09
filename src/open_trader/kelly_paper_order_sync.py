from __future__ import annotations

import copy
import json
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from .kelly_lab import PAPER_ORDERS_SCHEMA_VERSION

TRD_ENV_SIMULATE = "SIMULATE"


class FutuPaperOrderSyncError(RuntimeError):
    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


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


class FutuSimulatePaperOrderClient:
    environment = TRD_ENV_SIMULATE
    source = "futu_simulate_paper_order_client"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        experiment_symbol_index: dict[tuple[str, str], str],
        context_factory: Callable[..., Any] = None,
        connectivity_checker: Callable[[str, int], bool] = None,
    ) -> None:
        connectivity_checker = connectivity_checker or _can_connect_to_opend
        context_factory = context_factory or _default_trade_context_factory
        if not connectivity_checker(host, port):
            raise FutuPaperOrderSyncError(
                f"Futu OpenD is not reachable at {host}:{port}. Start OpenD, log in, and check host/port.",
                error_type="opend_unreachable",
            )
        try:
            self.context = context_factory(host=host, port=port)
        except FutuPaperOrderSyncError:
            raise
        except Exception as exc:
            raise FutuPaperOrderSyncError(
                f"failed to create Futu trade context at {host}:{port}: {exc}",
                error_type="trade_context_failed",
            ) from exc
        self.host = host
        self.port = port
        self.experiment_symbol_index = experiment_symbol_index

    def list_orders(self) -> list[dict[str, Any]]:
        accounts = self._simulate_accounts()
        orders: list[dict[str, Any]] = []
        for account in accounts:
            ret_code, data = self.context.order_list_query(
                trd_env=TRD_ENV_SIMULATE,
                acc_id=account["acc_id"],
                acc_index=account["acc_index"],
                refresh_cache=True,
                order_market="N/A",
            )
            if ret_code != 0:
                raise FutuPaperOrderSyncError(
                    str(data),
                    error_type="order_query_failed",
                )
            for record in _records(data):
                order = _order_from_futu_record(record, self.experiment_symbol_index)
                if order is not None:
                    orders.append(order)
        return orders

    def close(self) -> None:
        self.context.close()

    def _simulate_accounts(self) -> list[dict[str, int]]:
        ret_code, data = self.context.get_acc_list()
        if ret_code != 0:
            raise FutuPaperOrderSyncError(
                str(data),
                error_type="account_query_failed",
            )
        accounts: list[dict[str, int]] = []
        for record in _records(data):
            trd_env = str(record.get("trd_env", "")).strip().upper()
            acc_status = str(record.get("acc_status", "ACTIVE")).strip().upper()
            if trd_env != TRD_ENV_SIMULATE or acc_status not in {"", "ACTIVE"}:
                continue
            accounts.append(
                {
                    "acc_id": _as_int(record.get("acc_id"), field_name="acc_id"),
                    "acc_index": _as_int(
                        record.get("acc_index", 0),
                        field_name="acc_index",
                    ),
                }
            )
        if not accounts:
            raise FutuPaperOrderSyncError(
                "no SIMULATE Futu securities accounts found",
                error_type="no_simulate_accounts",
            )
        return accounts


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


def load_kelly_experiment_symbol_index(data_dir: Path) -> dict[tuple[str, str], str]:
    path = data_dir / "latest" / "kelly_experiments.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    experiments = payload.get("experiments")
    if not isinstance(experiments, list):
        raise ValueError(f"{path.name} must contain an experiments list")

    experiment_ids_by_symbol: dict[tuple[str, str], set[str]] = {}
    for experiment in experiments:
        if not isinstance(experiment, dict):
            continue
        experiment_id = experiment.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            continue
        participants = experiment.get("participants")
        if not isinstance(participants, list):
            continue
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            market = participant.get("market")
            symbol = participant.get("symbol")
            if not isinstance(market, str) or not isinstance(symbol, str):
                continue
            key = (market.strip().upper(), symbol.strip().upper())
            if key[0] and key[1]:
                experiment_ids_by_symbol.setdefault(key, set()).add(experiment_id)

    return {
        key: next(iter(experiment_ids))
        for key, experiment_ids in experiment_ids_by_symbol.items()
        if len(experiment_ids) == 1
    }


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


def _can_connect_to_opend(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _default_trade_context_factory(*, host: str, port: int) -> Any:
    try:
        from futu import OpenSecTradeContext
    except ImportError as exc:
        raise FutuPaperOrderSyncError(
            "futu-api is not installed. Install it with: .venv/bin/python -m pip install futu-api",
            error_type="trade_context_failed",
        ) from exc
    return OpenSecTradeContext(host=host, port=port)


def _records(data: object) -> list[dict[str, object]]:
    if hasattr(data, "to_dict"):
        rows = data.to_dict("records")
        return [dict(row) for row in rows]
    raise FutuPaperOrderSyncError(
        f"Futu returned an unsupported table payload: {type(data).__name__}",
        error_type="trade_context_failed",
    )


def _as_int(value: object, *, field_name: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise FutuPaperOrderSyncError(
            f"Futu account field {field_name} is not an integer: {value!r}",
            error_type="account_query_failed",
        ) from exc


def _order_from_futu_record(
    record: dict[str, object],
    experiment_symbol_index: dict[tuple[str, str], str],
) -> dict[str, Any] | None:
    market_symbol = _market_symbol_from_futu_code(record.get("code"))
    if market_symbol is None:
        return None
    experiment_id = experiment_symbol_index.get(market_symbol)
    if experiment_id is None:
        return None
    market, symbol = market_symbol
    filled_qty = _first_text(record, ("dealt_qty", "filled_qty", "fill_qty"))
    avg_fill_price = _first_text(
        record,
        ("dealt_avg_price", "avg_fill_price", "avg_price"),
        "-",
    )
    if not avg_fill_price:
        avg_fill_price = "-"
    return {
        "experiment_id": experiment_id,
        "market": market,
        "symbol": symbol,
        "side": _normalize_side(_first_text(record, ("trd_side", "side"))),
        "submitted_at": _first_text(
            record,
            ("create_time", "submitted_at", "create_time_str"),
        ),
        "order_price": _first_text(record, ("price", "order_price")),
        "order_qty": _first_text(record, ("qty", "order_qty")),
        "filled_qty": filled_qty,
        "avg_fill_price": avg_fill_price,
        "status": _normalize_order_status(
            _first_text(record, ("order_status", "status")),
        ),
        "order_id": _first_text(record, ("order_id", "orderid")),
    }


def _market_symbol_from_futu_code(value: object) -> tuple[str, str] | None:
    text = str(value or "").strip().upper()
    if "." not in text:
        return None
    market, symbol = text.split(".", 1)
    if not market or not symbol:
        return None
    return market, symbol


def _first_text(
    record: dict[str, object],
    keys: tuple[str, ...],
    default: str = "",
) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _normalize_side(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {"BUY", "BUY_BACK"}:
        return "buy"
    if normalized in {"SELL", "SELL_SHORT"}:
        return "sell"
    return normalized.lower()


def _normalize_order_status(value: str) -> str:
    normalized = value.strip().upper()
    if normalized == "FILLED_ALL":
        return "filled"
    if normalized in {"FILLED_PART", "FILL_CANCELLED", "CANCELLED_PART"}:
        return "partial_filled"
    if normalized in {"SUBMITTED", "SUBMITTING", "WAITING_SUBMIT", "UNSUBMITTED"}:
        return "submitted"
    if normalized in {"CANCELLED_ALL", "CANCELLING_ALL", "CANCELLING_PART"}:
        return "cancelled"
    if normalized in {"FAILED", "SUBMIT_FAILED", "TIMEOUT", "DISABLED", "DELETED"}:
        return "rejected"
    return normalized.lower()
