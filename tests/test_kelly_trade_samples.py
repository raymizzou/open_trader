from __future__ import annotations

from open_trader.kelly_trade_samples import build_kelly_trade_samples_payload


def _experiment(experiment_id: str = "trend_us") -> dict[str, object]:
    return {
        "experiment_id": experiment_id,
        "experiment_name": "Trend US",
        "market": "US",
        "stats": {},
        "participants": [
            {
                "market": "US",
                "symbol": "AAPL",
                "name": "Apple",
                "source": "watchlist",
                "locked": True,
                "per_symbol_budget": "10000",
                "budget_currency": "USD",
            }
        ],
    }


def test_build_trade_samples_pairs_filled_buy_and_sell_as_win() -> None:
    payload = build_kelly_trade_samples_payload(
        [_experiment()],
        {
            "schema_version": "open_trader.kelly_paper_orders.v1",
            "synced_at": "2026-07-11 09:30",
            "orders": [
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:31",
                    "filled_qty": "10",
                    "avg_fill_price": "100",
                    "status": "filled",
                    "order_id": "BUY-1",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "sell",
                    "submitted_at": "2026-07-12 10:00",
                    "filled_qty": "10",
                    "avg_fill_price": "106",
                    "status": "filled",
                    "order_id": "SELL-1",
                },
            ],
        },
        generated_at="2026-07-12 10:01",
    )

    assert payload["schema_version"] == "open_trader.kelly_trade_samples.v1"
    assert payload["sample_count"] == 1
    assert payload["open_position_count"] == 0
    assert payload["skipped_order_count"] == 0
    assert payload["source_orders_synced_at"] == "2026-07-11 09:30"
    assert payload["samples"] == [
        {
            "experiment_id": "trend_us",
            "market": "US",
            "symbol": "AAPL",
            "entry_order_id": "BUY-1",
            "exit_order_id": "SELL-1",
            "entry_submitted_at": "2026-07-11 09:31",
            "exit_submitted_at": "2026-07-12 10:00",
            "entry_price": "100",
            "exit_price": "106",
            "quantity": "10",
            "entry_notional": "1000",
            "exit_notional": "1060",
            "gross_pnl": "60",
            "net_pnl_pct": "6%",
            "result": "win",
        }
    ]
    assert payload["stats_by_experiment"]["trend_us"]["completed_samples"] == 1
    assert payload["stats_by_experiment"]["trend_us"]["winning_samples"] == 1
    assert (
        payload["stats_by_experiment"]["trend_us"]["parameter_source"]
        == "futu_paper_order_samples"
    )


def test_build_trade_samples_keeps_unmatched_buy_as_open_position() -> None:
    payload = build_kelly_trade_samples_payload(
        [_experiment()],
        {
            "schema_version": "open_trader.kelly_paper_orders.v1",
            "synced_at": "2026-07-11 09:30",
            "orders": [
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:31",
                    "filled_qty": "10",
                    "avg_fill_price": "100",
                    "status": "filled",
                    "order_id": "BUY-1",
                }
            ],
        },
        generated_at="2026-07-12 10:01",
    )

    assert payload["sample_count"] == 0
    assert payload["open_position_count"] == 1
    assert payload["open_positions"][0]["entry_order_id"] == "BUY-1"
    assert payload["stats_by_experiment"]["trend_us"]["completed_samples"] == 0
    assert payload["stats_by_experiment"]["trend_us"]["open_samples"] == 1
    assert payload["stats_by_experiment"]["trend_us"]["suggested_position_pct"] == "0%"


def test_build_trade_samples_keeps_unknown_experiment_out_of_stats() -> None:
    payload = build_kelly_trade_samples_payload(
        [_experiment()],
        {
            "schema_version": "open_trader.kelly_paper_orders.v1",
            "synced_at": "2026-07-11 09:30",
            "orders": [
                {
                    "experiment_id": "missing_exp",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:31",
                    "filled_qty": "10",
                    "avg_fill_price": "100",
                    "status": "filled",
                    "order_id": "BUY-1",
                }
            ],
        },
        generated_at="2026-07-12 10:01",
    )

    assert payload["skipped_order_count"] == 1
    assert payload["diagnostics"]["skipped_orders"][0]["reason"] == "unknown_experiment"
    assert payload["diagnostics"]["skipped_orders"][0]["experiment_id"] == "missing_exp"
    assert "missing_exp" not in payload["stats_by_experiment"]
