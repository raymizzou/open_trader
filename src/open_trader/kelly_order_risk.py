from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .kelly_lab import load_kelly_lab_state
from .kelly_market_rules import kelly_market_currency, normalize_kelly_market
from .kelly_strategy_stats import (
    kelly_trade_samples_digest,
    load_kelly_strategy_stats,
    validate_kelly_strategy_stats_payload,
)
from .kelly_trade_samples import load_kelly_trade_samples


ORDER_RISK_CHECKS_SCHEMA_VERSION = "open_trader.kelly_order_risk_checks.v1"
ORDER_INTENTS_SCHEMA_VERSION = "open_trader.kelly_order_intents.v1"


def load_kelly_order_intents(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "latest" / "kelly_order_intents.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if payload.get("schema_version") != ORDER_INTENTS_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version must be {ORDER_INTENTS_SCHEMA_VERSION!r}",
        )
    intents = payload.get("intents")
    if not isinstance(intents, list):
        raise ValueError(f"{path.name} must contain an intents list")
    return payload


def build_kelly_order_risk_checks(
    data_dir: Path,
    *,
    checked_at: str | None = None,
    max_entry_position_pct: str = "4",
    strategy_capital_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent_payload = load_kelly_order_intents(data_dir)
    strategy_stats_by_experiment = None
    strategy_stats_config_error = ""
    if _contains_entry_intent(intent_payload):
        try:
            lab_state = load_kelly_lab_state(
                data_dir,
                include_operational_artifacts=False,
                include_strategy_capital=False,
                include_strategy_stats=False,
            )
            if not lab_state.available:
                raise ValueError(lab_state.error or "Kelly Lab config is unavailable")
            expected_experiment_ids = {
                str(experiment["experiment_id"]).strip()
                for experiment in lab_state.experiments
            }
            trade_samples_payload = load_kelly_trade_samples(data_dir)
            strategy_stats_payload = load_kelly_strategy_stats(data_dir)
            strategy_stats_by_experiment = validate_kelly_strategy_stats_payload(
                strategy_stats_payload,
                expected_experiment_ids=expected_experiment_ids,
                expected_trade_samples_generated_at=trade_samples_payload[
                    "generated_at"
                ],
                expected_trade_samples_digest=kelly_trade_samples_digest(
                    trade_samples_payload
                ),
            )
        except (FileNotFoundError, ValueError) as exc:
            strategy_stats_by_experiment = None
            strategy_stats_config_error = (
                f"strategy stats/config validation failed: {exc}"
            )
    return build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at=checked_at,
        max_entry_position_pct=max_entry_position_pct,
        strategy_capital_payload=strategy_capital_payload,
        strategy_stats_by_experiment=strategy_stats_by_experiment,
        strategy_stats_config_error=strategy_stats_config_error,
    )


def _contains_entry_intent(payload: dict[str, Any]) -> bool:
    return any(
        isinstance(intent, dict)
        and str(intent.get("intent_type", "")).strip().lower() == "entry"
        and str(intent.get("side", "")).strip().lower() != "sell"
        for intent in payload.get("intents", [])
    )


def build_kelly_order_risk_checks_payload(
    intent_payload: dict[str, Any],
    *,
    checked_at: str | None = None,
    max_entry_position_pct: str = "4",
    strategy_capital_payload: dict[str, Any] | None = None,
    strategy_stats_by_experiment: dict[str, dict[str, Any]] | None = None,
    strategy_stats_config_error: str = "",
) -> dict[str, Any]:
    timestamp = checked_at or _current_timestamp()
    max_entry_pct = _parse_positive_decimal(max_entry_position_pct)
    if max_entry_pct is None:
        raise ValueError("max_entry_position_pct must be a positive decimal")

    raw_intents = intent_payload.get("intents")
    if not isinstance(raw_intents, list):
        raise ValueError("intent payload must contain an intents list")

    capital_by_experiment = _strategy_capital_by_experiment(strategy_capital_payload)
    checks: list[dict[str, Any]] = []
    for item in raw_intents:
        if not isinstance(item, dict):
            continue
        checks.append(
            _build_single_check(
                item,
                checked_at=timestamp,
                max_entry_position_pct=max_entry_pct,
                capital_by_experiment=capital_by_experiment,
                strategy_stats_by_experiment=strategy_stats_by_experiment,
                strategy_stats_config_error=strategy_stats_config_error,
            )
        )

    approved_count = sum(1 for check in checks if check["risk_status"] == "approved")
    blocked_count = sum(1 for check in checks if check["risk_status"] == "blocked")
    return {
        "schema_version": ORDER_RISK_CHECKS_SCHEMA_VERSION,
        "checked_at": timestamp,
        "max_entry_position_pct": _decimal_text(max_entry_pct),
        "intent_count": len(checks),
        "approved_count": approved_count,
        "blocked_count": blocked_count,
        "checks": checks,
    }


def write_kelly_order_risk_checks(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / "latest" / "kelly_order_risk_checks.json"
    _write_json_atomic(path, payload)
    return path


def _build_single_check(
    intent: dict[str, Any],
    *,
    checked_at: str,
    max_entry_position_pct: Decimal,
    capital_by_experiment: dict[str, dict[str, Any]] | None,
    strategy_stats_by_experiment: dict[str, dict[str, Any]] | None,
    strategy_stats_config_error: str,
) -> dict[str, Any]:
    base = _base_check(intent, checked_at=checked_at)
    side = str(intent.get("side", "")).strip().lower()
    intent_type = str(intent.get("intent_type", "")).strip().lower()
    budget_currency = str(intent.get("budget_currency", "")).strip()
    is_exit = side == "sell" or intent_type == "exit"
    market_scope_results = _market_scope_check_results(
        intent,
        include_budget_currency=not is_exit,
    )

    if any(result["status"] == "failed" for result in market_scope_results):
        return {
            **base,
            "risk_status": "blocked",
            "execution_status": "risk_blocked",
            "planned_notional": "",
            "budget_currency": budget_currency,
            "reason": "market scope checks failed",
            "check_results": market_scope_results,
        }

    if is_exit:
        return {
            **base,
            "risk_status": "approved",
            "execution_status": "ready",
            "planned_notional": "",
            "budget_currency": budget_currency,
            "reason": "exit intent reduces exposure",
            "check_results": [
                *market_scope_results,
                {
                    "check": "exit_default_allow",
                    "status": "passed",
                    "detail": "sell/exit intents are not blocked in v1",
                }
            ],
        }

    budget = _parse_positive_decimal(intent.get("per_symbol_budget"))
    position_pct = _parse_positive_decimal(intent.get("suggested_position_pct"))
    check_results = [
        *market_scope_results,
        {
            "check": "per_symbol_budget_positive",
            "status": "passed" if budget is not None else "failed",
            "detail": _field_text(intent.get("per_symbol_budget")),
        },
        {
            "check": "suggested_position_pct_positive",
            "status": "passed" if position_pct is not None else "failed",
            "detail": _field_text(intent.get("suggested_position_pct")).rstrip("%"),
        },
        _strategy_stats_provenance_result(
            intent,
            strategy_stats_by_experiment,
            strategy_stats_config_error=strategy_stats_config_error,
        ),
    ]

    planned_notional = ""
    if budget is not None and position_pct is not None:
        planned = budget * position_pct / Decimal("100")
        planned_notional = _decimal_text(planned)
        pct_check_passed = position_pct <= max_entry_position_pct
        check_results.append(
            {
                "check": "max_entry_position_pct",
                "status": "passed" if pct_check_passed else "failed",
                "detail": (
                    f"{_decimal_text(position_pct)} "
                    f"{'<=' if pct_check_passed else '>'} "
                    f"{_decimal_text(max_entry_position_pct)}"
                ),
            }
        )
        experiment_id = str(intent.get("experiment_id", "")).strip()
        if capital_by_experiment is not None:
            check_results.append(
                _strategy_available_capital_result(
                    intent,
                    capital_by_experiment.get(experiment_id),
                    planned=planned,
                    planned_notional=planned_notional,
                    budget_currency=budget_currency,
                )
            )

    risk_status = (
        "approved"
        if check_results
        and all(result["status"] == "passed" for result in check_results)
        else "blocked"
    )
    return {
        **base,
        "risk_status": risk_status,
        "execution_status": "ready" if risk_status == "approved" else "risk_blocked",
        "planned_notional": planned_notional,
        "budget_currency": budget_currency,
        "reason": (
            "entry risk checks passed"
            if risk_status == "approved"
            else "entry risk checks failed"
        ),
        "check_results": check_results,
    }


def _strategy_capital_by_experiment(
    payload: dict[str, Any] | None,
) -> dict[str, dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        return {}
    return {
        str(item.get("experiment_id", "")).strip(): item
        for item in strategies
        if isinstance(item, dict) and str(item.get("experiment_id", "")).strip()
    }


def _strategy_stats_provenance_result(
    intent: dict[str, Any],
    stats_by_experiment: dict[str, dict[str, Any]] | None,
    *,
    strategy_stats_config_error: str = "",
) -> dict[str, str]:
    if strategy_stats_config_error:
        return {
            "check": "strategy_stats_provenance",
            "status": "failed",
            "detail": strategy_stats_config_error,
        }
    experiment_id = _field_text(intent.get("experiment_id"))
    stats = (
        stats_by_experiment.get(experiment_id)
        if stats_by_experiment is not None
        else None
    )
    expected = {
        "suggested_position_pct": _field_text(
            stats.get("suggested_position_pct") if stats else None
        ),
        "parameter_source": _field_text(
            stats.get("parameter_source") if stats else None
        ),
        "strategy_stats_generated_at": _field_text(
            stats.get("last_recomputed_at") if stats else None
        ),
        "strategy_stats_source_samples_generated_at": _field_text(
            stats.get("source_trade_samples_generated_at") if stats else None
        ),
        "source_trade_samples_digest": _field_text(
            stats.get("source_trade_samples_digest") if stats else None
        ),
    }
    mismatched = [
        field
        for field, expected_value in expected.items()
        if not expected_value or _field_text(intent.get(field)) != expected_value
    ]
    return {
        "check": "strategy_stats_provenance",
        "status": "failed" if mismatched else "passed",
        "detail": (
            ", ".join(mismatched)
            if mismatched
            else f"matches current stats for {experiment_id}"
        ),
    }


def _strategy_available_capital_result(
    intent: dict[str, Any],
    capital_snapshot: dict[str, Any] | None,
    *,
    planned: Decimal,
    planned_notional: str,
    budget_currency: str,
) -> dict[str, str]:
    experiment_id = str(intent.get("experiment_id", "")).strip()
    if capital_snapshot is None:
        return {
            "check": "strategy_available_capital",
            "status": "failed",
            "detail": f"missing capital snapshot for {experiment_id}",
        }

    intent_market = str(
        intent.get("market") or intent.get("experiment_market") or ""
    ).strip().upper()
    capital_market = str(capital_snapshot.get("market") or "").strip().upper()
    if capital_market and capital_market != intent_market:
        return {
            "check": "strategy_available_capital",
            "status": "failed",
            "detail": f"capital market {capital_market} != {intent_market}",
        }

    expected_currency = str(budget_currency or "").strip().upper()
    currency = str(capital_snapshot.get("currency") or expected_currency).strip().upper()
    if currency and expected_currency and currency != expected_currency:
        return {
            "check": "strategy_available_capital",
            "status": "failed",
            "detail": f"capital currency {currency} != {expected_currency}",
        }

    available = _parse_positive_decimal(capital_snapshot.get("available_notional")) or Decimal(
        "0"
    )
    capital_check_passed = planned <= available
    return {
        "check": "strategy_available_capital",
        "status": "passed" if capital_check_passed else "failed",
        "detail": (
            f"{planned_notional} <= "
            f"{_decimal_text(available)} {currency or expected_currency}"
        ),
    }


def _market_scope_check_results(
    intent: dict[str, Any],
    *,
    include_budget_currency: bool,
) -> list[dict[str, str]]:
    try:
        experiment_market = normalize_kelly_market(intent.get("experiment_market"))
    except ValueError:
        return [
            {
                "check": "experiment_market_present",
                "status": "failed",
                "detail": _field_text(intent.get("experiment_market")),
            }
        ]

    try:
        symbol_market = normalize_kelly_market(intent.get("market"))
    except ValueError:
        return [
            {
                "check": "symbol_market_present",
                "status": "failed",
                "detail": _field_text(intent.get("market")),
            }
        ]

    market_matches = symbol_market == experiment_market
    results = [
        {
            "check": "experiment_market_matches_symbol",
            "status": "passed" if market_matches else "failed",
            "detail": (
                f"{symbol_market} == {experiment_market}"
                if market_matches
                else f"{symbol_market} != {experiment_market}"
            ),
        }
    ]
    if not include_budget_currency:
        return results

    budget_currency = str(intent.get("budget_currency", "")).strip().upper()
    market_currency = kelly_market_currency(symbol_market)
    currency_matches = budget_currency == market_currency
    results.append(
        {
            "check": "budget_currency_matches_market",
            "status": "passed" if currency_matches else "failed",
            "detail": (
                f"{budget_currency} == {market_currency}"
                if currency_matches
                else f"{budget_currency} != {market_currency}"
            ),
        }
    )
    return results


def _base_check(intent: dict[str, Any], *, checked_at: str) -> dict[str, str]:
    return {
        "intent_id": str(intent.get("intent_id", "")).strip(),
        "experiment_id": str(intent.get("experiment_id", "")).strip(),
        "experiment_name": str(intent.get("experiment_name", "")).strip(),
        "strategy_id": str(intent.get("strategy_id", "")).strip(),
        "strategy_version": str(intent.get("strategy_version", "")).strip(),
        "market": str(intent.get("market", "")).strip().upper(),
        "symbol": str(intent.get("symbol", "")).strip().upper(),
        "intent_type": str(intent.get("intent_type", "")).strip(),
        "side": str(intent.get("side", "")).strip(),
        "suggested_position_pct": str(
            intent.get("suggested_position_pct", "")
        ).strip(),
        "parameter_source": str(intent.get("parameter_source", "")).strip(),
        "strategy_stats_generated_at": str(
            intent.get("strategy_stats_generated_at", "")
        ).strip(),
        "strategy_stats_source_samples_generated_at": str(
            intent.get("strategy_stats_source_samples_generated_at", "")
        ).strip(),
        "source_trade_samples_digest": str(
            intent.get("source_trade_samples_digest", "")
        ).strip(),
        "checked_at": checked_at,
    }


def _parse_positive_decimal(value: object) -> Decimal | None:
    text = _field_text(value).rstrip("%")
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _field_text(value: object) -> str:
    return str(value or "").strip()


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


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
