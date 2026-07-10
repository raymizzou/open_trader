from __future__ import annotations

from open_trader.kelly_strategy_capital import (
    build_kelly_strategy_capital_payload,
    load_kelly_strategy_capital,
    write_kelly_strategy_capital,
)


def test_build_kelly_strategy_capital_payload_initializes_empty_experiment() -> None:
    payload = build_kelly_strategy_capital_payload(
        [
            {
                "experiment_id": "trend_us",
                "experiment_name": "趋势回调 US",
                "market": "us",
                "experiment_budget": "30000",
                "budget_currency": "uSd",
                "participants": [
                    {"market": "US", "symbol": "RAM"},
                    {"market": "US", "symbol": "SOXX"},
                ],
            }
        ],
        calculated_at="2026-07-10 21:00",
    )

    assert payload == {
        "schema_version": "open_trader.kelly_strategy_capital.v1",
        "calculated_at": "2026-07-10 21:00",
        "strategy_count": 1,
        "strategies": [
            {
                "experiment_id": "trend_us",
                "experiment_name": "趋势回调 US",
                "market": "US",
                "currency": "USD",
                "budget": "30000",
                "occupied_notional": "0",
                "position_notional": "0",
                "reserved_order_notional": "0",
                "available_notional": "30000",
                "utilization_pct": "0",
                "open_buy_order_count": 0,
                "realized_pnl": "0",
                "updated_at": "2026-07-10 21:00",
                "symbol_occupancy": [],
                "next_order_impact": {},
            }
        ],
    }


def test_build_kelly_strategy_capital_payload_counts_reserved_orders_and_positions() -> None:
    payload = build_kelly_strategy_capital_payload(
        [
            {
                "experiment_id": "trend_us",
                "experiment_name": "趋势回调 US",
                "market": "US",
                "experiment_budget": "30000",
                "budget_currency": "USD",
            }
        ],
        paper_orders_payload={
            "orders": [
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "RAM",
                    "side": "buy",
                    "status": "submitted",
                    "limit_price": "150",
                    "quantity": "8",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "SOXX",
                    "side": "buy",
                    "status": "filled",
                    "filled_avg_price": "620",
                    "filled_qty": "10",
                },
            ]
        },
        calculated_at="2026-07-10 21:05",
    )

    capital = payload["strategies"][0]
    assert capital["reserved_order_notional"] == "1200"
    assert capital["position_notional"] == "6200"
    assert capital["occupied_notional"] == "7400"
    assert capital["available_notional"] == "22600"
    assert capital["utilization_pct"] == "24.67"
    assert capital["open_buy_order_count"] == 1
    assert capital["symbol_occupancy"] == [
        {"market": "US", "symbol": "RAM", "notional": "1200"},
        {"market": "US", "symbol": "SOXX", "notional": "6200"},
    ]


def test_write_and_load_kelly_strategy_capital_roundtrips_payload(tmp_path) -> None:
    payload = build_kelly_strategy_capital_payload(
        [
            {
                "experiment_id": "trend_us",
                "experiment_name": "趋势回调 US",
                "market": "US",
                "experiment_budget": "30000",
                "budget_currency": "USD",
            }
        ],
        calculated_at="2026-07-10 21:00",
    )

    path = write_kelly_strategy_capital(tmp_path / "data", payload)

    assert path == tmp_path / "data" / "latest" / "kelly_strategy_capital.json"
    assert load_kelly_strategy_capital(tmp_path / "data") == payload
