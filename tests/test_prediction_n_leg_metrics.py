"""Issue #85: bounded N_LEG metric window behavior."""

from __future__ import annotations

import pytest

from open_trader.prediction_n_leg_metrics import (
    BoundedMetricWindow,
    n_leg_metrics_payload,
)


def test_window_summary_reports_p50_p95_worst() -> None:
    window = BoundedMetricWindow(maxlen=100)
    for value in range(1, 101):
        window.record(value)

    summary = window.summary()

    assert summary["samples"] == 100
    assert summary["p50"] == 50.5
    assert summary["p95"] == 95
    assert summary["worst"] == 100


def test_window_is_bounded_and_drops_oldest_samples() -> None:
    window = BoundedMetricWindow(maxlen=5)
    for value in range(1000):
        window.record(value)

    assert len(window) == 5
    assert window.summary() == {
        "samples": 5,
        "p50": 997.0,
        "p95": 999,
        "worst": 999,
    }


def test_window_ignores_invalid_samples() -> None:
    window = BoundedMetricWindow(maxlen=10)
    window.record("not-a-number")
    window.record(float("nan"))
    window.record(float("inf"))
    window.record(3)

    assert len(window) == 1
    assert window.summary()["worst"] == 3


def test_window_rejects_zero_capacity() -> None:
    with pytest.raises(ValueError):
        BoundedMetricWindow(maxlen=0)


def test_empty_window_summary_is_fail_closed() -> None:
    summary = BoundedMetricWindow(maxlen=10).summary()
    assert summary == {"samples": 0, "p50": None, "p95": None, "worst": None}


def test_metrics_payload_covers_required_fields_and_defaults() -> None:
    payload = n_leg_metrics_payload(
        windows={
            "compile": BoundedMetricWindow(maxlen=10),
            "solve": BoundedMetricWindow(maxlen=10),
        },
        counters={"queue_merge_drop": 2, "timeout": 1},
    )

    assert payload["compile"] == {"samples": 0, "p50": None, "p95": None, "worst": None}
    assert payload["solve"] == {"samples": 0, "p50": None, "p95": None, "worst": None}
    assert payload["end_to_end"] == {"samples": 0, "p50": None, "p95": None, "worst": None}
    assert payload["opportunity_survival"] == {"samples": 0, "p50": None, "p95": None, "worst": None}
    assert payload["queue_merge_drop"] == 2
    assert payload["timeout"] == 1
    assert payload["stale_reject"] == 0
