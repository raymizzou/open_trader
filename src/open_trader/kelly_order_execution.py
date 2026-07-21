from __future__ import annotations

import json
import math
import os
import socket
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any, Protocol


ORDER_EXECUTIONS_SCHEMA_VERSION = "open_trader.kelly_order_executions.v1"
ORDER_RISK_CHECKS_SCHEMA_VERSION = "open_trader.kelly_order_risk_checks.v1"
ORDER_LINKS_SCHEMA_VERSION = "open_trader.kelly_order_links.v1"
TRD_ENV_SIMULATE = "SIMULATE"


class FutuOrderExecutionError(RuntimeError):
    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class OrderExecutionClient(Protocol):
    environment: str
    source: str

    def place_order(self, request: dict[str, Any]) -> dict[str, Any]:
        """Submit a normalized Kelly order request and return execution metadata."""


class ExecutorGuardedOrderClient:
    def __init__(self, delegate: object, authorize: Callable[[], object]) -> None:
        self._delegate = delegate
        self._authorize = authorize

    def place_order(self, request: dict[str, Any]) -> dict[str, Any]:
        self._authorize()
        return self._delegate.place_order(request)

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class FutuSimulateOrderExecutionClient:
    environment = TRD_ENV_SIMULATE
    source = "futu_simulate_order_execution_client"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        simulate_acc_id: int | None = None,
        trd_market: str = "HK",
        context_factory: Any = None,
        connectivity_checker: Any = None,
    ) -> None:
        connectivity_checker = connectivity_checker or _can_connect_to_opend
        context_factory = context_factory or _default_trade_context_factory
        if not connectivity_checker(host, port):
            raise FutuOrderExecutionError(
                f"Futu OpenD is not reachable at {host}:{port}. Start OpenD, log in, and check host/port.",
                error_type="opend_unreachable",
            )
        try:
            self.context = context_factory(
                host=host,
                port=port,
                trd_market=trd_market,
            )
        except FutuOrderExecutionError:
            raise
        except Exception as exc:
            raise FutuOrderExecutionError(
                f"failed to create Futu trade context at {host}:{port}: {exc}",
                error_type="trade_context_failed",
            ) from exc
        self.host = host
        self.port = port
        self.trd_market = trd_market
        self.account = self._select_simulate_account(simulate_acc_id)
        # Current orders are refreshed before every action. Cache only the
        # rate-limited history query for this short-lived reconciliation client.
        self._history_order_cache: dict[
            tuple[str | None, str | None], list[dict[str, Any]]
        ] = {}

    def place_order(self, request: dict[str, Any]) -> dict[str, Any]:
        side = str(request["side"]).strip().lower()
        trd_side = _futu_trade_side(side)
        order_type = str(request.get("order_type") or "NORMAL").strip().upper()
        if order_type not in {"NORMAL", "MARKET"}:
            raise ValueError("order_type must be NORMAL or MARKET")
        ret_code, data = self.context.place_order(
            price=0.0 if order_type == "MARKET" else float(request["price"]),
            qty=float(request["qty"]),
            code=request["futu_code"],
            trd_side=trd_side,
            order_type=order_type,
            trd_env=TRD_ENV_SIMULATE,
            acc_id=self.account["acc_id"],
            acc_index=self.account["acc_index"],
            remark=request.get("remark") or None,
        )
        if ret_code != 0:
            raise FutuOrderExecutionError(
                str(data),
                error_type="place_order_failed",
            )
        raw = _first_record_or_payload(data)
        return {
            "futu_order_id": _first_text(raw, ("order_id", "orderid")),
            "status": "submitted",
            "raw": raw,
        }

    def account_snapshot(self) -> dict[str, Any]:
        account_rows = self._query("accinfo_query")
        positions = self._query("position_list_query")
        raw = account_rows[0] if account_rows else {}
        return {
            "acc_id": self.account["acc_id"],
            "net_value": _first_text(
                raw, ("total_assets", "total_asset", "net_assets", "net_asset")
            ),
            "cash": _first_text(raw, ("cash", "cash_balance", "available_funds")),
            "positions": positions,
            "raw": raw,
        }

    def list_orders(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        active = self._query("order_list_query")
        kwargs: dict[str, object] = {
            "trd_env": TRD_ENV_SIMULATE,
            "acc_id": self.account["acc_id"],
            "acc_index": self.account["acc_index"],
        }
        if start is not None or end is not None:
            kwargs["start"] = start
            kwargs["end"] = end
        cache_key = (start, end)
        if cache_key not in self._history_order_cache:
            ret_code, data = self.context.history_order_list_query(**kwargs)
            if ret_code != 0:
                raise FutuOrderExecutionError(
                    str(data), error_type="history_order_list_query_failed"
                )
            self._history_order_cache[cache_key] = [
                dict(item) for item in _records(data)
            ]
        history = self._history_order_cache[cache_key]
        orders: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in [*active, *history]:
            order_id = _first_text(item, ("order_id", "orderid")).strip()
            identity = (
                ("id", order_id)
                if order_id
                else (
                    "json",
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ),
                )
            )
            if identity not in seen:
                seen.add(identity)
                orders.append(item)
        return {
            "acc_id": self.account["acc_id"],
            "orders": orders,
        }

    def _query(self, method_name: str) -> list[dict[str, Any]]:
        ret_code, data = getattr(self.context, method_name)(
            trd_env=TRD_ENV_SIMULATE,
            acc_id=self.account["acc_id"],
            acc_index=self.account["acc_index"],
        )
        if ret_code != 0:
            raise FutuOrderExecutionError(
                str(data), error_type=f"{method_name}_failed"
            )
        return [dict(item) for item in _records(data)]

    def close(self) -> None:
        self.context.close()

    def _select_simulate_account(
        self,
        simulate_acc_id: int | None,
    ) -> dict[str, int]:
        ret_code, data = self.context.get_acc_list()
        if ret_code != 0:
            raise FutuOrderExecutionError(
                str(data),
                error_type="account_query_failed",
            )
        accounts: list[dict[str, int]] = []
        for record in _records(data):
            trd_env = str(record.get("trd_env", "")).strip().upper()
            acc_status = str(record.get("acc_status", "ACTIVE")).strip().upper()
            if trd_env != TRD_ENV_SIMULATE or acc_status not in {"", "ACTIVE"}:
                continue
            account = {
                "acc_id": _as_int(record.get("acc_id"), field_name="acc_id"),
                "acc_index": _as_int(record.get("acc_index", 0), field_name="acc_index"),
            }
            accounts.append(account)
        if not accounts:
            raise FutuOrderExecutionError(
                "no SIMULATE Futu securities accounts found",
                error_type="no_simulate_accounts",
            )
        if simulate_acc_id is not None:
            for account in accounts:
                if account["acc_id"] == simulate_acc_id:
                    return account
            raise FutuOrderExecutionError(
                f"SIMULATE Futu account {simulate_acc_id} was not found",
                error_type="simulate_account_not_found",
            )
        if len(accounts) > 1:
            account_ids = ", ".join(str(account["acc_id"]) for account in accounts)
            raise FutuOrderExecutionError(
                "multiple SIMULATE Futu accounts found; pass --simulate-acc-id "
                f"with one of: {account_ids}",
                error_type="multiple_simulate_accounts",
            )
        return accounts[0]


class MarketRoutingOrderExecutionClient:
    environment = TRD_ENV_SIMULATE
    source = "market_routing_futu_simulate_order_execution_client"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        simulate_acc_id: int | None = None,
        client_factory: Any = None,
    ) -> None:
        self.host = host
        self.port = port
        self.simulate_acc_id = simulate_acc_id
        self.client_factory = client_factory or FutuSimulateOrderExecutionClient
        self.clients_by_market: dict[str, OrderExecutionClient] = {}

    def place_order(self, request: dict[str, Any]) -> dict[str, Any]:
        market = _request_market(request)
        client = self.clients_by_market.get(market)
        if client is None:
            client = self.client_factory(
                host=self.host,
                port=self.port,
                simulate_acc_id=self.simulate_acc_id,
                trd_market=market,
            )
            self.clients_by_market[market] = client
        return client.place_order(request)

    def close(self) -> None:
        for client in self.clients_by_market.values():
            close = getattr(client, "close", None)
            if callable(close):
                close()


def load_kelly_order_risk_checks(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "latest" / "kelly_order_risk_checks.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if payload.get("schema_version") != ORDER_RISK_CHECKS_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version must be {ORDER_RISK_CHECKS_SCHEMA_VERSION!r}",
        )
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise ValueError(f"{path.name} must contain a checks list")
    return payload


def execute_kelly_orders(
    data_dir: Path,
    *,
    dry_run: bool = True,
    executed_at: str | None = None,
    limit_prices: dict[str, str] | None = None,
    order_quantities: dict[str, str] | None = None,
    client: OrderExecutionClient | None = None,
) -> dict[str, Any]:
    return execute_kelly_orders_from_risk_checks(
        load_kelly_order_risk_checks(data_dir),
        dry_run=dry_run,
        executed_at=executed_at,
        limit_prices=limit_prices,
        order_quantities=order_quantities,
        client=client,
    )


def execute_kelly_orders_from_risk_checks(
    risk_payload: dict[str, Any],
    *,
    dry_run: bool = True,
    executed_at: str | None = None,
    limit_prices: dict[str, str] | None = None,
    order_quantities: dict[str, str] | None = None,
    client: OrderExecutionClient | None = None,
) -> dict[str, Any]:
    timestamp = executed_at or _current_timestamp()
    prices = _normalize_value_map(limit_prices or {})
    quantities = _normalize_value_map(order_quantities or {})
    raw_checks = risk_payload.get("checks")
    if not isinstance(raw_checks, list):
        raise ValueError("risk payload must contain a checks list")
    if not dry_run and client is None:
        raise ValueError("client is required when dry_run is false")

    executions: list[dict[str, Any]] = []
    for check in raw_checks:
        if not isinstance(check, dict):
            continue
        execution = _build_execution_record(
            check,
            dry_run=dry_run,
            client=client,
            executed_at=timestamp,
            limit_prices=prices,
            order_quantities=quantities,
        )
        executions.append(execution)

    return {
        "schema_version": ORDER_EXECUTIONS_SCHEMA_VERSION,
        "environment": "DRY_RUN" if dry_run else str(client.environment).strip().upper(),
        "source": "dry_run" if dry_run else str(client.source).strip(),
        "executed_at": timestamp,
        "execution_count": len(executions),
        "submitted_count": _count_status(executions, "submitted"),
        "dry_run_count": _count_status(executions, "dry_run"),
        "skipped_count": _count_status(executions, "skipped"),
        "failed_count": _count_status(executions, "failed"),
        "executions": executions,
    }


def write_kelly_order_executions(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / "latest" / "kelly_order_executions.json"
    writable_payload = {
        key: value for key, value in payload.items() if key != "latest_path"
    }
    _write_json_atomic(path, writable_payload)
    return path


def write_kelly_order_links_from_executions(
    data_dir: Path,
    execution_payload: dict[str, Any],
) -> Path:
    path = data_dir / "latest" / "kelly_order_links.json"
    existing_links = _load_existing_order_links(path)
    links_by_order_id = {
        str(link.get("futu_order_id", "")).strip(): dict(link)
        for link in existing_links
        if str(link.get("futu_order_id", "")).strip()
    }

    for execution in execution_payload.get("executions", []):
        if not isinstance(execution, dict):
            continue
        if execution.get("submitted") is not True:
            continue
        futu_order_id = str(execution.get("futu_order_id", "")).strip()
        experiment_id = str(execution.get("experiment_id", "")).strip()
        if not futu_order_id or not experiment_id:
            continue
        links_by_order_id[futu_order_id] = {
            "futu_order_id": futu_order_id,
            "experiment_id": experiment_id,
            "intent_id": str(execution.get("intent_id", "")).strip(),
            "market": str(execution.get("market", "")).strip().upper(),
            "symbol": str(execution.get("symbol", "")).strip().upper(),
            "side": str(execution.get("side", "")).strip().lower(),
            "price": str(execution.get("price", "")).strip(),
            "qty": str(execution.get("qty", "")).strip(),
        }

    payload = {
        "schema_version": ORDER_LINKS_SCHEMA_VERSION,
        "updated_at": str(execution_payload.get("executed_at", "")).strip(),
        "links": list(links_by_order_id.values()),
    }
    _write_json_atomic(path, payload)
    return path


def _load_existing_order_links(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if payload.get("schema_version") != ORDER_LINKS_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version must be {ORDER_LINKS_SCHEMA_VERSION!r}",
        )
    links = payload.get("links")
    if not isinstance(links, list):
        raise ValueError(f"{path.name} must contain a links list")
    return [dict(link) for link in links if isinstance(link, dict)]


def _build_execution_record(
    check: dict[str, Any],
    *,
    dry_run: bool,
    client: OrderExecutionClient | None,
    executed_at: str,
    limit_prices: dict[str, str],
    order_quantities: dict[str, str],
) -> dict[str, Any]:
    base = _base_execution(check, executed_at=executed_at)
    if not _is_ready_check(check):
        return _skipped_execution(base, "risk check is not ready")

    request = _build_order_request(
        check,
        limit_prices=limit_prices,
        order_quantities=order_quantities,
    )
    prepared = {
        **base,
        "price": request.get("price", ""),
        "qty": request.get("qty", ""),
    }
    if request.get("error"):
        return _skipped_execution(prepared, str(request["error"]))

    if dry_run:
        return {
            **prepared,
            "execution_status": "dry_run",
            "submitted": False,
            "futu_order_id": "",
            "error": "",
        }

    try:
        result = client.place_order(request) if client is not None else {}
    except Exception as exc:
        return {
            **prepared,
            "execution_status": "failed",
            "submitted": False,
            "futu_order_id": "",
            "error": str(exc),
        }
    return {
        **prepared,
        "execution_status": str(result.get("status", "submitted")).strip()
        or "submitted",
        "submitted": True,
        "futu_order_id": str(result.get("futu_order_id", "")).strip(),
        "error": "",
    }


def _base_execution(check: dict[str, Any], *, executed_at: str) -> dict[str, Any]:
    market = str(check.get("market", "")).strip().upper()
    symbol = str(check.get("symbol", "")).strip().upper()
    side = str(check.get("side", "")).strip().lower()
    return {
        "intent_id": str(check.get("intent_id", "")).strip(),
        "experiment_id": str(check.get("experiment_id", "")).strip(),
        "experiment_name": str(check.get("experiment_name", "")).strip(),
        "strategy_id": str(check.get("strategy_id", "")).strip(),
        "strategy_version": str(check.get("strategy_version", "")).strip(),
        "market": market,
        "symbol": symbol,
        "futu_code": f"{market}.{symbol}" if market and symbol else "",
        "side": side,
        "order_type": "NORMAL",
        "price": "",
        "qty": "",
        "planned_notional": str(check.get("planned_notional", "")).strip(),
        "budget_currency": str(check.get("budget_currency", "")).strip(),
        "executed_at": executed_at,
    }


def _build_order_request(
    check: dict[str, Any],
    *,
    limit_prices: dict[str, str],
    order_quantities: dict[str, str],
) -> dict[str, Any]:
    market = str(check.get("market", "")).strip().upper()
    symbol = str(check.get("symbol", "")).strip().upper()
    futu_code = f"{market}.{symbol}" if market and symbol else ""
    price = _parse_positive_decimal(limit_prices.get(futu_code))
    if price is None:
        return {"error": "missing limit price"}
    price_text = _decimal_text(price)

    side = str(check.get("side", "")).strip().lower()
    if side == "sell":
        qty = _parse_positive_decimal(order_quantities.get(futu_code))
        if qty is None:
            return {"price": price_text, "error": "missing order quantity"}
        qty = qty.to_integral_value(rounding=ROUND_FLOOR)
    else:
        planned_notional = _parse_positive_decimal(check.get("planned_notional"))
        if planned_notional is None:
            return {"price": price_text, "error": "missing planned notional"}
        qty = (planned_notional / price).to_integral_value(rounding=ROUND_FLOOR)

    if qty < 1:
        return {
            "price": price_text,
            "error": "calculated quantity is less than 1",
        }
    intent_id = str(check.get("intent_id", "")).strip()
    return {
        "intent_id": intent_id,
        "market": market,
        "futu_code": futu_code,
        "side": side,
        "order_type": "NORMAL",
        "price": price_text,
        "qty": _decimal_text(qty),
        "remark": f"open_trader:{intent_id}",
    }


def _request_market(request: dict[str, Any]) -> str:
    market = str(request.get("market", "")).strip().upper()
    if market:
        return market
    futu_code = str(request.get("futu_code", "")).strip().upper()
    if "." in futu_code:
        market = futu_code.split(".", 1)[0].strip()
    if market:
        return market
    raise FutuOrderExecutionError(
        "order request is missing market",
        error_type="missing_order_market",
    )


def _is_ready_check(check: dict[str, Any]) -> bool:
    return (
        str(check.get("risk_status", "")).strip() == "approved"
        and str(check.get("execution_status", "")).strip() == "ready"
    )


def _skipped_execution(base: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        **base,
        "execution_status": "skipped",
        "submitted": False,
        "futu_order_id": "",
        "error": error,
    }


def _normalize_value_map(values: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in values.items():
        normalized[str(key).strip().upper()] = str(value).strip()
    return normalized


def _parse_positive_decimal(value: object) -> Decimal | None:
    text = str(value or "").strip().rstrip("%")
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _count_status(executions: list[dict[str, Any]], status: str) -> int:
    return sum(
        1
        for execution in executions
        if execution.get("execution_status") == status
    )


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


def _can_connect_to_opend(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _default_trade_context_factory(*, host: str, port: int, trd_market: str = "HK") -> Any:
    try:
        from futu import OpenSecTradeContext
    except ImportError as exc:
        raise FutuOrderExecutionError(
            "futu-api is not installed. Install it with: .venv/bin/python -m pip install futu-api",
            error_type="trade_context_failed",
        ) from exc
    return OpenSecTradeContext(
        host=host,
        port=port,
        filter_trdmarket=_futu_trd_market(trd_market),
    )


def _futu_trade_side(side: str) -> str:
    try:
        from futu import TrdSide
    except ImportError as exc:
        raise FutuOrderExecutionError(
            "futu-api is not installed. Install it with: .venv/bin/python -m pip install futu-api",
            error_type="trade_context_failed",
        ) from exc
    if side == "buy":
        return TrdSide.BUY
    if side == "sell":
        return TrdSide.SELL
    raise FutuOrderExecutionError(
        f"unsupported order side: {side!r}",
        error_type="unsupported_side",
    )


def _futu_trd_market(trd_market: str) -> str:
    try:
        from futu import TrdMarket
    except ImportError as exc:
        raise FutuOrderExecutionError(
            "futu-api is not installed. Install it with: .venv/bin/python -m pip install futu-api",
            error_type="trade_context_failed",
        ) from exc
    key = str(trd_market).strip().upper()
    if key == "HK":
        return TrdMarket.HK
    if key == "US":
        return TrdMarket.US
    if key in {"CN", "A", "A_SHARE"}:
        return TrdMarket.CN
    raise FutuOrderExecutionError(
        f"unsupported trading market: {trd_market!r}",
        error_type="unsupported_trd_market",
    )


def _records(data: object) -> list[dict[str, object]]:
    if hasattr(data, "to_dict"):
        rows = data.to_dict("records")
        return [dict(row) for row in rows]
    raise FutuOrderExecutionError(
        f"Futu returned an unsupported table payload: {type(data).__name__}",
        error_type="trade_context_failed",
    )


def _first_record_or_payload(data: object) -> dict[str, object]:
    if hasattr(data, "to_dict"):
        records = _records(data)
        return records[0] if records else {}
    if isinstance(data, dict):
        return data
    return {"message": str(data)}


def _first_text(record: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and not _is_nan(value):
            text = str(value).strip()
            if text:
                return text
    return ""


def _is_nan(value: object) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _as_int(value: object, *, field_name: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise FutuOrderExecutionError(
            f"Futu account field {field_name} is not an integer: {value!r}",
            error_type="account_query_failed",
        ) from exc
