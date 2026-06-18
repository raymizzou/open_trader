from __future__ import annotations

import json
import threading
import urllib.request
from typing import Any

from open_trader.dashboard_quotes import QuoteRefreshResult
from open_trader.portfolio import PORTFOLIO_FIELDNAMES

from tests.test_dashboard import dashboard_config, portfolio_rows, write_csv


class FakeQuoteService:
    def __init__(self, result: QuoteRefreshResult) -> None:
        self.result = result
        self.refresh_count = 0

    def refresh(self) -> QuoteRefreshResult:
        self.refresh_count += 1
        return self.result


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

    assert dashboard_payload["summary"]["holding_count"] == 1
    assert dashboard_payload["holdings"][0]["symbol"] == "VIXY"
    assert quotes_payload["quotes"]["US.MSFT"]["last_price"] == "500"
    assert quote_service.refresh_count == 1
