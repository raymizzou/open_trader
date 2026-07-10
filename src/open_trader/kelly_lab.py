from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .kelly_lifecycle import build_kelly_lifecycle_states
from .kelly_market_rules import (
    kelly_market_capital_pool,
    kelly_market_currency,
    normalize_kelly_market,
)


TEMPLATES_SCHEMA_VERSION = "open_trader.kelly_strategy_templates.v1"
EXPERIMENTS_SCHEMA_VERSION = "open_trader.kelly_experiments.v1"
PAPER_ORDERS_SCHEMA_VERSION = "open_trader.kelly_paper_orders.v1"
ORDER_EXECUTIONS_SCHEMA_VERSION = "open_trader.kelly_order_executions.v1"

ALLOWED_EXPERIMENT_STATUSES = {"draft", "running", "paused", "completed", "failed"}

REQUIRED_TEMPLATE_FIELDS = {
    "strategy_id",
    "strategy_name",
    "strategy_version",
    "entry_rule_description",
    "exit_rule_description",
    "max_holding_days",
    "order_type",
    "market_session",
}

REQUIRED_EXPERIMENT_FIELDS = {
    "experiment_id",
    "experiment_name",
    "strategy_id",
    "strategy_version",
    "market",
    "start_date",
    "paper_account",
    "experiment_budget",
    "budget_currency",
    "capital_utilization_pct",
    "allocation_mode",
    "max_open_position_per_symbol",
    "status",
    "locked",
    "participants",
    "stats",
}

REQUIRED_PARTICIPANT_FIELDS = {
    "market",
    "symbol",
    "name",
    "source",
    "locked",
    "per_symbol_budget",
    "budget_currency",
}


@dataclass(frozen=True)
class KellyLabState:
    available: bool
    templates: list[dict[str, Any]] = field(default_factory=list)
    experiments: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "template_count": len(self.templates),
            "experiment_count": len(self.experiments),
            "templates": self.templates,
            "experiments": self.experiments,
            "error": self.error,
        }


def load_kelly_lab_state(data_dir: Path) -> KellyLabState:
    latest_dir = data_dir / "latest"
    templates_path = latest_dir / "kelly_strategy_templates.json"
    experiments_path = latest_dir / "kelly_experiments.json"
    paper_orders_path = latest_dir / "kelly_paper_orders.json"
    order_executions_path = latest_dir / "kelly_order_executions.json"

    missing_path = _first_missing_path(templates_path, experiments_path)
    if missing_path is not None:
        return KellyLabState(
            available=False,
            error=f"{missing_path.name} not found at {missing_path}",
        )

    templates_payload = _load_json_object(templates_path)
    experiments_payload = _load_json_object(experiments_path)

    templates = _validate_templates_payload(templates_payload, templates_path)
    templates_by_key = _index_templates_by_strategy_key(templates)
    experiments = _validate_experiments_payload(
        experiments_payload,
        experiments_path,
        templates_by_key,
    )
    paper_orders = _load_optional_paper_orders(paper_orders_path)
    experiments = _attach_paper_orders_to_experiments(experiments, paper_orders)
    order_execution = _load_optional_order_execution(order_executions_path)
    experiments = _attach_order_execution_to_experiments(experiments, order_execution)

    return KellyLabState(
        available=True,
        templates=templates,
        experiments=experiments,
    )


def index_kelly_experiments_by_market_symbol(
    experiments: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    indexed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for experiment in experiments:
        participants = experiment.get("participants")
        if not isinstance(participants, list):
            continue
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            market = participant.get("market")
            symbol = participant.get("symbol")
            if not isinstance(market, str) or not isinstance(symbol, str):
                continue
            key = (market.upper(), symbol.upper())
            indexed.setdefault(key, []).append(experiment)
    return indexed


def _first_missing_path(*paths: Path) -> Path | None:
    for path in paths:
        if not path.exists():
            return path
    return None


def _load_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _load_optional_paper_orders(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = _load_json_object(path)
    _validate_schema_version(
        payload,
        path,
        expected_schema_version=PAPER_ORDERS_SCHEMA_VERSION,
    )
    orders = payload.get("orders")
    if not isinstance(orders, list):
        raise ValueError(f"{path.name} must contain an orders list")

    validated: list[dict[str, Any]] = []
    for index, order in enumerate(orders):
        if not isinstance(order, dict):
            raise ValueError(f"{path.name} order {index} must be an object")
        experiment_id = order.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise ValueError(f"{path.name} order {index} has invalid experiment_id")
        normalized = copy.deepcopy(order)
        for key in ("market", "symbol", "side", "status"):
            if isinstance(normalized.get(key), str):
                normalized[key] = normalized[key].strip()
        validated.append(normalized)
    return validated


def _load_optional_order_execution(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = _load_json_object(path)
    _validate_schema_version(
        payload,
        path,
        expected_schema_version=ORDER_EXECUTIONS_SCHEMA_VERSION,
    )
    executions = payload.get("executions")
    if not isinstance(executions, list):
        raise ValueError(f"{path.name} must contain an executions list")

    validated: list[dict[str, Any]] = []
    for index, execution in enumerate(executions):
        if not isinstance(execution, dict):
            raise ValueError(f"{path.name} execution {index} must be an object")
        experiment_id = execution.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            raise ValueError(f"{path.name} execution {index} has invalid experiment_id")
        normalized = copy.deepcopy(execution)
        normalized["experiment_id"] = experiment_id.strip()
        for key in ("market", "symbol", "side", "execution_status"):
            if isinstance(normalized.get(key), str):
                normalized[key] = normalized[key].strip()
        validated.append(normalized)

    normalized_payload = copy.deepcopy(payload)
    normalized_payload["executions"] = validated
    return normalized_payload


def _attach_paper_orders_to_experiments(
    experiments: list[dict[str, Any]],
    paper_orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not paper_orders:
        return experiments
    orders_by_experiment: dict[str, list[dict[str, Any]]] = {}
    for order in paper_orders:
        experiment_id = order.get("experiment_id")
        if not isinstance(experiment_id, str):
            continue
        orders_by_experiment.setdefault(experiment_id, []).append(copy.deepcopy(order))

    attached: list[dict[str, Any]] = []
    for experiment in experiments:
        normalized = copy.deepcopy(experiment)
        experiment_id = normalized.get("experiment_id")
        if isinstance(experiment_id, str) and experiment_id in orders_by_experiment:
            order_sync = normalized.get("order_sync")
            if isinstance(order_sync, dict):
                order_sync = copy.deepcopy(order_sync)
            else:
                order_sync = {}
            order_sync["orders"] = orders_by_experiment[experiment_id]
            normalized["order_sync"] = order_sync
        attached.append(normalized)
    return attached


def _attach_order_execution_to_experiments(
    experiments: list[dict[str, Any]],
    order_execution: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if order_execution is None:
        return experiments

    executions_by_experiment: dict[str, list[dict[str, Any]]] = {}
    for execution in order_execution.get("executions", []):
        if not isinstance(execution, dict):
            continue
        experiment_id = execution.get("experiment_id")
        if not isinstance(experiment_id, str):
            continue
        executions_by_experiment.setdefault(experiment_id, []).append(
            copy.deepcopy(execution)
        )

    attached: list[dict[str, Any]] = []
    for experiment in experiments:
        normalized = copy.deepcopy(experiment)
        experiment_id = normalized.get("experiment_id")
        if isinstance(experiment_id, str):
            executions = executions_by_experiment.get(experiment_id, [])
            if executions:
                normalized["order_execution"] = _order_execution_summary(
                    order_execution,
                    executions,
                )
        attached.append(normalized)
    return attached


def _order_execution_summary(
    payload: dict[str, Any],
    executions: list[dict[str, Any]],
) -> dict[str, Any]:
    dry_run_count = _count_executions(executions, "dry_run")
    submitted_count = _count_executions(executions, "submitted")
    skipped_count = _count_executions(executions, "skipped")
    failed_count = _count_executions(executions, "failed")
    status = "failed" if failed_count else "partial" if skipped_count else "success"
    return {
        "status": status,
        "environment": str(payload.get("environment", "")).strip(),
        "source": str(payload.get("source", "")).strip(),
        "last_executed_at": str(payload.get("executed_at", "")).strip(),
        "execution_count": len(executions),
        "submitted_count": submitted_count,
        "dry_run_count": dry_run_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "message": (
            "Kelly 订单执行存在失败或跳过项。"
            if failed_count or skipped_count
            else "Kelly 订单执行结果已生成。"
        ),
        "executions": executions,
    }


def _count_executions(executions: list[dict[str, Any]], status: str) -> int:
    return sum(
        1
        for execution in executions
        if str(execution.get("execution_status", "")).strip() == status
    )


def _validate_templates_payload(
    payload: dict[str, Any],
    path: Path,
) -> list[dict[str, Any]]:
    _validate_schema_version(
        payload,
        path,
        expected_schema_version=TEMPLATES_SCHEMA_VERSION,
    )
    templates = payload.get("templates")
    if not isinstance(templates, list):
        raise ValueError(f"{path.name} must contain a templates list")

    validated: list[dict[str, Any]] = []
    for index, template in enumerate(templates):
        if not isinstance(template, dict):
            raise ValueError(f"{path.name} template {index} must be an object")
        _require_fields(
            template,
            REQUIRED_TEMPLATE_FIELDS,
            f"{path.name} template {index}",
        )
        validated.append(copy.deepcopy(template))
    return validated


def _validate_experiments_payload(
    payload: dict[str, Any],
    path: Path,
    templates_by_key: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    _validate_schema_version(
        payload,
        path,
        expected_schema_version=EXPERIMENTS_SCHEMA_VERSION,
    )
    experiments = payload.get("experiments")
    if not isinstance(experiments, list):
        raise ValueError(f"{path.name} must contain an experiments list")

    validated: list[dict[str, Any]] = []
    for index, experiment in enumerate(experiments):
        context = f"{path.name} experiment {index}"
        if not isinstance(experiment, dict):
            raise ValueError(f"{context} must be an object")
        _require_fields(experiment, REQUIRED_EXPERIMENT_FIELDS, context)

        status = experiment["status"]
        if not isinstance(status, str) or status not in ALLOWED_EXPERIMENT_STATUSES:
            raise ValueError(f"{context} has invalid status {status!r}")
        if status != "draft" and experiment["locked"] is not True:
            raise ValueError(f"{context} must be locked when status is {status!r}")

        strategy_id = experiment["strategy_id"]
        strategy_version = experiment["strategy_version"]
        if not isinstance(strategy_id, str) or not isinstance(strategy_version, str):
            raise ValueError(f"{context} has invalid strategy template reference")
        strategy_key = (strategy_id, strategy_version)
        if strategy_key not in templates_by_key:
            raise ValueError(
                f"{context} references unknown strategy template {strategy_key!r}",
            )

        participants = experiment["participants"]
        if not isinstance(participants, list):
            raise ValueError(f"{context} participants must be a list")

        normalized_experiment = copy.deepcopy(experiment)
        experiment_id = _required_string(
            normalized_experiment["experiment_id"],
            f"{context} experiment_id",
        )
        experiment_market = normalize_kelly_market(normalized_experiment["market"])
        expected_currency = kelly_market_currency(experiment_market)
        normalized_experiment["market"] = experiment_market
        normalized_experiment["budget_currency"] = _validate_budget_currency(
            normalized_experiment["budget_currency"],
            expected_currency,
            (
                f"{experiment_id} budget_currency "
                f"{normalized_experiment['budget_currency']} must match "
                f"market {experiment_market} currency {expected_currency}"
            ),
        )
        normalized_experiment["participants"] = _validate_participants(
            participants,
            context,
            experiment_id,
            experiment_market,
            expected_currency,
        )
        normalized_experiment["market_capital_pool"] = kelly_market_capital_pool(
            experiment_market,
        )
        normalized_experiment["template"] = copy.deepcopy(templates_by_key[strategy_key])
        if "lifecycle_states" in normalized_experiment:
            normalized_experiment["lifecycle_states"] = _filter_lifecycle_states_to_participants(
                normalized_experiment["lifecycle_states"],
                normalized_experiment["participants"],
            )
        else:
            normalized_experiment["lifecycle_states"] = build_kelly_lifecycle_states(
                normalized_experiment,
            )
        validated.append(normalized_experiment)
    return validated


def _filter_lifecycle_states_to_participants(
    lifecycle_states: Any,
    participants: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(lifecycle_states, list):
        return []
    allowed = {
        (participant["market"].upper(), participant["symbol"].upper())
        for participant in participants
        if isinstance(participant.get("market"), str)
        and isinstance(participant.get("symbol"), str)
    }
    filtered: list[dict[str, Any]] = []
    for lifecycle_state in lifecycle_states:
        if not isinstance(lifecycle_state, dict):
            continue
        market = lifecycle_state.get("market")
        symbol = lifecycle_state.get("symbol")
        if not isinstance(market, str) or not isinstance(symbol, str):
            continue
        if (market.upper(), symbol.upper()) in allowed:
            filtered.append(copy.deepcopy(lifecycle_state))
    return filtered


def _validate_participants(
    participants: list[Any],
    context: str,
    experiment_id: str,
    experiment_market: str,
    expected_currency: str,
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for index, participant in enumerate(participants):
        participant_context = f"{context} participant {index}"
        if not isinstance(participant, dict):
            raise ValueError(f"{participant_context} must be an object")
        _require_fields(participant, REQUIRED_PARTICIPANT_FIELDS, participant_context)
        if participant["locked"] is not True:
            raise ValueError(f"{participant_context} must be locked")

        normalized = copy.deepcopy(participant)
        normalized["market"] = _uppercase_required_string(
            normalized["market"],
            f"{participant_context} market",
        )
        normalized["symbol"] = _uppercase_required_string(
            normalized["symbol"],
            f"{participant_context} symbol",
        )
        participant_label = f"{normalized['market']}.{normalized['symbol']}"
        if normalized["market"] != experiment_market:
            raise ValueError(
                f"{experiment_id} participant {participant_label} "
                f"must match experiment market {experiment_market}",
            )
        normalized["budget_currency"] = _validate_budget_currency(
            normalized["budget_currency"],
            expected_currency,
            (
                f"{experiment_id} participant {participant_label} "
                f"budget_currency {normalized['budget_currency']} must match "
                f"market {experiment_market} currency {expected_currency}"
            ),
        )
        validated.append(normalized)
    return validated


def _validate_schema_version(
    payload: dict[str, Any],
    path: Path,
    *,
    expected_schema_version: str,
) -> None:
    schema_version = payload.get("schema_version")
    if schema_version != expected_schema_version:
        raise ValueError(
            f"{path.name} schema_version must be {expected_schema_version!r}",
        )


def _index_templates_by_strategy_key(
    templates: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for index, template in enumerate(templates):
        strategy_id = template["strategy_id"]
        if not isinstance(strategy_id, str) or not strategy_id:
            raise ValueError(
                f"kelly_strategy_templates.json template {index} has invalid strategy_id",
            )
        strategy_version = template["strategy_version"]
        if not isinstance(strategy_version, str) or not strategy_version:
            raise ValueError(
                f"kelly_strategy_templates.json template {index} has invalid strategy_version",
            )
        strategy_key = (strategy_id, strategy_version)
        if strategy_key in indexed:
            raise ValueError(
                "kelly_strategy_templates.json contains duplicate "
                f"strategy template {strategy_key!r}",
            )
        indexed[strategy_key] = template
    return indexed


def _require_fields(
    payload: dict[str, Any],
    required_fields: set[str],
    context: str,
) -> None:
    missing = sorted(required_fields - payload.keys())
    if missing:
        raise ValueError(f"{context} missing required fields: {', '.join(missing)}")


def _uppercase_required_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value.strip().upper()


def _required_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value.strip()


def _validate_budget_currency(
    value: Any,
    expected_currency: str,
    message: str,
) -> str:
    currency = _uppercase_required_string(value, "budget_currency")
    if currency != expected_currency:
        raise ValueError(message)
    return currency
