from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

from open_trader.dashboard_quotes import QuoteRefreshResult
from open_trader.dashboard_web import STATIC_DIR
from open_trader.portfolio import PORTFOLIO_FIELDNAMES

from tests.test_dashboard import dashboard_config, portfolio_rows, write_csv


class FakeQuoteService:
    def __init__(self, result: QuoteRefreshResult) -> None:
        self.result = result
        self.refresh_count = 0

    def refresh(self) -> QuoteRefreshResult:
        self.refresh_count += 1
        return self.result


class RaisingQuoteService:
    def refresh(self) -> QuoteRefreshResult:
        raise RuntimeError("boom")


def quote_result() -> QuoteRefreshResult:
    return QuoteRefreshResult(
        status="ok",
        requested_count=1,
        quote_count=1,
        missing_count=0,
        fetched_at="2026-06-19T09:30:00+08:00",
        last_success_at="2026-06-19T09:30:00+08:00",
        stale=False,
        quotes={
            "US.MSFT": {
                "market": "US",
                "symbol": "MSFT",
                "name": "Microsoft",
                "futu_symbol": "US.MSFT",
                "status": "ok",
                "last_price": "500",
                "fetched_at": "2026-06-19T09:30:00+08:00",
                "stale": False,
            }
        },
        diagnostic={},
    )


def read_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        content_length = response.headers["Content-Length"]
        payload = response.read()
        assert content_length == str(len(payload))
        return json.loads(payload.decode("utf-8"))


def read_error_json(url: str) -> tuple[int, str, dict[str, Any]]:
    try:
        urllib.request.urlopen(url, timeout=5)
    except urllib.error.HTTPError as error:
        payload = error.read()
        assert error.headers["Content-Length"] == str(len(payload))
        return (
            error.code,
            error.headers["Content-Type"],
            json.loads(payload.decode("utf-8")),
        )
    raise AssertionError("expected HTTPError")


def test_dashboard_static_assets_include_local_shell() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")

    assert "Open Trader" in html
    assert "持仓实时看板" in html
    assert "刷新行情" in html
    assert "全部市场" in html
    assert "缺行情" in js
    assert "数据已过期" in js
    assert "dashboardError" in js
    assert "scheduleQuotePolling" in js
    assert "Math.max(1000" in js
    assert ".dashboard-shell" in css


def test_build_dashboard_payload_returns_json_safe_state(tmp_path) -> None:
    from open_trader.dashboard_web import build_dashboard_payload

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])

    payload = build_dashboard_payload(config)

    json.dumps(payload)
    assert payload["summary"]["holding_count"] == 1
    assert len(payload["holdings"]) == 1
    assert payload["holdings"][0]["symbol"] == "VIXY"


def test_build_quotes_payload_returns_service_refresh() -> None:
    from open_trader.dashboard_web import build_quotes_payload

    service = FakeQuoteService(quote_result())

    payload = build_quotes_payload(service)

    json.dumps(payload)
    assert service.refresh_count == 1
    assert payload["status"] == "ok"
    assert list(payload["quotes"]) == ["US.MSFT"]
    assert payload["quotes"]["US.MSFT"]["last_price"] == "500"


def test_dashboard_server_serves_dashboard_and_quotes_api(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    quote_service = FakeQuoteService(quote_result())
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=quote_service,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        dashboard_payload = read_json(f"http://{host}:{port}/api/dashboard")
        quotes_payload = read_json(f"http://{host}:{port}/api/quotes")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert dashboard_payload["summary"]["holding_count"] == 1
    assert dashboard_payload["holdings"][0]["symbol"] == "VIXY"
    assert quotes_payload["quotes"]["US.MSFT"]["last_price"] == "500"
    assert quote_service.refresh_count == 1


def test_dashboard_server_returns_json_500_when_quotes_refresh_raises(
    tmp_path,
) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=RaisingQuoteService(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        status, content_type, payload = read_error_json(
            f"http://{host}:{port}/api/quotes"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert status == 500
    assert content_type == "application/json; charset=utf-8"
    assert payload == {
        "status": "error",
        "error_type": "RuntimeError",
        "message": "boom",
    }


def test_dashboard_server_returns_json_500_when_dashboard_payload_raises(
    tmp_path,
    monkeypatch,
) -> None:
    import open_trader.dashboard_web as dashboard_web

    def raise_runtime_error(config) -> dict[str, Any]:
        raise RuntimeError("dashboard boom")

    monkeypatch.setattr(
        dashboard_web,
        "build_dashboard_payload",
        raise_runtime_error,
    )
    config = dashboard_config(tmp_path)
    server = dashboard_web.create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        status, content_type, payload = read_error_json(
            f"http://{host}:{port}/api/dashboard"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert status == 500
    assert content_type == "application/json; charset=utf-8"
    assert payload == {
        "status": "error",
        "error_type": "RuntimeError",
        "message": "dashboard boom",
    }


def test_dashboard_server_serves_static_routes_when_files_exist(
    tmp_path,
    monkeypatch,
) -> None:
    import open_trader.dashboard_web as dashboard_web

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<main>dashboard</main>", encoding="utf-8")
    (static_dir / "dashboard.css").write_text("body{}", encoding="utf-8")
    (static_dir / "dashboard.js").write_text("console.log('ok');", encoding="utf-8")
    monkeypatch.setattr(dashboard_web, "STATIC_DIR", static_dir)

    config = dashboard_config(tmp_path)
    server = dashboard_web.create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "text/html; charset=utf-8"
            assert response.read().decode("utf-8") == "<main>dashboard</main>"
        with urllib.request.urlopen(
            f"http://{host}:{port}/static/dashboard.css",
            timeout=5,
        ) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "text/css; charset=utf-8"
            assert response.read().decode("utf-8") == "body{}"
        with urllib.request.urlopen(
            f"http://{host}:{port}/static/dashboard.js",
            timeout=5,
        ) as response:
            assert response.status == 200
            assert (
                response.headers["Content-Type"]
                == "application/javascript; charset=utf-8"
            )
            assert response.read().decode("utf-8") == "console.log('ok');"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
