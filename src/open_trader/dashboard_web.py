from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .backtest import run_backtest
from .backtest_prices import DailyKlineProvider
from .dashboard import (
    DashboardConfig,
    _backtest_holding_detail,
    _latest_backtests_by_holding,
    load_dashboard_state,
)
from .dashboard_account_sync import DashboardAccountSyncService
from .dashboard_quotes import DashboardQuoteService
from .futu_quote import FutuQuoteClient
from .research_chat import ResearchChatError, ResearchChatService
from .standard_strategies import strategy_catalog
from .strategy_backtest import (
    StandardBacktestRequest,
    run_standard_backtest,
    validate_standard_backtest_request,
)
from .trading_plan import load_trading_plan_rows


STATIC_DIR = Path(__file__).with_name("dashboard_static")
STANDARD_BACKTEST_RANGES = ("6M", "1Y", "3Y", "5Y", "CUSTOM")
STANDARD_BACKTEST_REQUEST_KEYS = {
    "market", "symbol", "strategy_id", "range_preset", "custom_start", "custom_end",
    "initial_cash", "max_strategy_weight", "commission_bps", "slippage_bps",
}


class StandardBacktestExecutionError(RuntimeError):
    pass


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
        "benchmarks": {"US": "SPY", "HK": "HK.02800"},
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
        if custom_start is None or custom_end is None:
            raise ValueError("自定义区间必须提供开始和结束日期")
        if custom_start > custom_end:
            raise ValueError("开始日期不能晚于结束日期")
    elif custom_start is not None or custom_end is not None:
        raise ValueError("预设区间不能同时提供自定义日期")
    options = build_standard_backtest_options_payload(config)
    universe = options["universe"]["holdings"] + options["universe"]["watchlist"]
    normalized = symbol.zfill(5) if market == "HK" and symbol.isdigit() else symbol
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
            host=config.futu_host, port=config.futu_port
        )
    except Exception as exc:
        raise StandardBacktestExecutionError(f"行情服务连接失败：{exc}") from exc
    try:
        try:
            return run_standard_backtest(parsed, price_provider=price_provider).to_dict()
        except Exception as exc:
            raise StandardBacktestExecutionError(f"标准策略回测执行失败：{exc}") from exc
    finally:
        if owned_provider and hasattr(price_provider, "close"):
            price_provider.close()


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
) -> ThreadingHTTPServer:
    service = quote_service or DashboardQuoteService(config=config)
    chat_service = research_chat_service or ResearchChatService(data_dir=config.data_dir)

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
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
            if path == "/api/backtests/options":
                try:
                    self._send_json(build_standard_backtest_options_payload(config))
                except Exception as exc:
                    self._send_error_json(exc)
                return
            if path == "/api/quotes":
                try:
                    self._send_json(
                        build_quotes_payload(
                            service,
                            account_sync_service=account_sync_service,
                        )
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

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json_body(self) -> dict[str, Any]:
            content_length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(content_length) if content_length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ResearchChatError("request body must be a JSON object")
            return payload

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
            payload: dict[str, Any],
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, error: Exception) -> None:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            if type(error) is ValueError:
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


def serve_dashboard(config: DashboardConfig, *, host: str, port: int) -> None:
    account_sync_service = DashboardAccountSyncService(config=config)
    server = create_dashboard_server(
        config=config,
        host=host,
        port=port,
        account_sync_service=account_sync_service,
    )
    _, actual_port = server.server_address
    try:
        print(f"dashboard_url: http://{host}:{actual_port}")
        print(f"portfolio: {config.portfolio_path}")
        print(f"futu: {config.futu_host}:{config.futu_port}")
        print(f"poll_seconds: {config.poll_seconds}")
        print(f"account_sync_seconds: {account_sync_service.interval_seconds}")
        server.serve_forever()
    finally:
        server.server_close()
