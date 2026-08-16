"""Issue #85: bounded in-memory N_LEG performance metrics.

``BoundedMetricWindow`` keeps a fixed-size sample window in memory (no
persistence) and summarizes it as p50/p95/worst.  The read model exposes these
summaries so the dashboard can show N_LEG pipeline health without unbounded
retention.
"""

from __future__ import annotations

from collections import deque
from math import ceil
from statistics import median
from typing import Mapping, Sequence


class BoundedMetricWindow:
    """Fixed-capacity sample window with p50/p95/worst summaries."""

    def __init__(self, maxlen: int) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be >= 1")
        self._samples: deque[float] = deque(maxlen=maxlen)

    def record(self, value: object) -> None:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if number != number or number in (float("inf"), float("-inf")):
            return
        self._samples.append(number)

    def __len__(self) -> int:
        return len(self._samples)

    def summary(self) -> dict[str, object]:
        values = sorted(self._samples)
        if not values:
            return {"samples": 0, "p50": None, "p95": None, "worst": None}
        return {
            "samples": len(values),
            "p50": median(values),
            "p95": _nearest_rank(values, 0.95),
            "worst": values[-1],
        }


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    index = max(0, min(len(values) - 1, ceil(percentile * len(values)) - 1))
    return values[index]


def n_leg_metrics_payload(
    *,
    windows: Mapping[str, BoundedMetricWindow],
    counters: Mapping[str, int],
) -> dict[str, object]:
    """Compose the read-model ``n_leg_metrics`` mapping (fail-closed defaults)."""
    empty = {"samples": 0, "p50": None, "p95": None, "worst": None}
    payload: dict[str, object] = {}
    for name in ("compile", "solve", "end_to_end", "opportunity_survival"):
        window = windows.get(name)
        payload[name] = window.summary() if window is not None else dict(empty)
    for name in ("queue_merge_drop", "timeout", "stale_reject"):
        value = counters.get(name)
        payload[name] = int(value) if value is not None else 0
    return payload
