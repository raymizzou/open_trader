from __future__ import annotations

import copy

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


def _samples(*, wins: int, losses: int, flats: int = 0) -> list[dict[str, str]]:
    return [
        {
            "experiment_id": "trend_us",
            "result": "win",
            "net_pnl_pct": "10%",
            "exit_submitted_at": "2026-07-11 11:59",
        }
        for _ in range(wins)
    ] + [
        {
            "experiment_id": "trend_us",
            "result": "loss",
            "net_pnl_pct": "-5%",
            "exit_submitted_at": "2026-07-11 11:59",
        }
        for _ in range(losses)
    ] + [
        {
            "experiment_id": "trend_us",
            "result": "flat",
            "net_pnl_pct": "0%",
            "exit_submitted_at": "2026-07-11 11:59",
        }
        for _ in range(flats)
    ]


def _valid_stats_payload() -> dict[str, object]:
    return build_kelly_strategy_stats_payload(
        [{"experiment_id": "trend_us", "market": "US"}],
        _trade_samples_payload(),
        generated_at="2026-07-11 12:01",
    )


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


def test_builds_unshrunk_stats_at_200_samples() -> None:
    trade_samples = _trade_samples_payload()
    trade_samples["samples"] = _samples(wins=100, losses=100)

    at_199 = copy.deepcopy(trade_samples)
    at_199["samples"] = _samples(wins=99, losses=100)
    shrunk = build_kelly_strategy_stats_payload(
        [{"experiment_id": "trend_us", "market": "US"}],
        at_199,
        generated_at="2026-07-11 12:01",
    )["stats_by_experiment"]["trend_us"]
    unshrunk = build_kelly_strategy_stats_payload(
        [{"experiment_id": "trend_us", "market": "US"}],
        trade_samples,
        generated_at="2026-07-11 12:01",
    )["stats_by_experiment"]["trend_us"]

    assert shrunk["sample_stage"] == "insufficient"
    assert shrunk["adjusted_win_rate"] != shrunk["raw_win_rate"]
    assert unshrunk["sample_stage"] == "sufficient"
    assert unshrunk["adjusted_win_rate"] == unshrunk["raw_win_rate"] == "50%"


def test_applies_quarter_kelly_and_four_percent_cap() -> None:
    trade_samples = _trade_samples_payload()
    trade_samples["samples"] = _samples(wins=150, losses=50)

    stats = build_kelly_strategy_stats_payload(
        [{"experiment_id": "trend_us", "market": "US"}],
        trade_samples,
        generated_at="2026-07-11 12:01",
    )["stats_by_experiment"]["trend_us"]

    assert stats["full_kelly_pct"] == "62.5%"
    assert stats["fractional_kelly_pct"] == "15.63%"
    assert stats["suggested_position_pct"] == "4%"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(experiment_count="1"), "experiment_count"),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                completed_samples="1"
            ),
            "completed_samples",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                winning_samples=2
            ),
            "winning_samples",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                open_samples=-1
            ),
            "open_samples",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                sample_stage="sufficient"
            ),
            "sample_stage",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                suggested_position_pct="four percent"
            ),
            "suggested_position_pct",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                suggested_position_pct="4.01%"
            ),
            "suggested_position_pct",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                parameter_source="other"
            ),
            "parameter_source",
        ),
        (lambda payload: payload.update(generated_at=" "), "generated_at"),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                last_recomputed_at=" "
            ),
            "last_recomputed_at",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                source_trade_samples_generated_at="2026-07-11 12:02"
            ),
            "source_trade_samples_generated_at",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                raw_win_rate="not-a-percent"
            ),
            "raw_win_rate",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                avg_net_win_pct="-1%"
            ),
            "avg_net_win_pct",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                payoff_ratio="NaN"
            ),
            "payoff_ratio",
        ),
        (
            lambda payload: payload["stats_by_experiment"]["trend_us"].update(
                full_kelly_pct="101%"
            ),
            "kelly percentage",
        ),
    ],
)
def test_validate_rejects_malformed_decision_stats(mutate, message: str) -> None:
    payload = _valid_stats_payload()
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        validate_kelly_strategy_stats_payload(payload)


def test_validate_requires_zero_sample_position_to_be_zero() -> None:
    payload = build_kelly_strategy_stats_payload(
        [{"experiment_id": "trend_us", "market": "US"}],
        {
            **_trade_samples_payload(),
            "samples": [],
        },
        generated_at="2026-07-11 12:01",
    )
    payload["stats_by_experiment"]["trend_us"]["suggested_position_pct"] = "0.01%"

    with pytest.raises(ValueError, match="suggested_position_pct"):
        validate_kelly_strategy_stats_payload(payload)
