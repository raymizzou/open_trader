from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from open_trader.kelly_order_execution import (
    ExecutorGuardedOrderClient,
    FutuOrderExecutionError,
    FutuSimulateOrderExecutionClient,
    MarketRoutingOrderExecutionClient,
    execute_kelly_orders_from_risk_checks,
    write_kelly_order_links_from_executions,
    write_kelly_order_executions,
)


class FakeOrderExecutionClient:
    environment = "SIMULATE"
    source = "fake_order_execution_client"

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def place_order(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "futu_order_id": f"SIM-{len(self.requests)}",
            "status": "submitted",
            "raw": {"order_id": f"SIM-{len(self.requests)}"},
        }


class FakeFutuExecutionContext:
    def __init__(self, *, host: str, port: int, trd_market: str = "HK") -> None:
        self.host = host
        self.port = port
        self.trd_market = trd_market
        self.place_calls: list[dict[str, object]] = []
        self.history_order_calls: list[dict[str, object]] = []

    def get_acc_list(self) -> object:
        return (
            0,
            FakeDataFrame(
                [
                    {
                        "acc_id": 12958916,
                        "acc_index": 0,
                        "trd_env": "SIMULATE",
                        "acc_status": "ACTIVE",
                    }
                ]
            ),
        )

    def close(self) -> None:
        pass

    def place_order(self, **kwargs: object) -> object:
        self.place_calls.append(dict(kwargs))
        return 0, FakeDataFrame([{"order_id": f"SIM-{len(self.place_calls)}"}])

    def accinfo_query(self, **kwargs: object) -> object:
        return 0, FakeDataFrame([{"total_assets": "100000", "cash": "90000"}])

    def position_list_query(self, **kwargs: object) -> object:
        return 0, FakeDataFrame([{"code": "SH.600001", "qty": "100"}])

    def order_list_query(self, **kwargs: object) -> object:
        return 0, FakeDataFrame([{"order_id": "SIM-1", "order_status": "FILLED_ALL"}])

    def history_order_list_query(self, **kwargs: object) -> object:
        self.history_order_calls.append(dict(kwargs))
        return 0, FakeDataFrame(
            [
                {
                    "order_id": "SIM-HISTORY-1",
                    "order_status": "FILLED_ALL",
                    "qty": "100",
                    "dealt_qty": "100",
                    "dealt_avg_price": "20.74",
                }
            ]
        )


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self._rows


def risk_payload() -> dict[str, Any]:
    return {
        "schema_version": "open_trader.kelly_order_risk_checks.v1",
        "checked_at": "2026-07-10 13:31",
        "max_entry_position_pct": "4",
        "intent_count": 4,
        "approved_count": 3,
        "blocked_count": 1,
        "checks": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "risk_status": "approved",
                "execution_status": "ready",
                "planned_notional": "1000",
                "budget_currency": "USD",
            },
            {
                "intent_id": "trend:HK:02840:exit",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "HK",
                "symbol": "02840",
                "intent_type": "exit",
                "side": "sell",
                "risk_status": "approved",
                "execution_status": "ready",
                "planned_notional": "",
                "budget_currency": "HKD",
            },
            {
                "intent_id": "breakout:US:TSM:entry",
                "experiment_id": "breakout",
                "experiment_name": "突破第一批",
                "strategy_id": "breakout_10d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "TSM",
                "intent_type": "entry",
                "side": "buy",
                "risk_status": "blocked",
                "execution_status": "risk_blocked",
                "planned_notional": "1200",
                "budget_currency": "USD",
            },
            {
                "intent_id": "trend:US:SOXX:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "SOXX",
                "intent_type": "entry",
                "side": "buy",
                "risk_status": "approved",
                "execution_status": "ready",
                "planned_notional": "100",
                "budget_currency": "USD",
            },
        ],
    }


def test_execute_kelly_orders_dry_run_builds_buy_request_and_skips_unready_items() -> None:
    payload = execute_kelly_orders_from_risk_checks(
        risk_payload(),
        dry_run=True,
        executed_at="2026-07-10 13:32",
        limit_prices={"US.RAM": "12.50", "US.SOXX": "250"},
        order_quantities={},
    )

    assert payload == {
        "schema_version": "open_trader.kelly_order_executions.v1",
        "environment": "DRY_RUN",
        "source": "dry_run",
        "executed_at": "2026-07-10 13:32",
        "execution_count": 4,
        "submitted_count": 0,
        "dry_run_count": 1,
        "skipped_count": 3,
        "failed_count": 0,
        "executions": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "RAM",
                "futu_code": "US.RAM",
                "side": "buy",
                "order_type": "NORMAL",
                "price": "12.5",
                "qty": "80",
                "planned_notional": "1000",
                "budget_currency": "USD",
                "execution_status": "dry_run",
                "submitted": False,
                "futu_order_id": "",
                "executed_at": "2026-07-10 13:32",
                "error": "",
            },
            {
                "intent_id": "trend:HK:02840:exit",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "HK",
                "symbol": "02840",
                "futu_code": "HK.02840",
                "side": "sell",
                "order_type": "NORMAL",
                "price": "",
                "qty": "",
                "planned_notional": "",
                "budget_currency": "HKD",
                "execution_status": "skipped",
                "submitted": False,
                "futu_order_id": "",
                "executed_at": "2026-07-10 13:32",
                "error": "missing limit price",
            },
            {
                "intent_id": "breakout:US:TSM:entry",
                "experiment_id": "breakout",
                "experiment_name": "突破第一批",
                "strategy_id": "breakout_10d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "TSM",
                "futu_code": "US.TSM",
                "side": "buy",
                "order_type": "NORMAL",
                "price": "",
                "qty": "",
                "planned_notional": "1200",
                "budget_currency": "USD",
                "execution_status": "skipped",
                "submitted": False,
                "futu_order_id": "",
                "executed_at": "2026-07-10 13:32",
                "error": "risk check is not ready",
            },
            {
                "intent_id": "trend:US:SOXX:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "SOXX",
                "futu_code": "US.SOXX",
                "side": "buy",
                "order_type": "NORMAL",
                "price": "250",
                "qty": "",
                "planned_notional": "100",
                "budget_currency": "USD",
                "execution_status": "skipped",
                "submitted": False,
                "futu_order_id": "",
                "executed_at": "2026-07-10 13:32",
                "error": "calculated quantity is less than 1",
            },
        ],
    }


def test_execute_kelly_orders_submits_ready_orders_with_client() -> None:
    client = FakeOrderExecutionClient()

    payload = execute_kelly_orders_from_risk_checks(
        risk_payload(),
        dry_run=False,
        client=client,
        executed_at="2026-07-10 13:32",
        limit_prices={"US.RAM": "12.50", "HK.02840": "3000"},
        order_quantities={"HK.02840": "1"},
    )

    assert client.requests == [
        {
            "intent_id": "trend:US:RAM:entry",
            "market": "US",
            "futu_code": "US.RAM",
            "side": "buy",
            "order_type": "NORMAL",
            "price": "12.5",
            "qty": "80",
            "remark": "open_trader:trend:US:RAM:entry",
        },
        {
            "intent_id": "trend:HK:02840:exit",
            "market": "HK",
            "futu_code": "HK.02840",
            "side": "sell",
            "order_type": "NORMAL",
            "price": "3000",
            "qty": "1",
            "remark": "open_trader:trend:HK:02840:exit",
        },
    ]
    assert payload["environment"] == "SIMULATE"
    assert payload["source"] == "fake_order_execution_client"
    assert payload["submitted_count"] == 2
    assert payload["skipped_count"] == 2
    assert payload["executions"][0]["execution_status"] == "submitted"
    assert payload["executions"][0]["futu_order_id"] == "SIM-1"
    assert payload["executions"][1]["execution_status"] == "submitted"
    assert payload["executions"][1]["futu_order_id"] == "SIM-2"


def test_futu_simulate_order_execution_client_uses_requested_trd_market() -> None:
    client = FutuSimulateOrderExecutionClient(
        host="127.0.0.1",
        port=11111,
        simulate_acc_id=12958916,
        trd_market="US",
        context_factory=FakeFutuExecutionContext,
        connectivity_checker=lambda host, port: True,
    )

    assert client.context.trd_market == "US"
    assert client.account == {"acc_id": 12958916, "acc_index": 0}


def test_futu_simulate_client_supports_market_order_and_keeps_limit_default() -> None:
    client = FutuSimulateOrderExecutionClient(
        host="127.0.0.1",
        port=11111,
        simulate_acc_id=12958916,
        trd_market="CN",
        context_factory=FakeFutuExecutionContext,
        connectivity_checker=lambda host, port: True,
    )

    client.place_order(
        {
            "side": "buy",
            "price": "0",
            "qty": "100",
            "futu_code": "SH.600001",
            "order_type": "MARKET",
            "remark": "trend:CN:2026-07-17:1",
        }
    )
    client.place_order(
        {
            "side": "buy",
            "price": "10.5",
            "qty": "100",
            "futu_code": "SH.600001",
        }
    )

    assert client.context.place_calls[0]["order_type"] == "MARKET"
    assert client.context.place_calls[0]["price"] == 0.0
    assert client.context.place_calls[1]["order_type"] == "NORMAL"
    assert client.context.place_calls[1]["price"] == 10.5

    snapshot = client.account_snapshot()
    assert snapshot["acc_id"] == 12958916
    assert snapshot["net_value"] == "100000"
    assert snapshot["positions"] == [{"code": "SH.600001", "qty": "100"}]
    assert client.list_orders(start="2026-07-17", end="2026-07-17")["orders"] == [
        {"order_id": "SIM-1", "order_status": "FILLED_ALL"},
        {
            "order_id": "SIM-HISTORY-1",
            "order_status": "FILLED_ALL",
            "qty": "100",
            "dealt_qty": "100",
            "dealt_avg_price": "20.74",
        }
    ]
    assert client.context.history_order_calls == [
        {
            "start": "2026-07-17",
            "end": "2026-07-17",
            "trd_env": "SIMULATE",
            "acc_id": 12958916,
            "acc_index": 0,
        }
    ]


def test_futu_list_orders_combines_active_and_history_without_duplicates() -> None:
    class ActiveAndHistoryContext(FakeFutuExecutionContext):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.active_order_calls: list[dict[str, object]] = []

        def order_list_query(self, **kwargs: object) -> object:
            self.active_order_calls.append(dict(kwargs))
            rows = [
                {
                    "order_id": "SIM-ACTIVE",
                    "order_status": "FILLED_PART",
                    "qty": "100",
                    "dealt_qty": "20",
                }
            ]
            if len(self.active_order_calls) > 1:
                rows.append({
                    "order_id": "SIM-NEW",
                    "order_status": "SUBMITTED",
                    "qty": "50",
                    "dealt_qty": "0",
                })
            return 0, FakeDataFrame(
                rows
            )

        def history_order_list_query(self, **kwargs: object) -> object:
            self.history_order_calls.append(dict(kwargs))
            return 0, FakeDataFrame(
                [
                    {
                        "order_id": "SIM-ACTIVE",
                        "order_status": "FILLED_PART",
                        "qty": "100",
                        "dealt_qty": "20",
                    },
                    {
                        "order_id": "SIM-TERMINAL",
                        "order_status": "FILLED_ALL",
                        "qty": "50",
                        "dealt_qty": "50",
                    },
                ]
            )

    client = FutuSimulateOrderExecutionClient(
        host="127.0.0.1",
        port=11111,
        simulate_acc_id=12958916,
        trd_market="CN",
        context_factory=ActiveAndHistoryContext,
        connectivity_checker=lambda host, port: True,
    )

    orders = client.list_orders(start="2026-07-20", end="2026-07-20")[
        "orders"
    ]
    refreshed = client.list_orders(start="2026-07-20", end="2026-07-20")[
        "orders"
    ]

    assert [order["order_id"] for order in orders] == [
        "SIM-ACTIVE",
        "SIM-TERMINAL",
    ]
    assert [order["order_id"] for order in refreshed] == [
        "SIM-ACTIVE",
        "SIM-NEW",
        "SIM-TERMINAL",
    ]
    assert len(client.context.active_order_calls) == 2
    assert len(client.context.history_order_calls) == 1


def test_executor_guard_delegates_reads_and_authorizes_every_mutation() -> None:
    delegate = FakeOrderExecutionClient()
    delegate.list_orders = lambda **kwargs: {"orders": [], **kwargs}
    authorizations: list[str] = []
    blocked = False

    def authorize() -> object:
        authorizations.append("checked")
        if blocked:
            raise RuntimeError("not the executor")
        return object()

    client = ExecutorGuardedOrderClient(delegate, authorize)

    assert client.list_orders(start="2026-07-20") == {
        "orders": [],
        "start": "2026-07-20",
    }
    client.place_order({"futu_code": "SH.600001"})
    client.place_order({"futu_code": "SH.600002"})
    blocked = True
    try:
        client.place_order({"futu_code": "SH.600003"})
    except RuntimeError as exc:
        assert str(exc) == "not the executor"
    else:
        raise AssertionError("expected executor authorization failure")

    assert authorizations == ["checked", "checked", "checked"]
    assert [request["futu_code"] for request in delegate.requests] == [
        "SH.600001",
        "SH.600002",
    ]


def test_futu_simulate_client_reports_history_order_query_failure() -> None:
    class FailedHistoryContext(FakeFutuExecutionContext):
        def history_order_list_query(self, **kwargs: object) -> object:
            return -1, "history unavailable"

    client = FutuSimulateOrderExecutionClient(
        host="127.0.0.1",
        port=11111,
        simulate_acc_id=12958916,
        trd_market="CN",
        context_factory=FailedHistoryContext,
        connectivity_checker=lambda host, port: True,
    )

    try:
        client.list_orders(start="2026-07-17", end="2026-07-17")
    except FutuOrderExecutionError as exc:
        assert exc.error_type == "history_order_list_query_failed"
        assert str(exc) == "history unavailable"
    else:
        raise AssertionError("expected FutuOrderExecutionError")


def test_futu_simulate_client_reports_active_order_query_failure() -> None:
    class FailedActiveContext(FakeFutuExecutionContext):
        def order_list_query(self, **kwargs: object) -> object:
            return -1, "active orders unavailable"

    client = FutuSimulateOrderExecutionClient(
        host="127.0.0.1",
        port=11111,
        simulate_acc_id=12958916,
        trd_market="CN",
        context_factory=FailedActiveContext,
        connectivity_checker=lambda host, port: True,
    )

    try:
        client.list_orders(start="2026-07-17", end="2026-07-17")
    except FutuOrderExecutionError as exc:
        assert exc.error_type == "order_list_query_failed"
        assert str(exc) == "active orders unavailable"
    else:
        raise AssertionError("expected FutuOrderExecutionError")


def test_market_routing_order_execution_client_routes_by_request_market() -> None:
    created_markets: list[str] = []

    class FakeMarketClient:
        environment = "SIMULATE"
        source = "fake_market_client"

        def __init__(self, **kwargs: object) -> None:
            self.trd_market = str(kwargs["trd_market"])
            self.requests: list[dict[str, object]] = []
            created_markets.append(self.trd_market)

        def place_order(self, request: dict[str, object]) -> dict[str, object]:
            self.requests.append(request)
            return {
                "futu_order_id": f"{self.trd_market}-1",
                "status": "submitted",
            }

        def close(self) -> None:
            pass

    client = MarketRoutingOrderExecutionClient(
        host="127.0.0.1",
        port=11111,
        client_factory=FakeMarketClient,
    )

    assert client.place_order({"market": "US", "futu_code": "US.RAM"})[
        "futu_order_id"
    ] == "US-1"
    assert client.place_order({"market": "HK", "futu_code": "HK.02840"})[
        "futu_order_id"
    ] == "HK-1"
    assert client.place_order({"market": "US", "futu_code": "US.SOXX"})[
        "futu_order_id"
    ] == "US-1"

    assert created_markets == ["US", "HK"]


def test_write_kelly_order_executions_writes_latest_artifact(tmp_path: Path) -> None:
    payload = {
        "schema_version": "open_trader.kelly_order_executions.v1",
        "environment": "DRY_RUN",
        "source": "dry_run",
        "executed_at": "2026-07-10 13:32",
        "execution_count": 0,
        "submitted_count": 0,
        "dry_run_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "executions": [],
    }

    path = write_kelly_order_executions(tmp_path / "data", payload)

    assert path == tmp_path / "data/latest/kelly_order_executions.json"
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_write_kelly_order_links_from_executions_indexes_submitted_orders(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "latest").mkdir(parents=True)
    (data_dir / "latest" / "kelly_order_links.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_order_links.v1",
                "links": [
                    {
                        "futu_order_id": "OLD-1",
                        "experiment_id": "old_exp",
                        "intent_id": "old_exp:US:OLD:entry",
                        "market": "US",
                        "symbol": "OLD",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    execution_payload = {
        "schema_version": "open_trader.kelly_order_executions.v1",
        "environment": "SIMULATE",
        "source": "fake_order_execution_client",
        "executed_at": "2026-07-10 13:32",
        "executions": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "market": "US",
                "symbol": "RAM",
                "side": "buy",
                "price": "12.5",
                "qty": "80",
                "execution_status": "submitted",
                "submitted": True,
                "futu_order_id": "SIM-1",
            },
            {
                "intent_id": "trend:HK:02840:exit",
                "experiment_id": "trend",
                "market": "HK",
                "symbol": "02840",
                "side": "sell",
                "price": "3000",
                "qty": "1",
                "execution_status": "skipped",
                "submitted": False,
                "futu_order_id": "",
            },
        ],
    }

    path = write_kelly_order_links_from_executions(data_dir, execution_payload)

    assert path == data_dir / "latest/kelly_order_links.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": "open_trader.kelly_order_links.v1",
        "updated_at": "2026-07-10 13:32",
        "links": [
            {
                "futu_order_id": "OLD-1",
                "experiment_id": "old_exp",
                "intent_id": "old_exp:US:OLD:entry",
                "market": "US",
                "symbol": "OLD",
            },
            {
                "futu_order_id": "SIM-1",
                "experiment_id": "trend",
                "intent_id": "trend:US:RAM:entry",
                "market": "US",
                "symbol": "RAM",
                "side": "buy",
                "price": "12.5",
                "qty": "80",
            },
        ],
    }
