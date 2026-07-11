from __future__ import annotations

import pytest

from open_trader.kelly_strategy_stats import (
    build_kelly_strategy_stats_payload,
    load_kelly_strategy_stats,
    validate_kelly_strategy_stats_payload,
    write_kelly_strategy_stats,
)


def _trade_samples_payload() -> dict[str, object]:
    return {
        "schema_version": "open_trader.kelly_trade_samples.v1",
        "generated_at": "2026-07-11 12:00",
        "samples": [
            {
                "experiment_id": "trend_us",
                "result": "win",
                "net_pnl_pct": "10%",
                "exit_submitted_at": "2026-07-11 11:59",
            }
        ],
        "open_positions": [],
        "diagnostics": {"skipped_orders": []},
    }


def test_builds_stats_for_every_configured_experiment() -> None:
    payload = build_kelly_strategy_stats_payload(
        [
            {"experiment_id": "trend_us", "market": "US"},
            {"experiment_id": "breakout_hk", "market": "HK"},
        ],
        _trade_samples_payload(),
        generated_at="2026-07-11 12:01",
    )

    assert payload["schema_version"] == "open_trader.kelly_strategy_stats.v1"
    assert payload["source_trade_samples_generated_at"] == "2026-07-11 12:00"
    assert payload["stats_by_experiment"]["trend_us"]["completed_samples"] == 1
    assert payload["stats_by_experiment"]["breakout_hk"]["completed_samples"] == 0
    assert (
        payload["stats_by_experiment"]["breakout_hk"]["suggested_position_pct"]
        == "0%"
    )
    assert payload["stats_by_experiment"]["breakout_hk"]["sample_stage"] == "insufficient"


def test_validate_rejects_stale_or_incomplete_experiment_coverage() -> None:
    payload = build_kelly_strategy_stats_payload(
        [{"experiment_id": "trend_us", "market": "US"}],
        _trade_samples_payload(),
        generated_at="2026-07-11 12:01",
    )

    with pytest.raises(ValueError, match="experiment coverage"):
        validate_kelly_strategy_stats_payload(
            payload,
            expected_experiment_ids={"trend_us", "breakout_hk"},
        )

    with pytest.raises(ValueError, match="stale"):
        validate_kelly_strategy_stats_payload(
            payload,
            expected_trade_samples_generated_at="2026-07-11 12:02",
        )


def test_write_and_load_kelly_strategy_stats(tmp_path) -> None:
    payload = build_kelly_strategy_stats_payload(
        [{"experiment_id": "trend_us", "market": "US"}],
        _trade_samples_payload(),
        generated_at="2026-07-11 12:01",
    )

    path = write_kelly_strategy_stats(tmp_path / "data", payload)
    loaded = load_kelly_strategy_stats(tmp_path / "data")

    assert path == tmp_path / "data" / "latest" / "kelly_strategy_stats.json"
    assert loaded == payload
