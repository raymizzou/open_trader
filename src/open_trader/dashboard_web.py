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
from .backtest_prices import DailyKlineProvider, fetch_backtest_prices
from .dashboard import DashboardConfig, load_dashboard_state
from .dashboard_account_sync import DashboardAccountSyncService
from .dashboard_quotes import DashboardQuoteService
from .futu_quote import FutuQuoteClient
from .research_chat import ResearchChatError, ResearchChatService
from .trading_plan import load_trading_plan_rows


STATIC_DIR = Path(__file__).with_name("dashboard_static")


def build_dashboard_payload(
    config: DashboardConfig,
    *,
    auto_fetch_backtest_prices: bool = False,
    backtest_price_provider: DailyKlineProvider | None = None,
) -> dict[str, Any]:
    backtest_price_sync: dict[str, Any] | None = None
    if auto_fetch_backtest_prices:
        backtest_price_sync = auto_fetch_missing_backtest_prices(
            config,
            provider=backtest_price_provider,
        )
    payload = load_dashboard_state(config).to_dict()
    if backtest_price_sync is not None:
        payload["backtest_price_sync"] = backtest_price_sync
    return payload


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


def build_backtest_prices_payload(
    config: DashboardConfig,
    request: dict[str, Any],
    *,
    provider: DailyKlineProvider | None = None,
) -> dict[str, Any]:
    market = str(request.get("market") or "").strip().upper()
    symbol = str(request.get("symbol") or "").strip().upper()
    if not market or not symbol:
        raise ValueError("market and symbol are required")

    plan_path = _dashboard_backtest_plan_path(config.data_dir, market)
    plan = _latest_active_plan(plan_path, market=market, symbol=symbol)
    end = str(request.get("end") or date.today().isoformat()).strip()
    start = str(request.get("start") or plan.run_date or end).strip()
    owned_provider = provider is None
    price_provider = provider or FutuQuoteClient(host=config.futu_host, port=config.futu_port)
    try:
        result = fetch_backtest_prices(
            data_dir=config.data_dir,
            market=market,
            symbol=symbol,
            start=start,
            end=end,
            provider=price_provider,
        )
    finally:
        if owned_provider and hasattr(price_provider, "close"):
            price_provider.close()

    return {
        "status": "ok",
        "market": result.market,
        "symbol": result.symbol,
        "start": result.start,
        "end": result.end,
        "records": result.records,
        "prices_path": str(result.prices_path),
        "backtest_readiness": _dashboard_backtest_readiness_for_holding(
            config,
            market=result.market,
            symbol=result.symbol,
        ),
    }


def auto_fetch_missing_backtest_prices(
    config: DashboardConfig,
    *,
    provider: DailyKlineProvider | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    payload = load_dashboard_state(config).to_dict()
    requests = _missing_backtest_price_requests(payload)
    if not requests:
        return {
            "status": "skipped",
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "errors": [],
        }

    owned_provider = provider is None
    price_provider = provider or FutuQuoteClient(host=config.futu_host, port=config.futu_port)
    fetch_end = end or date.today().isoformat()
    succeeded = 0
    errors: list[dict[str, str]] = []
    try:
        for request in requests:
            try:
                fetch_backtest_prices(
                    data_dir=config.data_dir,
                    market=request["market"],
                    symbol=request["symbol"],
                    start=request["start"] or fetch_end,
                    end=fetch_end,
                    provider=price_provider,
                )
                succeeded += 1
            except Exception as exc:
                errors.append(
                    {
                        "market": request["market"],
                        "symbol": request["symbol"],
                        "message": str(exc) or "auto backtest price fetch failed",
                    }
                )
                continue
    finally:
        if owned_provider and hasattr(price_provider, "close"):
            price_provider.close()
    failed = len(errors)
    status = "ok"
    if failed and succeeded:
        status = "partial"
    elif failed:
        status = "failed"
    return {
        "status": status,
        "attempted": len(requests),
        "succeeded": succeeded,
        "failed": failed,
        "errors": errors,
    }


def _missing_backtest_price_requests(payload: dict[str, Any]) -> list[dict[str, str]]:
    requests: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for holding in payload.get("holdings", []):
        if not isinstance(holding, dict):
            continue
        readiness = holding.get("backtest_readiness")
        if not isinstance(readiness, dict) or readiness.get("prices_missing") is not True:
            continue
        if readiness.get("status") in {"unsupported_strategy", "missing_plan"}:
            continue
        market = str(holding.get("market") or "").strip().upper()
        symbol = str(holding.get("symbol") or "").strip().upper()
        if not market or not symbol:
            continue
        key = (market, symbol)
        if key in seen:
            continue
        seen.add(key)
        requests.append(
            {
                "market": market,
                "symbol": symbol,
                "start": str(readiness.get("run_date") or "").strip(),
            }
        )
    return requests


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
    payload = build_dashboard_payload(config)
    for holding in payload.get("holdings", []):
        if (
            str(holding.get("market", "")).strip().upper() == market
            and str(holding.get("symbol", "")).strip().upper() == symbol
        ):
            backtest = holding.get("backtest")
            if isinstance(backtest, dict):
                return backtest
    return {"available": False, "error": "holding is not visible in dashboard"}


def _dashboard_backtest_readiness_for_holding(
    config: DashboardConfig,
    *,
    market: str,
    symbol: str,
) -> dict[str, Any]:
    payload = build_dashboard_payload(config)
    for holding in payload.get("holdings", []):
        if (
            str(holding.get("market", "")).strip().upper() == market
            and str(holding.get("symbol", "")).strip().upper() == symbol
        ):
            readiness = holding.get("backtest_readiness")
            if isinstance(readiness, dict):
                return readiness
    return {
        "available": False,
        "status": "missing_plan",
        "error": "holding is not visible in dashboard",
    }


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
                        build_dashboard_payload(
                            config,
                            auto_fetch_backtest_prices=True,
                            backtest_price_provider=backtest_price_provider,
                        )
                    )
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
                if path == "/api/backtests/prices":
                    self._send_json(
                        build_backtest_prices_payload(
                            config,
                            self._read_json_body(),
                            provider=backtest_price_provider,
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
            self._send_json(
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
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
