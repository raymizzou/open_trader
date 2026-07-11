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
    evidence = _validate_trade_samples_evidence(
        trade_samples_payload,
        artifact_name="trade samples",
    )
    configured_experiments = _configured_experiments(experiments)
    _validate_evidence_experiment_coverage(
        evidence,
        {experiment_id for experiment_id, _, _ in configured_experiments},
    )
    timestamp = generated_at or _current_timestamp()
    source_generated_at = evidence["generated_at"]
    stats_by_experiment = {
        experiment_id: _experiment_stats(
            samples=_for_experiment(evidence["samples"], experiment_id),
            open_positions=_for_experiment(
                evidence["open_positions"], experiment_id
            ),
            skipped_orders=_for_experiment(
                evidence["diagnostics"]["skipped_orders"],
                experiment_id,
            ),
            generated_at=timestamp,
            market=market,
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            source_trade_samples_generated_at=source_generated_at,
        )
        for experiment_id, experiment_name, market in configured_experiments
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
    experiment_count = payload.get("experiment_count")
    if not _is_nonnegative_int(experiment_count) or experiment_count != len(stats):
        raise ValueError(f"{artifact_name} contains invalid experiment_count")
    if (
        expected_trade_samples_generated_at is not None
        and source_generated_at != expected_trade_samples_generated_at
    ):
        raise ValueError(f"{artifact_name} is stale")
    if expected_experiment_ids is not None and set(stats) != expected_experiment_ids:
        raise ValueError(f"{artifact_name} experiment coverage mismatch")
    required = {
        "experiment_id",
        "experiment_name",
        "market",
        "completed_samples",
        "winning_samples",
        "losing_samples",
        "flat_samples",
        "open_samples",
        "skipped_order_count",
        "raw_win_rate",
        "adjusted_win_rate",
        "avg_net_win_pct",
        "avg_net_loss_pct",
        "payoff_ratio",
        "full_kelly_pct",
        "fractional_kelly_pct",
        "sample_stage",
        "suggested_position_pct",
        "sample_adjustment",
        "parameter_source",
        "last_sample_closed_at",
        "last_recomputed_at",
        "source_trade_samples_generated_at",
        "updated_at",
        "calculation_inputs",
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
        _validate_stats_record(
            item,
            experiment_id=experiment_id,
            source_trade_samples_generated_at=source_generated_at,
            artifact_name=artifact_name,
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


def validate_kelly_trade_samples_payload(
    payload: object,
    *,
    artifact_name: str = "kelly_trade_samples.json",
) -> dict[str, Any]:
    return _validate_trade_samples_evidence(
        payload,
        artifact_name=artifact_name,
        require_artifact_fields=True,
    )


def _validate_trade_samples_evidence(
    payload: object,
    *,
    artifact_name: str,
    require_artifact_fields: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{artifact_name} must contain a JSON object")
    if payload.get("schema_version") != TRADE_SAMPLES_SCHEMA_VERSION:
        raise ValueError(
            f"{artifact_name} schema_version must be "
            f"{TRADE_SAMPLES_SCHEMA_VERSION!r}"
        )
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValueError(f"{artifact_name} must contain generated_at")
    samples = payload.get("samples")
    if not isinstance(samples, list):
        raise ValueError(f"{artifact_name} must contain samples")
    open_positions = payload.get("open_positions")
    if not isinstance(open_positions, list):
        raise ValueError(f"{artifact_name} must contain open_positions")
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, dict) or not isinstance(
        diagnostics.get("skipped_orders"), list
    ):
        raise ValueError(f"{artifact_name} must contain diagnostics.skipped_orders")
    skipped_orders = diagnostics["skipped_orders"]
    _validate_completed_samples(samples, artifact_name)
    _validate_open_positions(open_positions, artifact_name)
    _validate_skipped_orders(skipped_orders, artifact_name)
    if require_artifact_fields:
        if not isinstance(payload.get("source_orders_synced_at"), str):
            raise ValueError(f"{artifact_name} must contain source_orders_synced_at")
        _validate_evidence_count(payload, "sample_count", len(samples), artifact_name)
        _validate_evidence_count(
            payload,
            "open_position_count",
            len(open_positions),
            artifact_name,
        )
        _validate_evidence_count(
            payload,
            "skipped_order_count",
            len(skipped_orders),
            artifact_name,
        )
        if not isinstance(payload.get("stats_by_experiment"), dict):
            raise ValueError(f"{artifact_name} must contain stats_by_experiment")
    return payload


def _validate_evidence_count(
    payload: dict[str, Any],
    field: str,
    expected: int,
    artifact_name: str,
) -> None:
    value = payload.get(field)
    if not _is_nonnegative_int(value) or value != expected:
        raise ValueError(f"{artifact_name} contains invalid {field}")


def _validate_completed_samples(
    samples: list[object],
    artifact_name: str,
) -> None:
    for index, sample in enumerate(samples):
        label = f"{artifact_name} samples[{index}]"
        if not isinstance(sample, dict):
            raise ValueError(f"{label} must be an object")
        _require_nonblank_strings(
            sample,
            (
                "experiment_id",
                "market",
                "symbol",
                "entry_order_id",
                "exit_order_id",
                "entry_submitted_at",
                "exit_submitted_at",
            ),
            label,
        )
        result = sample.get("result")
        if result not in {"win", "loss", "flat"}:
            raise ValueError(f"{label} contains invalid result")
        net_pnl_pct = _sample_pct_decimal(sample.get("net_pnl_pct"))
        if net_pnl_pct is None:
            raise ValueError(f"{label} contains invalid net_pnl_pct")
        entry_price = _positive_decimal(sample.get("entry_price"))
        exit_price = _positive_decimal(sample.get("exit_price"))
        quantity = _positive_decimal(sample.get("quantity"))
        entry_notional = _positive_decimal(sample.get("entry_notional"))
        exit_notional = _positive_decimal(sample.get("exit_notional"))
        gross_pnl = _decimal(sample.get("gross_pnl"))
        if None in {
            entry_price,
            exit_price,
            quantity,
            entry_notional,
            exit_notional,
            gross_pnl,
        }:
            raise ValueError(f"{label} contains invalid numeric fields")
        assert entry_price is not None
        assert exit_price is not None
        assert quantity is not None
        assert entry_notional is not None
        assert exit_notional is not None
        assert gross_pnl is not None
        if entry_notional != entry_price * quantity:
            raise ValueError(f"{label} contains invalid entry_notional")
        if exit_notional != exit_price * quantity:
            raise ValueError(f"{label} contains invalid exit_notional")
        if gross_pnl != exit_notional - entry_notional:
            raise ValueError(f"{label} contains invalid gross_pnl")
        if (result == "win" and net_pnl_pct <= 0) or (
            result == "loss" and net_pnl_pct >= 0
        ) or (result == "flat" and net_pnl_pct != 0):
            raise ValueError(f"{label} contains inconsistent result")


def _validate_open_positions(
    open_positions: list[object],
    artifact_name: str,
) -> None:
    for index, position in enumerate(open_positions):
        label = f"{artifact_name} open_positions[{index}]"
        if not isinstance(position, dict):
            raise ValueError(f"{label} must be an object")
        _require_nonblank_strings(
            position,
            (
                "experiment_id",
                "market",
                "symbol",
                "entry_order_id",
                "entry_submitted_at",
            ),
            label,
        )
        entry_price = _positive_decimal(position.get("entry_price"))
        quantity = _positive_decimal(position.get("quantity"))
        entry_notional = _positive_decimal(position.get("entry_notional"))
        if None in {entry_price, quantity, entry_notional}:
            raise ValueError(f"{label} contains invalid numeric fields")
        assert entry_price is not None
        assert quantity is not None
        assert entry_notional is not None
        if entry_notional != entry_price * quantity:
            raise ValueError(f"{label} contains invalid entry_notional")


def _validate_skipped_orders(
    skipped_orders: list[object],
    artifact_name: str,
) -> None:
    for index, diagnostic in enumerate(skipped_orders):
        label = f"{artifact_name} diagnostics.skipped_orders[{index}]"
        if not isinstance(diagnostic, dict):
            raise ValueError(f"{label} must be an object")
        reason = diagnostic.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{label} contains invalid reason")
        for field in (
            "experiment_id",
            "market",
            "symbol",
            "side",
            "status",
            "order_id",
            "submitted_at",
        ):
            if not isinstance(diagnostic.get(field), str):
                raise ValueError(f"{label} contains invalid {field}")


def _require_nonblank_strings(
    record: dict[str, Any],
    fields: tuple[str, ...],
    label: str,
) -> None:
    for field in fields:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} contains invalid {field}")


def _positive_decimal(value: object) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _sample_pct_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str) or not value.strip().endswith("%"):
        return None
    return _decimal(value.strip()[:-1])


def _validate_evidence_experiment_coverage(
    evidence: dict[str, Any],
    configured_experiment_ids: set[str],
) -> None:
    for field in ("samples", "open_positions"):
        for index, record in enumerate(evidence[field]):
            experiment_id = record["experiment_id"]
            if experiment_id not in configured_experiment_ids:
                raise ValueError(
                    f"trade samples {field}[{index}] has unknown experiment "
                    f"{experiment_id}"
                )


def _validate_stats_record(
    item: dict[str, Any],
    *,
    experiment_id: str,
    source_trade_samples_generated_at: str,
    artifact_name: str,
) -> None:
    label = f"{artifact_name} stats for {experiment_id}"
    if item["experiment_id"] != experiment_id:
        raise ValueError(f"{label} contains invalid experiment_id")
    if not isinstance(item["experiment_name"], str):
        raise ValueError(f"{label} contains invalid experiment_name")
    if not isinstance(item["market"], str) or not item["market"].strip():
        raise ValueError(f"{label} contains invalid market")

    counts = {
        field: item[field]
        for field in (
            "completed_samples",
            "winning_samples",
            "losing_samples",
            "flat_samples",
            "open_samples",
            "skipped_order_count",
        )
    }
    for field, value in counts.items():
        if not _is_nonnegative_int(value):
            raise ValueError(f"{label} contains invalid {field}")
    completed = counts["completed_samples"]
    classified = (
        counts["winning_samples"]
        + counts["losing_samples"]
        + counts["flat_samples"]
    )
    for field in ("winning_samples", "losing_samples", "flat_samples"):
        if counts[field] > completed:
            raise ValueError(f"{label} contains invalid {field}")
    if classified > completed:
        raise ValueError(f"{label} contains invalid completed_samples")

    expected_stage = "sufficient" if completed >= 200 else "insufficient"
    if item["sample_stage"] != expected_stage:
        raise ValueError(f"{label} contains invalid sample_stage")
    expected_adjustment = (
        "未收缩" if completed >= 200 else "样本少于 200，向 50% 收缩"
    )
    if item["sample_adjustment"] != expected_adjustment:
        raise ValueError(f"{label} contains invalid sample_adjustment")

    calculation_inputs = _validated_calculation_inputs(item, label)
    raw_win_rate = calculation_inputs["raw_win_rate_fraction"]
    adjusted_win_rate = calculation_inputs["adjusted_win_rate_fraction"]
    avg_net_win = calculation_inputs["avg_net_win_fraction"]
    avg_net_loss = calculation_inputs["avg_net_loss_fraction"]
    payoff_ratio = calculation_inputs["payoff_ratio"]
    if raw_win_rate > Decimal("1") or adjusted_win_rate > Decimal("1"):
        raise ValueError(f"{label} contains invalid win rate")
    expected_raw_win_rate = (
        Decimal(counts["winning_samples"]) / Decimal(completed)
        if completed
        else Decimal("0")
    )
    expected_adjusted_win_rate = _adjusted_win_rate(
        counts["winning_samples"], completed
    )
    if raw_win_rate != expected_raw_win_rate:
        raise ValueError(f"{label} contains invalid raw_win_rate")
    if adjusted_win_rate != expected_adjusted_win_rate:
        raise ValueError(f"{label} contains invalid adjusted_win_rate")
    if item["raw_win_rate"] != _pct_text(raw_win_rate):
        raise ValueError(f"{label} contains invalid raw_win_rate")
    if item["adjusted_win_rate"] != _pct_text(adjusted_win_rate):
        raise ValueError(f"{label} contains invalid adjusted_win_rate")
    if item["win_rate"] != _pct_text(raw_win_rate):
        raise ValueError(f"{label} contains invalid win_rate")
    full_kelly_display = _validated_pct(
        item["full_kelly_pct"], field="full_kelly_pct", label=label
    )
    fractional_kelly_display = _validated_pct(
        item["fractional_kelly_pct"],
        field="fractional_kelly_pct",
        label=label,
    )
    if any(value > Decimal("1") for value in (full_kelly_display, fractional_kelly_display)):
        raise ValueError(f"{label} contains invalid kelly percentage")
    if avg_net_win < 0 or avg_net_loss < 0:
        raise ValueError(f"{label} contains invalid net pnl percentage")
    if payoff_ratio < 0:
        raise ValueError(f"{label} contains invalid payoff_ratio")
    if counts["winning_samples"] == 0 and avg_net_win != 0:
        raise ValueError(f"{label} contains invalid avg_net_win_pct")
    if counts["losing_samples"] == 0 and avg_net_loss != 0:
        raise ValueError(f"{label} contains invalid avg_net_loss_pct")
    expected_payoff_ratio = (
        Decimal("0") if avg_net_loss <= 0 else avg_net_win / avg_net_loss
    )
    if payoff_ratio != expected_payoff_ratio:
        raise ValueError(f"{label} contains invalid payoff_ratio")
    full_kelly = _kelly_fraction(adjusted_win_rate, payoff_ratio)
    fractional_kelly = full_kelly / Decimal("4") if full_kelly > 0 else Decimal("0")
    suggested_position = (
        min(fractional_kelly, Decimal("0.04"))
        if fractional_kelly > 0
        else Decimal("0")
    )
    if item["avg_net_win_pct"] != _pct_text(avg_net_win):
        raise ValueError(f"{label} contains invalid avg_net_win_pct")
    if item["avg_net_loss_pct"] != _pct_text(avg_net_loss):
        raise ValueError(f"{label} contains invalid avg_net_loss_pct")
    if item["payoff_ratio"] != _decimal_text(payoff_ratio):
        raise ValueError(f"{label} contains invalid payoff_ratio")
    if item["full_kelly_pct"] != _pct_text(full_kelly):
        raise ValueError(f"{label} contains invalid full_kelly_pct")
    if item["fractional_kelly_pct"] != _pct_text(fractional_kelly):
        raise ValueError(f"{label} contains invalid fractional_kelly_pct")
    if item["suggested_position_pct"] != _pct_text(suggested_position):
        raise ValueError(f"{label} contains invalid suggested_position_pct")
    if completed == 0 and suggested_position != 0:
        raise ValueError(f"{label} contains invalid suggested_position_pct")

    if item["parameter_source"] != "futu_paper_order_samples":
        raise ValueError(f"{label} contains invalid parameter_source")
    if item["source_trade_samples_generated_at"] != source_trade_samples_generated_at:
        raise ValueError(
            f"{label} contains invalid source_trade_samples_generated_at"
        )
    for field in ("last_recomputed_at", "updated_at"):
        if not isinstance(item[field], str) or not item[field].strip():
            raise ValueError(f"{label} contains invalid {field}")
    if not isinstance(item["last_sample_closed_at"], str):
        raise ValueError(f"{label} contains invalid last_sample_closed_at")


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validated_calculation_inputs(
    item: dict[str, Any],
    label: str,
) -> dict[str, Decimal]:
    raw_inputs = item.get("calculation_inputs")
    if not isinstance(raw_inputs, dict):
        raise ValueError(f"{label} contains invalid calculation_inputs")
    required = {
        "raw_win_rate_fraction",
        "adjusted_win_rate_fraction",
        "avg_net_win_fraction",
        "avg_net_loss_fraction",
        "payoff_ratio",
    }
    if set(raw_inputs) != required:
        raise ValueError(f"{label} contains invalid calculation_inputs")
    parsed: dict[str, Decimal] = {}
    for field in required:
        value = raw_inputs[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} contains invalid calculation_inputs")
        decimal_value = _decimal(value)
        if decimal_value is None or _decimal_text(decimal_value) != value:
            raise ValueError(f"{label} contains invalid calculation_inputs")
        parsed[field] = decimal_value
    return parsed


def _validated_decimal(value: object, *, field: str, label: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} contains invalid {field}")
    parsed = _decimal(value)
    if parsed is None:
        raise ValueError(f"{label} contains invalid {field}")
    return parsed


def _validated_pct(value: object, *, field: str, label: str) -> Decimal:
    if not isinstance(value, str) or not value.strip().endswith("%"):
        raise ValueError(f"{label} contains invalid {field}")
    parsed = _decimal(value.strip()[:-1])
    if parsed is None or parsed < 0 or parsed != parsed.quantize(Decimal("0.01")):
        raise ValueError(f"{label} contains invalid {field}")
    return parsed / Decimal("100")


def _configured_experiments(
    experiments: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    return [
        (
            experiment_id,
            _text(experiment.get("experiment_name")),
            _text(experiment.get("market")).upper(),
        )
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
    experiment_name: str,
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
    calculation_inputs = {
        "raw_win_rate_fraction": _decimal_text(raw_win_rate),
        "adjusted_win_rate_fraction": _decimal_text(adjusted_win_rate),
        "avg_net_win_fraction": _decimal_text(avg_net_win),
        "avg_net_loss_fraction": _decimal_text(avg_net_loss),
        "payoff_ratio": _decimal_text(payoff_ratio),
    }
    last_closed_at = max(
        (_text(sample.get("exit_submitted_at")) for sample in samples),
        default="",
    )
    return {
        "experiment_id": experiment_id,
        "experiment_name": experiment_name,
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
        "calculation_inputs": calculation_inputs,
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
    pct = _pct_value(value) * Decimal("100")
    return f"{_decimal_text(pct)}%"


def _pct_value(value: Decimal) -> Decimal:
    return (value * Decimal("100")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    ) / Decimal("100")


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
