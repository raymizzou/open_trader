from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


STRATEGY_STATS_SCHEMA_VERSION = "open_trader.kelly_strategy_stats.v1"
TRADE_SAMPLES_SCHEMA_VERSION = "open_trader.kelly_trade_samples.v1"


def build_kelly_strategy_stats_payload(
    experiments: list[dict[str, Any]],
    trade_samples_payload: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    _validate_trade_samples_payload(trade_samples_payload)
    timestamp = generated_at or _current_timestamp()
    source_generated_at = trade_samples_payload["generated_at"]
    stats_by_experiment = {
        experiment_id: _experiment_stats(
            samples=_for_experiment(trade_samples_payload["samples"], experiment_id),
            open_positions=_for_experiment(
                trade_samples_payload["open_positions"], experiment_id
            ),
            skipped_orders=_for_experiment(
                trade_samples_payload["diagnostics"]["skipped_orders"],
                experiment_id,
            ),
            generated_at=timestamp,
            market=market,
            experiment_id=experiment_id,
            source_trade_samples_generated_at=source_generated_at,
        )
        for experiment_id, market in _configured_experiments(experiments)
    }
    return {
        "schema_version": STRATEGY_STATS_SCHEMA_VERSION,
        "generated_at": timestamp,
        "source_trade_samples_generated_at": source_generated_at,
        "experiment_count": len(stats_by_experiment),
        "stats_by_experiment": stats_by_experiment,
    }


def validate_kelly_strategy_stats_payload(
    payload: object,
    *,
    artifact_name: str = "kelly_strategy_stats.json",
    expected_experiment_ids: set[str] | None = None,
    expected_trade_samples_generated_at: str | None = None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError(f"{artifact_name} must contain a JSON object")
    if payload.get("schema_version") != STRATEGY_STATS_SCHEMA_VERSION:
        raise ValueError(
            f"{artifact_name} schema_version must be "
            f"{STRATEGY_STATS_SCHEMA_VERSION!r}"
        )
    generated_at = payload.get("generated_at")
    source_generated_at = payload.get("source_trade_samples_generated_at")
    stats = payload.get("stats_by_experiment")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValueError(f"{artifact_name} must contain generated_at")
    if not isinstance(source_generated_at, str) or not source_generated_at.strip():
        raise ValueError(
            f"{artifact_name} must contain source_trade_samples_generated_at"
        )
    if not isinstance(stats, dict):
        raise ValueError(f"{artifact_name} must contain stats_by_experiment")
    if (
        expected_trade_samples_generated_at is not None
        and source_generated_at != expected_trade_samples_generated_at
    ):
        raise ValueError(f"{artifact_name} is stale")
    if expected_experiment_ids is not None and set(stats) != expected_experiment_ids:
        raise ValueError(f"{artifact_name} experiment coverage mismatch")
    required = {
        "completed_samples",
        "sample_stage",
        "suggested_position_pct",
        "parameter_source",
        "last_recomputed_at",
        "source_trade_samples_generated_at",
    }
    for experiment_id, item in stats.items():
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            raise ValueError(f"{artifact_name} contains invalid experiment id")
        if not isinstance(item, dict):
            raise ValueError(
                f"{artifact_name} stats for {experiment_id} must be an object"
            )
        missing = required - set(item)
        if missing:
            raise ValueError(
                f"{artifact_name} stats for {experiment_id} missing {sorted(missing)}"
            )
    return copy.deepcopy(stats)


def write_kelly_strategy_stats(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / "latest" / "kelly_strategy_stats.json"
    validate_kelly_strategy_stats_payload(payload)
    _write_json_atomic(path, payload)
    return path


def load_kelly_strategy_stats(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "latest" / "kelly_strategy_stats.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    validate_kelly_strategy_stats_payload(payload, artifact_name=path.name)
    return payload


def _validate_trade_samples_payload(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != TRADE_SAMPLES_SCHEMA_VERSION:
        raise ValueError(
            "trade samples schema_version must be "
            f"{TRADE_SAMPLES_SCHEMA_VERSION!r}"
        )
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValueError("trade samples must contain generated_at")
    if not isinstance(payload.get("samples"), list):
        raise ValueError("trade samples must contain samples")
    if not isinstance(payload.get("open_positions"), list):
        raise ValueError("trade samples must contain open_positions")
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, dict) or not isinstance(
        diagnostics.get("skipped_orders"), list
    ):
        raise ValueError("trade samples must contain diagnostics.skipped_orders")


def _configured_experiments(
    experiments: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    return [
        (experiment_id, _text(experiment.get("market")).upper())
        for experiment in experiments
        if (experiment_id := _text(experiment.get("experiment_id")))
    ]


def _for_experiment(
    records: list[object], experiment_id: str
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if isinstance(record, dict) and _text(record.get("experiment_id")) == experiment_id
    ]


def _experiment_stats(
    *,
    samples: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    skipped_orders: list[dict[str, Any]],
    generated_at: str,
    market: str,
    experiment_id: str,
    source_trade_samples_generated_at: str,
) -> dict[str, Any]:
    completed = len(samples)
    wins = [sample for sample in samples if _text(sample.get("result")) == "win"]
    losses = [sample for sample in samples if _text(sample.get("result")) == "loss"]
    flats = [sample for sample in samples if _text(sample.get("result")) == "flat"]
    raw_win_rate = (
        Decimal(len(wins)) / Decimal(completed) if completed else Decimal("0")
    )
    adjusted_win_rate = _adjusted_win_rate(len(wins), completed)
    avg_net_win = _average_pct(wins)
    avg_net_loss = abs(_average_pct(losses))
    payoff_ratio = (
        Decimal("0") if avg_net_loss <= 0 else avg_net_win / avg_net_loss
    )
    full_kelly = _kelly_fraction(adjusted_win_rate, payoff_ratio)
    fractional_kelly = full_kelly / Decimal("4") if full_kelly > 0 else Decimal("0")
    suggested_position = (
        min(fractional_kelly, Decimal("0.04"))
        if fractional_kelly > 0
        else Decimal("0")
    )
    last_closed_at = max(
        (_text(sample.get("exit_submitted_at")) for sample in samples),
        default="",
    )
    return {
        "experiment_id": experiment_id,
        "market": market,
        "completed_samples": completed,
        "winning_samples": len(wins),
        "losing_samples": len(losses),
        "flat_samples": len(flats),
        "open_samples": len(open_positions),
        "skipped_order_count": len(skipped_orders),
        "raw_win_rate": _pct_text(raw_win_rate),
        "adjusted_win_rate": _pct_text(adjusted_win_rate),
        "avg_net_win_pct": _pct_text(avg_net_win),
        "avg_net_loss_pct": _pct_text(avg_net_loss),
        "payoff_ratio": _decimal_text(payoff_ratio),
        "full_kelly_pct": _pct_text(full_kelly),
        "fractional_kelly_pct": _pct_text(fractional_kelly),
        "suggested_position_pct": _pct_text(suggested_position),
        "sample_stage": "sufficient" if completed >= 200 else "insufficient",
        "sample_adjustment": (
            "未收缩" if completed >= 200 else "样本少于 200，向 50% 收缩"
        ),
        "last_sample_closed_at": last_closed_at,
        "last_recomputed_at": generated_at,
        "win_rate": _pct_text(raw_win_rate),
        "parameter_source": "futu_paper_order_samples",
        "source_trade_samples_generated_at": source_trade_samples_generated_at,
        "updated_at": generated_at,
    }


def _adjusted_win_rate(winning_samples: int, completed_samples: int) -> Decimal:
    if completed_samples >= 200:
        return Decimal(winning_samples) / Decimal(completed_samples)
    return (Decimal(winning_samples) + Decimal("100")) / (
        Decimal(completed_samples) + Decimal("200")
    )


def _average_pct(samples: list[dict[str, Any]]) -> Decimal:
    values = [
        value
        for value in (_pct_decimal(sample.get("net_pnl_pct")) for sample in samples)
        if value is not None
    ]
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _pct_decimal(value: object) -> Decimal | None:
    text = _text(value).rstrip("%")
    if not text:
        return None
    parsed = _decimal(text)
    if parsed is None:
        return None
    return parsed / Decimal("100")


def _kelly_fraction(win_rate: Decimal, payoff_ratio: Decimal) -> Decimal:
    if payoff_ratio <= 0:
        return Decimal("0")
    kelly = win_rate - ((Decimal("1") - win_rate) / payoff_ratio)
    return kelly if kelly > 0 else Decimal("0")


def _decimal(value: object) -> Decimal | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _pct_text(value: Decimal) -> str:
    pct = (value * Decimal("100")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return f"{_decimal_text(pct)}%"


def _text(value: object) -> str:
    return str(value or "").strip()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _current_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
