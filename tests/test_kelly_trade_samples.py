from __future__ import annotations

from open_trader.kelly_trade_samples import (
    build_kelly_trade_samples_payload,
    load_kelly_trade_samples,
    write_kelly_trade_samples,
)


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


def test_build_trade_samples_skips_unsupported_order_patterns() -> None:
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
                    "filled_qty": "0",
                    "order_qty": "10",
                    "avg_fill_price": "-",
                    "status": "submitted",
                    "order_id": "SUBMITTED-1",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:32",
                    "filled_qty": "5",
                    "order_qty": "10",
                    "avg_fill_price": "100",
                    "status": "partial_filled",
                    "order_id": "PARTIAL-1",
                },
                {
                    "experiment_id": "missing_exp",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:33",
                    "filled_qty": "10",
                    "avg_fill_price": "100",
                    "status": "filled",
                    "order_id": "UNKNOWN-1",
                },
            ],
        },
        generated_at="2026-07-12 10:01",
    )

    reasons = [item["reason"] for item in payload["diagnostics"]["skipped_orders"]]
    assert reasons == [
        "unsupported_status",
        "partial_fill_not_supported",
        "unknown_experiment",
    ]
    assert payload["skipped_order_count"] == 3


def test_build_trade_samples_computes_loss_and_shrunk_kelly_stats() -> None:
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
                    "submitted_at": "2026-07-11 10:00",
                    "filled_qty": "10",
                    "avg_fill_price": "110",
                    "status": "filled",
                    "order_id": "SELL-1",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-12 09:31",
                    "filled_qty": "10",
                    "avg_fill_price": "100",
                    "status": "filled",
                    "order_id": "BUY-2",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "sell",
                    "submitted_at": "2026-07-12 10:00",
                    "filled_qty": "10",
                    "avg_fill_price": "95",
                    "status": "filled",
                    "order_id": "SELL-2",
                },
            ],
        },
        generated_at="2026-07-12 10:01",
    )

    stats = payload["stats_by_experiment"]["trend_us"]
    assert stats["completed_samples"] == 2
    assert stats["winning_samples"] == 1
    assert stats["losing_samples"] == 1
    assert stats["raw_win_rate"] == "50%"
    assert stats["adjusted_win_rate"] == "50%"
    assert stats["avg_net_win_pct"] == "10%"
    assert stats["avg_net_loss_pct"] == "5%"
    assert stats["payoff_ratio"] == "2"
    assert stats["full_kelly_pct"] == "25%"
    assert stats["fractional_kelly_pct"] == "6.25%"
    assert stats["suggested_position_pct"] == "4%"
    assert stats["sample_stage"] == "insufficient"
    assert stats["sample_adjustment"] == "样本少于 200，向 50% 收缩"
    assert stats["last_sample_closed_at"] == "2026-07-12 10:00"
    assert stats["last_recomputed_at"] == "2026-07-12 10:01"


def test_write_and_load_kelly_trade_samples(tmp_path) -> None:
    payload = build_kelly_trade_samples_payload(
        [_experiment()],
        {"schema_version": "open_trader.kelly_paper_orders.v1", "orders": []},
        generated_at="2026-07-12 10:01",
    )

    path = write_kelly_trade_samples(tmp_path / "data", payload)
    loaded = load_kelly_trade_samples(tmp_path / "data")

    assert path == tmp_path / "data" / "latest" / "kelly_trade_samples.json"
    assert loaded == payload
