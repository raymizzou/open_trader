from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from open_trader.kelly_order_execution import (
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
            "futu_code": "US.RAM",
            "side": "buy",
            "order_type": "NORMAL",
            "price": "12.5",
            "qty": "80",
            "remark": "open_trader:trend:US:RAM:entry",
        },
        {
            "intent_id": "trend:HK:02840:exit",
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
