from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


TRADE_SAMPLES_SCHEMA_VERSION = "open_trader.kelly_trade_samples.v1"


def build_kelly_trade_samples_payload(
    experiments: list[dict[str, Any]],
    paper_orders_payload: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    timestamp = generated_at or _current_timestamp()
    experiment_index = _experiment_index(experiments)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    skipped: list[dict[str, Any]] = []

    for order in _orders_from_payload(paper_orders_payload):
        experiment_id = _text(order.get("experiment_id"))
        market = _text(order.get("market")).upper()
        symbol = _text(order.get("symbol")).upper()
        key = (experiment_id, market, symbol)
        reason = _order_skip_reason(order, experiment_index)
        if reason:
            skipped.append(_diagnostic(order, reason))
            continue
        groups.setdefault(key, []).append(order)

    samples: list[dict[str, Any]] = []
    open_positions: list[dict[str, Any]] = []
    group_skips: list[dict[str, Any]] = []

    for key, orders in sorted(groups.items()):
        paired = _pair_group(key, orders)
        samples.extend(paired["samples"])
        open_positions.extend(paired["open_positions"])
        group_skips.extend(paired["skipped_orders"])

    diagnostics = {"skipped_orders": skipped + group_skips}
    stats = _stats_by_experiment(
        experiments,
        samples,
        open_positions,
        diagnostics["skipped_orders"],
        timestamp,
    )
    return {
        "schema_version": TRADE_SAMPLES_SCHEMA_VERSION,
        "generated_at": timestamp,
        "source_orders_synced_at": _text(paper_orders_payload.get("synced_at")),
        "sample_count": len(samples),
        "open_position_count": len(open_positions),
        "skipped_order_count": len(diagnostics["skipped_orders"]),
        "samples": samples,
        "open_positions": open_positions,
        "stats_by_experiment": stats,
        "diagnostics": diagnostics,
    }


def write_kelly_trade_samples(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / "latest" / "kelly_trade_samples.json"
    _write_json_atomic(path, payload)
    return path


def load_kelly_trade_samples(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "latest" / "kelly_trade_samples.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if payload.get("schema_version") != TRADE_SAMPLES_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version must be {TRADE_SAMPLES_SCHEMA_VERSION!r}",
        )
    if not isinstance(payload.get("stats_by_experiment"), dict):
        raise ValueError(f"{path.name} must contain stats_by_experiment")
    return payload


def _pair_group(
    key: tuple[str, str, str],
    orders: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    experiment_id, market, symbol = key
    samples: list[dict[str, Any]] = []
    open_positions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    open_entry: dict[str, Any] | None = None

    for order in sorted(orders, key=_order_sort_key):
        side = _text(order.get("side")).lower()
        if side == "buy":
            if open_entry is not None:
                skipped.append(_diagnostic(order, "repeated_entry_not_supported"))
                continue
            open_entry = order
            continue
        if side == "sell":
            if open_entry is None:
                skipped.append(_diagnostic(order, "sell_without_open_entry"))
                continue
            entry_qty = _decimal(open_entry.get("filled_qty"))
            exit_qty = _decimal(order.get("filled_qty"))
            if entry_qty is None or exit_qty is None or entry_qty != exit_qty:
                skipped.append(_diagnostic(order, "exit_quantity_mismatch"))
                continue
            samples.append(
                _completed_sample(experiment_id, market, symbol, open_entry, order)
            )
            open_entry = None
            continue
        skipped.append(_diagnostic(order, "unsupported_side"))

    if open_entry is not None:
        open_positions.append(_open_position(experiment_id, market, symbol, open_entry))
    return {"samples": samples, "open_positions": open_positions, "skipped_orders": skipped}


def _completed_sample(
    experiment_id: str,
    market: str,
    symbol: str,
    entry: dict[str, Any],
    exit_order: dict[str, Any],
) -> dict[str, Any]:
    quantity = _decimal(entry.get("filled_qty")) or Decimal("0")
    entry_price = _order_price(entry)
    exit_price = _order_price(exit_order)
    entry_notional = entry_price * quantity
    exit_notional = exit_price * quantity
    gross_pnl = exit_notional - entry_notional
    net_pnl_pct = Decimal("0") if entry_notional == 0 else gross_pnl / entry_notional
    return {
        "experiment_id": experiment_id,
        "market": market,
        "symbol": symbol,
        "entry_order_id": _stable_order_id(entry),
        "exit_order_id": _stable_order_id(exit_order),
        "entry_submitted_at": _text(entry.get("submitted_at")),
        "exit_submitted_at": _text(exit_order.get("submitted_at")),
        "entry_price": _decimal_text(entry_price),
        "exit_price": _decimal_text(exit_price),
        "quantity": _decimal_text(quantity),
        "entry_notional": _decimal_text(entry_notional),
        "exit_notional": _decimal_text(exit_notional),
        "gross_pnl": _decimal_text(gross_pnl),
        "net_pnl_pct": _pct_text(net_pnl_pct),
        "result": "win" if gross_pnl > 0 else "loss" if gross_pnl < 0 else "flat",
    }


def _open_position(
    experiment_id: str,
    market: str,
    symbol: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    quantity = _decimal(entry.get("filled_qty")) or Decimal("0")
    entry_price = _order_price(entry)
    entry_notional = entry_price * quantity
    return {
        "experiment_id": experiment_id,
        "market": market,
        "symbol": symbol,
        "entry_order_id": _stable_order_id(entry),
        "entry_submitted_at": _text(entry.get("submitted_at")),
        "entry_price": _decimal_text(entry_price),
        "quantity": _decimal_text(quantity),
        "entry_notional": _decimal_text(entry_notional),
    }


def _stats_by_experiment(
    experiments: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    skipped_orders: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for experiment in experiments:
        experiment_id = _text(experiment.get("experiment_id"))
        exp_samples = [
            sample for sample in samples if sample["experiment_id"] == experiment_id
        ]
        exp_open = [
            item for item in open_positions if item["experiment_id"] == experiment_id
        ]
        exp_skipped = [
            item for item in skipped_orders if item.get("experiment_id") == experiment_id
        ]
        stats[experiment_id] = _experiment_stats(
            exp_samples,
            exp_open,
            exp_skipped,
            generated_at,
        )
        stats[experiment_id].update(
            {
                "experiment_id": experiment_id,
                "experiment_name": _text(experiment.get("experiment_name")),
                "market": _text(experiment.get("market")).upper(),
            }
        )
    return stats


def _experiment_stats(
    samples: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    skipped_orders: list[dict[str, Any]],
    generated_at: str,
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
        "completed_samples": completed,
        "winning_samples": len(wins),
        "losing_samples": len(losses),
        "flat_samples": len(flats),
        "open_samples": len(open_positions),
        "skipped_orders": len(skipped_orders),
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


def _order_skip_reason(
    order: dict[str, Any],
    experiment_index: dict[str, dict[str, Any]],
) -> str:
    status = _text(order.get("status")).lower()
    if "partial" in status:
        return "partial_fill_not_supported"
    if status != "filled":
        return "unsupported_status"

    filled_qty = _decimal(order.get("filled_qty"))
    order_qty = _decimal(order.get("order_qty"))
    price = _order_price_or_none(order)
    if filled_qty is None or filled_qty <= 0 or price is None or price <= 0:
        return "missing_price_or_quantity"
    if order_qty is not None and filled_qty != order_qty:
        return "partial_fill_not_supported"

    experiment_id = _text(order.get("experiment_id"))
    experiment = experiment_index.get(experiment_id)
    if experiment is None:
        return "unknown_experiment"
    experiment_market = _text(experiment.get("market")).upper()
    if _text(order.get("market")).upper() != experiment_market:
        return "market_mismatch"
    return ""


def _orders_from_payload(paper_orders_payload: dict[str, Any]) -> list[dict[str, Any]]:
    orders = paper_orders_payload.get("orders")
    if not isinstance(orders, list):
        return []
    return [order for order in orders if isinstance(order, dict)]


def _experiment_index(
    experiments: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        _text(experiment.get("experiment_id")): experiment
        for experiment in experiments
        if _text(experiment.get("experiment_id"))
    }


def _order_price(order: dict[str, Any]) -> Decimal:
    return _order_price_or_none(order) or Decimal("0")


def _order_price_or_none(order: dict[str, Any]) -> Decimal | None:
    for key in (
        "avg_fill_price",
        "filled_avg_price",
        "avg_price",
        "order_price",
        "limit_price",
        "price",
    ):
        value = _decimal(order.get(key))
        if value is not None:
            return value
    return None


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


def _stable_order_id(order: dict[str, Any]) -> str:
    for key in ("order_id", "futu_order_id", "execution_order_id"):
        text = _text(order.get(key))
        if text:
            return text
    return ""


def _order_sort_key(order: dict[str, Any]) -> tuple[str, str]:
    return (_text(order.get("submitted_at")), _stable_order_id(order))


def _diagnostic(order: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "experiment_id": _text(order.get("experiment_id")),
        "market": _text(order.get("market")).upper(),
        "symbol": _text(order.get("symbol")).upper(),
        "side": _text(order.get("side")).lower(),
        "status": _text(order.get("status")),
        "order_id": _stable_order_id(order),
        "submitted_at": _text(order.get("submitted_at")),
    }


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
