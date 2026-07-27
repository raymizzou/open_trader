from __future__ import annotations

import json
import ipaddress
import os
import secrets
import subprocess
import threading
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse

from .backtest import run_backtest
from .backtest_prices import DailyKlineProvider, normalize_backtest_symbol
from .dashboard import (
    DETAIL_FX_TO_HKD,
    DashboardConfig,
    _backtest_holding_detail,
    _latest_backtests_by_holding,
    load_historical_trend_report,
    load_dashboard_state,
    load_trend_report_history,
)
from .dashboard_account_sync import DashboardAccountSyncService
from .dashboard_quotes import DashboardQuoteService
from .futu_quote import FutuQuoteClient
from .polymarket_monitor import PolymarketMonitor
from .polymarket_trading import PolymarketTradingClient, load_trading_config
from .daily_premarket import build_notifier
from .notifications import NullNotifier
from .prediction_arbitrage import (
    MAX_EMERGENCY_LOSS,
    MAX_NORMAL_COST,
    MAX_WALLET_BALANCE,
    MIN_ESTIMATED_PROFIT,
    MIN_NET_EDGE,
)
from .prediction_arbitrage_execution import PredictionExecutionService
from .prediction_arbitrage_store import PredictionArbitrageStore
from .research_chat import ResearchChatError, ResearchChatService
from .standard_strategies import strategy_catalog
from .statement_import import StatementImportService
from .strategy_backtest import (
    StandardBacktestRequest,
    run_standard_backtest,
    validate_standard_backtest_request,
)
from .trading_plan import load_trading_plan_rows
from .trend_simulate_positions import TrendSimulatePositionService


STATIC_DIR = Path(__file__).with_name("dashboard_static")
STANDARD_BACKTEST_RANGES = ("6M", "1Y", "3Y", "5Y", "CUSTOM")
STANDARD_BACKTEST_REQUEST_KEYS = {
    "market", "symbol", "strategy_id", "range_preset", "custom_start", "custom_end",
    "initial_cash", "max_strategy_weight", "commission_bps", "slippage_bps",
}
MAX_JSON_BODY_BYTES = 1024 * 1024
MAX_PDF_BODY_BYTES = 20 * 1024 * 1024


class RequestBodyTooLargeError(Exception):
    pass


class StandardBacktestExecutionError(RuntimeError):
    pass


PREDICTION_HISTORY_KINDS = {"signals", "executions", "incidents"}
PREDICTION_HISTORY_DEFAULT_LIMIT = 100
PREDICTION_HISTORY_MAX_LIMIT = 500


def _prediction_safe_value(value: object, *, key: str = "") -> object:
    """Convert monitor/store values to JSON without exposing credentials."""

    lowered = key.casefold()
    if any(part in lowered for part in ("private", "secret", "password", "credential", "signature")):
        return None
    if lowered in {"intent", "raw", "raw_markets", "raw_payload", "signed_order"}:
        return None
    if isinstance(value, datetime):
        return value.astimezone().isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for name, item in value.items():
            field = str(name)
            safe = _prediction_safe_value(item, key=field)
            if safe is not None:
                result[field] = safe
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_prediction_safe_value(item, key=key) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _prediction_mask_wallet(value: object) -> str:
    wallet = str(value or "").strip()
    if len(wallet) < 10:
        return ""
    return f"{wallet[:6]}…{wallet[-4:]}"


def _prediction_decimal_sort(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return Decimal("-Infinity")
    return parsed if parsed.is_finite() else Decimal("-Infinity")


def _prediction_sort_key(item: Mapping[str, object]) -> tuple[bool, Decimal, Decimal, str]:
    opportunities = item.get("opportunities")
    actionable = bool(item.get("actionable"))
    nested_profits: list[Decimal] = []
    nested_volumes: list[Decimal] = []
    if isinstance(opportunities, (list, tuple)):
        for row in opportunities:
            if not isinstance(row, Mapping):
                continue
            actionable = actionable or row.get("actionable") is True
            nested_profits.append(
                _prediction_decimal_sort(row.get("profit", row.get("minimum_profit")))
            )
            nested_volumes.append(_prediction_decimal_sort(row.get("volume_24h")))
    profit = item.get("profit", item.get("gross_upper_bound"))
    volume = item.get("volume_24h")
    if profit is None and nested_profits:
        profit = max(nested_profits)
    if volume is None and nested_volumes:
        volume = max(nested_volumes)
    return (
        not actionable,
        -_prediction_decimal_sort(profit),
        -_prediction_decimal_sort(volume),
        str(item.get("event_id") or ""),
    )


def _prediction_unavailable_state(csrf_token: str, reason: str = "configuration_unavailable") -> dict[str, object]:
    return {
        "status": "unavailable",
        "readiness": {"status": "unavailable", "reason": reason},
        "wallet": {"address": "", "masked_address": ""},
        "masked_wallet": "",
        "balances": {"p_usd": None, "allowance": None},
        "policy_limits": {
            "max_wallet_balance": format(MAX_WALLET_BALANCE, "f"),
            "max_normal_cost": format(MAX_NORMAL_COST, "f"),
            "min_estimated_profit": format(MIN_ESTIMATED_PROFIT, "f"),
            "min_net_edge": format(MIN_NET_EDGE, "f"),
            "max_emergency_loss": format(MAX_EMERGENCY_LOSS, "f"),
        },
        "heartbeat": None,
        "heartbeat_at": None,
        "events": [],
        "opportunities": [],
        "current_execution": None,
        "breaker": {"open": True, "status": "locked", "incident": None},
        "csrf_token": csrf_token,
    }


def _prediction_state_payload(
    *,
    store: PredictionArbitrageStore | None,
    monitor: object | None,
    execution: object | None,
    csrf_token: str,
) -> dict[str, object]:
    if monitor is None and store is None and execution is None:
        return _prediction_unavailable_state(csrf_token)
    snapshot: Mapping[str, object] = {}
    if monitor is not None:
        try:
            value = monitor.snapshot()
            if isinstance(value, Mapping):
                snapshot = value
        except Exception:
            snapshot = {}
    safe_snapshot = _prediction_safe_value(snapshot)
    if not isinstance(safe_snapshot, Mapping):
        safe_snapshot = {}
    readiness = safe_snapshot.get("readiness")
    if not isinstance(readiness, Mapping):
        readiness = {"status": "unavailable", "reason": "readiness_unavailable"}
    else:
        readiness = dict(readiness)
        for field in ("wallet_address", "wallet"):
            value = readiness.get(field)
            if isinstance(value, str) and value.startswith("0x"):
                readiness[field] = _prediction_mask_wallet(value)
    raw_readiness = snapshot.get("readiness")
    wallet_address = ""
    if isinstance(raw_readiness, Mapping):
        wallet_address = str(
            raw_readiness.get("wallet_address")
            or raw_readiness.get("wallet")
            or ""
        )
    if not wallet_address and execution is not None:
        trading = getattr(execution, "_trading", None)
        config = getattr(trading, "config", None)
        wallet_address = str(getattr(config, "wallet_address", "") or "")
    masked_wallet = _prediction_mask_wallet(wallet_address)
    try:
        active = store.active_execution() if store is not None else None
    except Exception:
        active = None
    try:
        incident = store.unacknowledged_incident() if store is not None else None
    except Exception:
        incident = None
    breaker_open = True
    breaker_method = getattr(execution, "_breaker_is_open", None)
    if callable(breaker_method):
        try:
            breaker_open = bool(breaker_method())
        except Exception:
            breaker_open = True
    elif execution is not None:
        breaker_open = bool(getattr(execution, "_breaker_open", True))
    events = safe_snapshot.get("events")
    opportunities = safe_snapshot.get("opportunities")
    event_rows = [row for row in events if isinstance(row, Mapping)] if isinstance(events, (list, tuple)) else []
    opportunity_rows = [row for row in opportunities if isinstance(row, Mapping)] if isinstance(opportunities, (list, tuple)) else []
    event_rows = sorted(event_rows, key=_prediction_sort_key)
    opportunity_rows = sorted(opportunity_rows, key=_prediction_sort_key)
    status = str(safe_snapshot.get("status") or "unavailable")
    if not snapshot:
        status = "unavailable"
    policy = _prediction_unavailable_state(csrf_token)["policy_limits"]
    balances = {
        "p_usd": readiness.get("p_usd_balance", readiness.get("balance")),
        "allowance": readiness.get("p_usd_allowance", readiness.get("allowance")),
    }
    return {
        "status": status,
        "readiness": dict(readiness),
        "wallet": {"address": "", "masked_address": masked_wallet},
        "masked_wallet": masked_wallet,
        "balances": balances,
        "policy_limits": policy,
        "heartbeat": safe_snapshot.get("heartbeat_at"),
        "heartbeat_at": safe_snapshot.get("heartbeat_at"),
        "events": event_rows,
        "opportunities": opportunity_rows,
        "current_execution": _prediction_safe_value(active),
        "breaker": {
            "open": breaker_open,
            "status": "locked" if breaker_open else "ready",
            "incident": _prediction_safe_value(incident),
        },
        "csrf_token": csrf_token,
    }


def _prediction_history_payload(
    store: PredictionArbitrageStore | None,
    *,
    kind: str,
    limit: int,
    offset: int,
) -> dict[str, object]:
    if kind not in PREDICTION_HISTORY_KINDS:
        raise ValueError("kind must be signals, executions, or incidents")
    rows = store.histories(kind) if store is not None else []
    safe_rows = [_prediction_safe_value(row) for row in rows]
    items = safe_rows[offset : offset + limit]
    return {
        "kind": kind,
        "items": items,
        "total": len(safe_rows),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < len(safe_rows),
    }


def build_standard_backtest_options_payload(config: DashboardConfig) -> dict[str, Any]:
    state = load_dashboard_state(config).to_dict()
    return {
        "strategies": [definition.to_dict() for definition in strategy_catalog()],
        "ranges": list(STANDARD_BACKTEST_RANGES),
        "defaults": {
            "range": "1Y", "initial_cash": "100000", "max_strategy_weight": "0.10",
            "commission_bps": "10", "slippage_bps": "5",
        },
        "universe": state["backtest_universe"],
        "benchmarks": {"US": "SPY", "HK": "HK.02800", "CN": "000300"},
    }


def _parse_decimal(request: dict[str, Any], key: str, default: str) -> Decimal:
    raw = str(request.get(key, default)).strip()
    percent = raw.endswith("%")
    if percent:
        raw = raw[:-1].strip()
    try:
        value = Decimal(raw)
    except Exception as exc:
        raise ValueError(f"{key} 必须是有效数字") from exc
    return value / Decimal("100") if percent else value


def _parse_iso_date(request: dict[str, Any], key: str) -> date | None:
    raw = str(request.get(key) or "").strip()
    if not raw:
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{key} 必须使用 YYYY-MM-DD 格式") from exc
    if parsed.isoformat() != raw:
        raise ValueError(f"{key} 必须使用 YYYY-MM-DD 格式")
    return parsed


def parse_standard_backtest_request(
    config: DashboardConfig, request: dict[str, Any]
) -> StandardBacktestRequest:
    unknown = set(request) - STANDARD_BACKTEST_REQUEST_KEYS
    if unknown:
        raise ValueError(f"不支持的请求字段：{', '.join(sorted(unknown))}")
    market = str(request.get("market") or "").strip().upper()
    symbol = str(request.get("symbol") or "").strip().upper()
    strategy_id = str(request.get("strategy_id") or "").strip()
    preset = str(request.get("range_preset") or "1Y").strip().upper()
    if preset not in STANDARD_BACKTEST_RANGES:
        raise ValueError(f"不支持的回测区间：{preset}")
    if strategy_id not in {item.strategy_id for item in strategy_catalog()}:
        raise ValueError(f"未知策略：{strategy_id}")
    custom_start = _parse_iso_date(request, "custom_start")
    custom_end = _parse_iso_date(request, "custom_end")
    if preset == "CUSTOM":
        if custom_start is None:
            raise ValueError("自定义区间必须提供开始日期")
        if custom_end is not None and custom_start >= custom_end:
            raise ValueError("开始日期必须早于结束日期")
    elif custom_start is not None or custom_end is not None:
        raise ValueError("预设区间不能同时提供自定义日期")
    options = build_standard_backtest_options_payload(config)
    universe = options["universe"]["holdings"] + options["universe"]["watchlist"]
    normalized = normalize_backtest_symbol(market, symbol)
    allowed = {
        (row["market"], row["symbol"].zfill(5) if row["market"] == "HK" and row["symbol"].isdigit() else row["symbol"])
        for row in universe
    }
    if (market, normalized) not in allowed:
        raise ValueError("所选标的不在可回测范围内")
    parsed = StandardBacktestRequest(
        data_dir=config.data_dir, reports_dir=config.reports_dir, market=market,
        symbol=normalized, strategy_id=strategy_id,
        range_preset=None if preset == "CUSTOM" else preset,
        custom_start=custom_start, custom_end=custom_end,
        initial_cash=_parse_decimal(request, "initial_cash", "100000"),
        max_strategy_weight=_parse_decimal(request, "max_strategy_weight", "0.10"),
        commission_bps=_parse_decimal(request, "commission_bps", "10"),
        slippage_bps=_parse_decimal(request, "slippage_bps", "5"),
    )
    validate_standard_backtest_request(parsed)
    return parsed


def build_standard_backtest_run_payload(
    config: DashboardConfig, request: dict[str, Any], *,
    provider: DailyKlineProvider | None = None,
) -> dict[str, Any]:
    if "adapter" in request:
        raise ValueError("不支持从界面选择回测执行工具")
    parsed = parse_standard_backtest_request(config, request)
    owned_provider = provider is None
    try:
        price_provider = provider or FutuQuoteClient(
            host=config.futu_host,
            port=config.futu_port,
        )
    except Exception as exc:
        raise StandardBacktestExecutionError(f"行情服务连接失败：{exc}") from exc
    result: dict[str, Any] | None = None
    primary_error: StandardBacktestExecutionError | None = None
    try:
        result = run_standard_backtest(parsed, price_provider=price_provider).to_dict()
    except Exception as exc:
        primary_error = StandardBacktestExecutionError(f"标准策略回测执行失败：{exc}")
        primary_error.__cause__ = exc
    if owned_provider and hasattr(price_provider, "close"):
        try:
            price_provider.close()
        except Exception as exc:
            if primary_error is None:
                raise StandardBacktestExecutionError(
                    f"行情服务关闭失败：{exc}"
                ) from exc
    if primary_error is not None:
        raise primary_error
    if result is None:  # pragma: no cover - defensive invariant
        raise StandardBacktestExecutionError("标准策略回测未返回结果")
    return result


def build_dashboard_payload(
    config: DashboardConfig,
) -> dict[str, Any]:
    return load_dashboard_state(config).to_dict()


def build_quotes_payload(
    quote_service: DashboardQuoteService,
    account_sync_service: DashboardAccountSyncService | None = None,
) -> dict[str, Any]:
    account_sync_payload = (
        account_sync_service.refresh_if_due().to_dict()
        if account_sync_service is not None
        else {}
    )
    payload = quote_service.refresh().to_dict()
    if account_sync_payload:
        payload["account_sync"] = account_sync_payload
    return payload


def build_backtest_run_payload(
    config: DashboardConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    market = str(request.get("market") or "").strip().upper()
    symbol = str(request.get("symbol") or "").strip().upper()
    if not market or not symbol:
        raise ValueError("market and symbol are required")

    plan_path = _dashboard_backtest_plan_path(config.data_dir, market)
    plan = _latest_active_plan(plan_path, market=market, symbol=symbol)
    prices_path = config.data_dir / "prices" / market / f"{symbol}.csv"
    run_backtest(
        plan_path=plan_path,
        prices_path=prices_path,
        data_dir=config.data_dir,
        reports_dir=config.reports_dir,
        run_date=plan.run_date,
        symbol=symbol,
        market=market,
        initial_cash=_decimal_request_value(request, "initial_cash", "100000"),
        initial_position_quantity=_decimal_request_value(
            request, "initial_position_quantity", "0"
        ),
        commission_bps=_decimal_request_value(request, "commission_bps", "10"),
        slippage_bps=_decimal_request_value(request, "slippage_bps", "5"),
        adapter=str(request.get("adapter") or "backtrader"),
    )
    backtest = _dashboard_backtest_for_holding(config, market=market, symbol=symbol)
    return {
        "status": "ok",
        "market": market,
        "symbol": symbol,
        "backtest": backtest,
    }


def _dashboard_backtest_plan_path(data_dir: Path, market: str) -> Path:
    scoped_path = data_dir / "latest" / market / "trading_plan.csv"
    if scoped_path.exists():
        return scoped_path
    return data_dir / "latest" / "trading_plan.csv"


def _latest_active_plan(plan_path: Path, *, market: str, symbol: str) -> Any:
    plans = [
        plan
        for plan in load_trading_plan_rows(plan_path)
        if plan.status == "active"
        and plan.market.upper() == market
        and plan.symbol.upper() == symbol
    ]
    if not plans:
        raise ValueError(f"no active trading plan found for {market}.{symbol}")
    plans.sort(key=lambda plan: (plan.run_date, plan.symbol))
    return plans[-1]


def _decimal_request_value(
    request: dict[str, Any],
    key: str,
    default: str,
) -> Decimal:
    value = request.get(key, default)
    return Decimal(str(value))


def _dashboard_backtest_for_holding(
    config: DashboardConfig,
    *,
    market: str,
    symbol: str,
) -> dict[str, Any]:
    rows = _latest_backtests_by_holding(
        data_dir=config.data_dir, reports_dir=config.reports_dir, markets={market},
    )
    return _backtest_holding_detail(rows.get((market, symbol)))


def create_dashboard_server(
    config: DashboardConfig,
    host: str,
    port: int,
    quote_service: DashboardQuoteService | None = None,
    account_sync_service: DashboardAccountSyncService | None = None,
    research_chat_service: ResearchChatService | None = None,
    backtest_price_provider: DailyKlineProvider | None = None,
    statement_import_service: StatementImportService | None = None,
    trend_simulate_position_service: TrendSimulatePositionService | None = None,
    eastmoney_password: str = "",
    prediction_store: PredictionArbitrageStore | None = None,
    prediction_monitor: object | None = None,
    prediction_execution_service: object | None = None,
    prediction_session_token: str | None = None,
    prediction_csrf_token: str | None = None,
) -> ThreadingHTTPServer:
    service = quote_service or DashboardQuoteService(config=config)
    chat_service = research_chat_service or ResearchChatService(data_dir=config.data_dir)
    import_service = statement_import_service or StatementImportService(
        data_dir=config.data_dir,
        reports_dir=config.reports_dir,
        portfolio_path=config.portfolio_path,
        eastmoney_password=eastmoney_password,
    )
    portfolio_update_lock = threading.Lock()
    prediction_session = prediction_session_token or secrets.token_urlsafe(32)
    prediction_csrf = prediction_csrf_token or secrets.token_urlsafe(32)

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return
            if path == "/static/dashboard.css":
                self._send_file(STATIC_DIR / "dashboard.css", "text/css; charset=utf-8")
                return
            if path == "/static/dashboard.js":
                self._send_file(
                    STATIC_DIR / "dashboard.js",
                    "application/javascript; charset=utf-8",
                )
                return
            if path == "/api/dashboard":
                try:
                    self._send_json(
                        build_dashboard_payload(config)
                    )
                except Exception as exc:
                    self._send_error_json(exc)
                return
            if path == "/api/prediction-arbitrage/state":
                self._send_prediction_state()
                return
            if path == "/api/prediction-arbitrage/history":
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    kind = str(query.get("kind", [""])[0]).strip()
                    limit = self._prediction_query_int(query, "limit", PREDICTION_HISTORY_DEFAULT_LIMIT)
                    offset = self._prediction_query_int(query, "offset", 0)
                    self._send_json(
                        _prediction_history_payload(
                            prediction_store,
                            kind=kind,
                            limit=limit,
                            offset=offset,
                        )
                    )
                except Exception as exc:
                    self._send_error_json(exc)
                return
            if path == "/api/backtests/options":
                try:
                    self._send_json(build_standard_backtest_options_payload(config))
                except Exception as exc:
                    self._send_error_json(exc)
                return
            if path == "/api/quotes":
                try:
                    with portfolio_update_lock:
                        self._send_json(
                            build_quotes_payload(
                                service,
                                account_sync_service=account_sync_service,
                            )
                        )
                except Exception as exc:
                    self._send_error_json(exc)
                return
            trend_reports_prefix = "/api/trend-reports/"
            if path.startswith(trend_reports_prefix):
                route = path.removeprefix(trend_reports_prefix).split("/", 2)
                try:
                    if len(route) == 2 and route[1] == "history":
                        self._send_json(
                            load_trend_report_history(
                                config.reports_dir, broker=route[0]
                            )
                        )
                        return
                    if len(route) == 3 and route[1] == "history":
                        try:
                            report = load_historical_trend_report(
                                config,
                                broker=route[0],
                                artifact=unquote(route[2]),
                            )
                        except FileNotFoundError as exc:
                            self._send_error_json(exc, HTTPStatus.NOT_FOUND)
                            return
                        self._send_json(report)
                        return
                except Exception as exc:
                    self._send_error_json(exc)
                    return
            trend_simulate_prefix = "/api/trend-simulate-positions/"
            if path.startswith(trend_simulate_prefix):
                broker = path.removeprefix(trend_simulate_prefix)
                if broker and "/" not in broker:
                    try:
                        if trend_simulate_position_service is None:
                            raise RuntimeError(
                                "trend simulate position service is unavailable"
                            )
                        self._send_json(
                            trend_simulate_position_service.load(broker)
                        )
                    except Exception as exc:
                        self._send_error_json(exc)
                    return
            session_id = self._research_chat_session_id(path)
            if session_id is not None:
                try:
                    self._send_json(chat_service.get_session(session_id))
                except Exception as exc:
                    self._send_error_json(exc)
                return
            self._send_not_found()

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                if path in {
                    "/api/prediction-arbitrage/preview",
                    "/api/prediction-arbitrage/executions",
                    "/api/prediction-arbitrage/circuit-breaker/reset",
                }:
                    self._require_prediction_mutation()
                    payload = self._read_json_body()
                    if prediction_execution_service is None:
                        raise RuntimeError("prediction execution service is unavailable")
                    if path.endswith("/preview"):
                        self._require_prediction_schema(payload, {"opportunity_id"})
                        opportunity_id = self._required_prediction_string(payload, "opportunity_id")
                        result = prediction_execution_service.preview(opportunity_id)
                    elif path.endswith("/executions"):
                        self._require_prediction_schema(payload, {"preview_id", "idempotency_key"})
                        preview_id = self._required_prediction_string(payload, "preview_id")
                        idempotency_key = self._required_prediction_string(payload, "idempotency_key")
                        result = prediction_execution_service.confirm(preview_id, idempotency_key)
                    else:
                        self._require_prediction_schema(payload, {"incident_id"})
                        incident_id = self._required_prediction_string(payload, "incident_id")
                        result = prediction_execution_service.reset_breaker(incident_id)
                    self._send_json(_prediction_safe_value(result))
                    return
                if path in {
                    "/api/statements/phillips",
                    "/api/statements/eastmoney",
                }:
                    if not _is_loopback_address(self.client_address[0]):
                        raise PermissionError("结单上传仅允许从本机访问")
                    broker = path.rsplit("/", 1)[-1]
                    with portfolio_update_lock:
                        self._send_json(
                            import_service.import_pdf(broker, self._read_pdf_body())
                        )
                    return
                if path == "/api/research-chat/sessions":
                    payload = self._read_json_body()
                    market = str(payload.get("market") or "")
                    symbol = str(payload.get("symbol") or "")
                    if not market or not symbol:
                        raise ResearchChatError("market and symbol are required")
                    self._send_json(
                        chat_service.create_session(
                            market=market,
                            symbol=symbol,
                        )
                    )
                    return
                if path == "/api/backtests/run":
                    self._send_json(
                        build_backtest_run_payload(config, self._read_json_body())
                    )
                    return
                if path == "/api/backtests/standard/run":
                    self._send_json(
                        build_standard_backtest_run_payload(
                            config, self._read_json_body(), provider=backtest_price_provider,
                        )
                    )
                    return
                if path.startswith("/api/research-chat/sessions/"):
                    route = self._research_chat_session_action(path)
                    if route is None:
                        self._send_not_found()
                        return
                    session_id, action = route
                    if action == "messages":
                        payload = self._read_json_body()
                        self._send_json(
                            chat_service.append_message(
                                session_id=session_id,
                                content=str(payload.get("content") or ""),
                            )
                        )
                        return
                    if action == "finalize":
                        self._read_json_body()
                        self._send_json(
                            chat_service.finalize_session(session_id=session_id)
                        )
                        return
            except Exception as exc:
                self._send_error_json(exc)
                return
            self._send_not_found()

        def _send_prediction_state(self) -> None:
            payload = _prediction_state_payload(
                store=prediction_store,
                monitor=prediction_monitor,
                execution=prediction_execution_service,
                csrf_token=prediction_csrf,
            )
            self.send_response(HTTPStatus.OK)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Set-Cookie",
                f"ot_prediction_session={prediction_session}; SameSite=Strict; HttpOnly; Path=/",
            )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        @staticmethod
        def _prediction_query_int(
            query: Mapping[str, list[str]], key: str, default: int
        ) -> int:
            raw = str(query.get(key, [str(default)])[0] or str(default))
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{key} must be a non-negative integer") from exc
            if value < 0 or (key == "limit" and value > PREDICTION_HISTORY_MAX_LIMIT):
                raise ValueError(f"{key} is outside the allowed range")
            if key == "limit" and value == 0:
                raise ValueError("limit must be positive")
            return value

        @staticmethod
        def _require_prediction_schema(
            payload: dict[str, Any], expected: set[str]
        ) -> None:
            if set(payload) != expected:
                raise ValueError("prediction request fields are invalid")

        @staticmethod
        def _required_prediction_string(payload: dict[str, Any], key: str) -> str:
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} is required")
            return value.strip()

        def _prediction_listener_host_header(self) -> str:
            address = self.server.server_address
            bound_host = str(address[0])
            bound_port = int(address[1])
            if ":" in bound_host and not bound_host.startswith("["):
                bound_host = f"[{bound_host}]"
            return f"{bound_host}:{bound_port}"

        def _require_prediction_mutation(self) -> None:
            try:
                if not _is_loopback_address(str(self.client_address[0])):
                    raise PermissionError("prediction mutations require loopback")
            except ValueError as exc:
                raise PermissionError("prediction mutations require loopback") from exc
            expected_host = self._prediction_listener_host_header()
            if self.headers.get("Host", "") != expected_host:
                raise PermissionError("prediction mutation Host is invalid")
            if self.headers.get("Origin", "") != f"http://{expected_host}":
                raise PermissionError("prediction mutation Origin is invalid")
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception as exc:
                raise PermissionError("prediction session is invalid") from exc
            provided_session = cookie.get("ot_prediction_session")
            if provided_session is None or not secrets.compare_digest(
                provided_session.value, prediction_session
            ):
                raise PermissionError("prediction session is invalid")
            provided_csrf = self.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(provided_csrf, prediction_csrf):
                raise PermissionError("prediction CSRF token is invalid")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json_body(self) -> dict[str, Any]:
            raw_content_length = self.headers.get("Content-Length") or "0"
            try:
                content_length = int(raw_content_length)
            except ValueError as exc:
                raise ValueError("Content-Length 必须是非负整数") from exc
            if content_length < 0:
                raise ValueError("Content-Length 必须是非负整数")
            if content_length > MAX_JSON_BODY_BYTES:
                raise RequestBodyTooLargeError("请求正文不能超过 1 MiB")
            body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("请求正文必须是有效的 JSON 对象") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求正文必须是有效的 JSON 对象")
            return payload

        def _read_pdf_body(self) -> bytes:
            if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/pdf":
                raise ValueError("请求正文必须是 PDF")
            raw_content_length = self.headers.get("Content-Length")
            if raw_content_length is None:
                raise ValueError("Content-Length 必须是非负整数")
            try:
                content_length = int(raw_content_length)
            except ValueError as exc:
                raise ValueError("Content-Length 必须是非负整数") from exc
            if content_length < 0:
                raise ValueError("Content-Length 必须是非负整数")
            if content_length > MAX_PDF_BODY_BYTES:
                raise RequestBodyTooLargeError("PDF 不能超过 20 MiB")
            body = self.rfile.read(content_length)
            if not body.startswith(b"%PDF-"):
                raise ValueError("请求正文必须是有效的 PDF")
            return body

        def _research_chat_session_id(self, path: str) -> str | None:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:3] == ["api", "research-chat", "sessions"]:
                return parts[3]
            return None

        def _research_chat_session_action(self, path: str) -> tuple[str, str] | None:
            parts = path.strip("/").split("/")
            if (
                len(parts) == 5
                and parts[:3] == ["api", "research-chat", "sessions"]
                and parts[3]
                and parts[4] in {"messages", "finalize"}
            ):
                return parts[3], parts[4]
            return None

        def _send_json(
            self,
            payload: Any,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_error_json(
            self, error: Exception, status: HTTPStatus | None = None
        ) -> None:
            if status is None:
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                if isinstance(error, RequestBodyTooLargeError):
                    status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                elif isinstance(error, PermissionError):
                    status = HTTPStatus.FORBIDDEN
                elif isinstance(error, ValueError):
                    status = HTTPStatus.BAD_REQUEST
                elif isinstance(error, StandardBacktestExecutionError):
                    status = HTTPStatus.BAD_GATEWAY
            self._send_json(
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                status=status,
            )

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                self._send_not_found()
                return

            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_not_found(self) -> None:
            body = b"not found"
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), DashboardRequestHandler)


def _is_loopback_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def _dashboard_runtime_metadata() -> dict[str, object]:
    cwd = Path.cwd().resolve()
    try:
        git_sha = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"], text=True
        ).strip()
        source_status = subprocess.check_output(
            [
                "git", "-C", str(cwd), "status", "--porcelain",
                "--untracked-files=all", "--", "src/open_trader",
            ],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        git_sha = ""
        source_status = "unavailable"
    return {
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(),
        "cwd": str(cwd),
        "git_sha": git_sha,
        "source_state": "clean" if not source_status else "dirty",
    }


def serve_dashboard(
    config: DashboardConfig,
    *,
    host: str,
    port: int,
    eastmoney_password: str = "",
    prediction_notifier: object | None = None,
) -> None:
    account_sync_service = DashboardAccountSyncService(config=config)
    trend_simulate_position_service = TrendSimulatePositionService(
        host=config.futu_host,
        port=config.futu_port,
        account_ids={
            "eastmoney": config.trend_review_cn_simulate_acc_id,
            "tiger": config.trend_review_us_simulate_acc_id,
            "phillips": config.trend_review_hk_simulate_acc_id,
        },
        fx_to_hkd=DETAIL_FX_TO_HKD,
        data_dir=config.data_dir,
        reports_dir=config.reports_dir,
    )
    prediction_store: PredictionArbitrageStore | None = None
    prediction_monitor: object | None = None
    prediction_execution: object | None = None
    prediction_trading: object | None = None
    if config.prediction_config_path is not None:
        prediction_path = config.prediction_config_path.expanduser()
        try:
            prediction_store = PredictionArbitrageStore(config.data_dir)
            prediction_trading = PolymarketTradingClient.from_keychain(
                load_trading_config(prediction_path)
            )
            prediction_monitor = PolymarketMonitor(
                store=prediction_store,
                trading=prediction_trading,
            )
            prediction_execution = PredictionExecutionService(
                store=prediction_store,
                monitor=prediction_monitor,
                trading=prediction_trading,
                notifier=prediction_notifier or NullNotifier(),
                lock_path=config.data_dir / "prediction_arbitrage" / "execution.lock",
            )
        except Exception:
            # A missing Keychain/config must leave a visible, schema-valid locked
            # Dashboard rather than aborting the existing portfolio surface.
            if prediction_monitor is not None:
                try:
                    prediction_monitor.stop()
                except Exception:
                    pass
            for resource in (prediction_execution, prediction_trading, prediction_store):
                close = getattr(resource, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            prediction_store = None
            prediction_monitor = None
            prediction_execution = None
            prediction_trading = None
        else:
            try:
                # Reconcile authenticated state before any public monitor heartbeat.
                prediction_execution.reconcile_startup()
            except Exception:
                # Keep the service visible and locked so the state API can expose
                # the failed startup rather than silently dropping its ledger.
                pass
            else:
                prediction_monitor.start()
    server = create_dashboard_server(
        config=config,
        host=host,
        port=port,
        account_sync_service=account_sync_service,
        trend_simulate_position_service=trend_simulate_position_service,
        eastmoney_password=eastmoney_password,
        prediction_store=prediction_store,
        prediction_monitor=prediction_monitor,
        prediction_execution_service=prediction_execution,
    )
    _, actual_port = server.server_address
    try:
        print(
            f"dashboard_runtime: {json.dumps(_dashboard_runtime_metadata())}",
            flush=True,
        )
        print(f"dashboard_url: http://{host}:{actual_port}", flush=True)
        print(f"portfolio: {config.portfolio_path}")
        print(f"futu: {config.futu_host}:{config.futu_port}")
        print(f"poll_seconds: {config.poll_seconds}")
        print(f"account_sync_seconds: {account_sync_service.interval_seconds}")
        server.serve_forever()
    finally:
        if prediction_monitor is not None:
            try:
                prediction_monitor.stop()
            except Exception:
                pass
        for resource in (prediction_execution, prediction_trading, prediction_store):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        server.server_close()
