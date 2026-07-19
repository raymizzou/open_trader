from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
from pathlib import Path
from typing import Sequence


TREND_API_STATS_SCHEMA_VERSION = "open_trader.trend_api_stats.v1"
KELLY_MINIMUM_SAMPLES = 30
KELLY_ROLLING_SAMPLES = 200
KELLY_QUANTUM = Decimal("0.000001")
KELLY_BISECTION_ITERATIONS = 96
KELLY_OPTIMIZER = (
    f"mean_log_growth_derivative_bisection_{KELLY_BISECTION_ITERATIONS}_floor_1e-6"
)


@dataclass(frozen=True)
class TrendKellyRound:
    round_id: str
    source: str
    market: str
    strategy_id: str
    opening_strategy_version: str
    closed_at: str
    net_return: Decimal
    costs_complete: bool
    attribution_status: str
    kelly_eligible: bool


@dataclass(frozen=True)
class TrendKellyState:
    phase: str
    eligible_sample_count: int
    selected_sample_count: int
    enabled: bool
    full_kelly: Decimal | None
    quarter_kelly_cap: Decimal | None
    reason: str
    last_closed_at: str
    selected_round_ids: tuple[str, ...]


def load_trend_kelly_rounds(data_dir: Path) -> tuple[TrendKellyRound, ...]:
    path = data_dir / "latest" / "trend_api_stats.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("trend_api_stats.json is unreadable") from None
    return trend_kelly_rounds_from_payload(payload)


def trend_kelly_rounds_from_payload(payload: object) -> tuple[TrendKellyRound, ...]:
    if not isinstance(payload, Mapping) or (
        payload.get("schema_version") != TREND_API_STATS_SCHEMA_VERSION
    ):
        raise ValueError(
            "trend_api_stats.json schema_version must be "
            f"{TREND_API_STATS_SCHEMA_VERSION!r}"
        )
    records = payload.get("rounds")
    if not isinstance(records, list):
        raise ValueError("trend_api_stats.json must contain rounds")

    rounds: list[TrendKellyRound] = []
    for index, record in enumerate(records):
        label = f"trend_api_stats.json rounds[{index}]"
        if not isinstance(record, Mapping):
            raise ValueError(f"{label} must be an object")
        if str(record.get("source") or "") != "simulation":
            continue
        if not (
            record.get("kelly_eligible") is True
            and record.get("costs_complete") is True
            and record.get("attribution_status") == "attributed"
        ):
            continue
        fields = {
            key: str(record.get(key) or "").strip()
            for key in (
                "round_id",
                "market",
                "strategy_id",
                "opening_strategy_version",
                "closed_at",
            )
        }
        missing = [key for key, value in fields.items() if not value]
        if missing:
            raise ValueError(f"{label} contains invalid {missing[0]}")
        _parse_closed_at(fields["closed_at"], f"{label} contains invalid closed_at")
        try:
            net_return = Decimal(str(record.get("net_return")))
        except (InvalidOperation, ValueError):
            raise ValueError(f"{label} contains invalid net_return") from None
        if not net_return.is_finite():
            raise ValueError(f"{label} contains invalid net_return")
        rounds.append(
            TrendKellyRound(
                round_id=fields["round_id"],
                source="simulation",
                market=fields["market"],
                strategy_id=fields["strategy_id"],
                opening_strategy_version=fields["opening_strategy_version"],
                closed_at=fields["closed_at"],
                net_return=net_return,
                costs_complete=True,
                attribution_status="attributed",
                kelly_eligible=True,
            )
        )
    return tuple(
        sorted(
            rounds,
            key=lambda item: (_parse_closed_at(item.closed_at, "invalid closed_at"), item.round_id),
        )
    )


def _parse_closed_at(value: str, error: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(error) from None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.isoformat() != value
    ):
        raise ValueError(error)
    return parsed


def maximize_average_log_growth(returns: Sequence[Decimal]) -> Decimal:
    """Maximize mean log(1 + f*r) on f in [0, 1], choosing downward ties."""
    ordered = tuple(sorted(returns))
    if not ordered:
        return Decimal("0")
    if any(not value.is_finite() or value < -1 for value in ordered):
        raise ValueError("Kelly returns must be finite and at least -1")

    with localcontext() as context:
        context.prec = 50
        if sum(ordered, Decimal("0")) <= 0:
            return Decimal("0")
        if all(value >= 0 for value in ordered):
            return Decimal("1")

        def derivative(fraction: Decimal) -> Decimal | None:
            terms: list[Decimal] = []
            for value in ordered:
                denominator = Decimal("1") + fraction * value
                if denominator <= 0:
                    return None
                terms.append(value / denominator)
            return sum(terms, Decimal("0"))

        boundary_derivative = derivative(Decimal("1"))
        if boundary_derivative is not None and boundary_derivative >= 0:
            return Decimal("1")

        low = Decimal("0")
        high = Decimal("1")
        for _ in range(KELLY_BISECTION_ITERATIONS):
            middle = (low + high) / 2
            slope = derivative(middle)
            if slope is not None and slope > 0:
                low = middle
            else:
                high = middle
        return low.quantize(KELLY_QUANTUM, rounding=ROUND_FLOOR)


def calculate_trend_kelly(
    rounds: Sequence[TrendKellyRound],
    *,
    market: str,
    strategy_id: str,
    opening_strategy_version: str,
) -> TrendKellyState:
    matching = [
        item
        for item in rounds
        if item.source == "simulation"
        and item.market == market
        and item.strategy_id == strategy_id
        and item.opening_strategy_version == opening_strategy_version
        and item.costs_complete
        and item.attribution_status == "attributed"
        and item.kelly_eligible
    ]
    ordered: list[tuple[datetime, TrendKellyRound]] = []
    seen: set[str] = set()
    for item in matching:
        if not item.round_id:
            raise ValueError("Kelly round_id must be non-empty")
        if item.round_id in seen:
            raise ValueError(f"duplicate Kelly round_id: {item.round_id}")
        seen.add(item.round_id)
        closed_at = _parse_closed_at(
            item.closed_at,
            f"Kelly round {item.round_id} closed_at must be canonical timezone-aware ISO",
        )
        if not item.net_return.is_finite():
            raise ValueError(f"Kelly round {item.round_id} net_return must be finite")
        if item.net_return < -1:
            raise ValueError(
                f"Kelly round {item.round_id} net_return must be at least -1"
            )
        ordered.append((closed_at, item))

    eligible = [
        item for _, item in sorted(ordered, key=lambda pair: (pair[0], pair[1].round_id))
    ]

    count = len(eligible)
    selected = eligible[-KELLY_ROLLING_SAMPLES:]
    selected_ids = tuple(item.round_id for item in selected)
    last_closed_at = eligible[-1].closed_at if eligible else ""
    if count < KELLY_MINIMUM_SAMPLES:
        return TrendKellyState(
            phase="cold_start",
            eligible_sample_count=count,
            selected_sample_count=len(selected),
            enabled=False,
            full_kelly=None,
            quarter_kelly_cap=None,
            reason=(
                f"eligible simulation rounds {count}/{KELLY_MINIMUM_SAMPLES}; "
                "Kelly disabled and fixed risk sizing remains active"
            ),
            last_closed_at=last_closed_at,
            selected_round_ids=selected_ids,
        )

    full_kelly = maximize_average_log_growth(
        tuple(item.net_return for item in selected)
    )
    cap = (full_kelly / 4).quantize(KELLY_QUANTUM, rounding=ROUND_FLOOR)
    return TrendKellyState(
        phase=(
            "active_rolling_200"
            if count >= KELLY_ROLLING_SAMPLES
            else "active_all_samples"
        ),
        eligible_sample_count=count,
        selected_sample_count=len(selected),
        enabled=True,
        full_kelly=full_kelly,
        quarter_kelly_cap=cap,
        reason=("quarter Kelly cap is zero; future entries paused" if cap == 0 else ""),
        last_closed_at=last_closed_at,
        selected_round_ids=selected_ids,
    )
