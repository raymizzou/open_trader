from __future__ import annotations

import copy
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping, Sequence


ARTIFACT_SCHEMA_VERSION = "open_trader.decision_plans.v1"
PLAN_SCHEMA_VERSION = "open_trader.decision_plan.v1"
MAX_WEIGHT = Decimal("0.10")
RANGE_ORDER = {"6M": 0, "1Y": 1, "5Y": 2}
FALLBACK_FACT_KEYS = (
    "ma20_distance_pct", "rsi14", "bollinger_position", "relative_volume",
)


def build_decision_plan(
    *,
    run_date: str,
    market: str,
    symbol: str,
    position: Mapping[str, str],
    strategy_snapshots: Sequence[Mapping[str, object]],
    backtests: Sequence[Mapping[str, object]],
    technical_facts: Mapping[str, object],
    tradingagents_summary: Mapping[str, object],
    effective_at: str,
    expires_at: str,
) -> dict[str, object]:
    parsed_date = date.fromisoformat(run_date)
    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip().upper()
    effective = datetime.fromisoformat(effective_at)
    expires = datetime.fromisoformat(expires_at)
    if effective.tzinfo is None or expires.tzinfo is None or effective >= expires:
        raise ValueError("计划生效和过期时间必须是有效的带时区区间")
    if parsed_date != effective.date():
        raise ValueError("计划日期与生效日期不一致")
    if not normalized_market or not normalized_symbol:
        raise ValueError("市场和标的不能为空")

    current_quantity = _decimal(position, "quantity", minimum=Decimal("0"))
    current_weight = _decimal(position, "weight", minimum=Decimal("0"))
    nav = _decimal(position, "nav", minimum=Decimal("0"), strictly_positive=True)
    price = _decimal(position, "price", minimum=Decimal("0"), strictly_positive=True)
    selected = _select_strategy(strategy_snapshots, backtests)
    common = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": f"{normalized_market}.{normalized_symbol}:{run_date}:v1",
        "run_date": run_date,
        "market": normalized_market,
        "symbol": normalized_symbol,
        "status": "waiting",
        "current_quantity": str(current_quantity),
        "current_weight": str(current_weight),
        "current_price": str(price),
        "portfolio_nav": str(nav),
        "max_weight": str(MAX_WEIGHT),
        "risk_status": "overweight_no_add" if current_weight > MAX_WEIGHT else "within_limit",
        "effective_at": effective_at,
        "expires_at": expires_at,
    }

    if selected is None:
        facts = []
        for key in FALLBACK_FACT_KEYS:
            value = technical_facts.get(key)
            if not isinstance(value, Mapping) or value.get("calculated_value") is None:
                raise ValueError(f"缺少兜底事实：{key}")
            facts.append({"key": key, **copy.deepcopy(dict(value))})
        record = {
            **common,
            "mode": "fallback_advice",
            "action_summary": "没有通过回测闸门的可执行策略",
            "next_condition_id": "",
            "strategy": {},
            "conditions": [],
            "backtests": _ordered_backtests(backtests),
            "fallback": {
                "label": "非执行型建议",
                "reason": "没有策略通过当前回测闸门",
                "recommendation": "考虑降低风险" if current_weight > MAX_WEIGHT else "禁止加仓",
                "facts": facts,
                "tradingagents": copy.deepcopy(dict(tradingagents_summary)),
                "max_weight": str(MAX_WEIGHT),
            },
        }
    else:
        snapshot, selected_backtests = selected
        conditions: list[dict[str, object]] = []
        for raw in snapshot.get("conditions", []):
            condition = copy.deepcopy(dict(raw))
            target_weight = _decimal(condition, "target_weight", minimum=Decimal("0"))
            if target_weight > MAX_WEIGHT:
                target_weight = MAX_WEIGHT
            if current_weight > MAX_WEIGHT and str(condition.get("suggested_action")) in {
                "买入", "加仓", "建立观察仓",
            }:
                continue
            condition["target_weight"] = str(target_weight)
            condition["target_quantity"] = str(
                (nav * target_weight / price).quantize(Decimal("1"), rounding=ROUND_DOWN)
            )
            conditions.append(condition)
        record = {
            **common,
            "mode": "validated_plan",
            "action_summary": "继续持有，等待条件触发",
            "next_condition_id": str(conditions[0]["condition_id"]) if conditions else "",
            "strategy": copy.deepcopy(dict(snapshot["strategy"])),
            "conditions": conditions,
            "backtests": _ordered_backtests(selected_backtests),
            "fallback": None,
        }
    validate_decision_plan(record)
    return record


def validate_decision_plan(record: Mapping[str, object]) -> None:
    if record.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("decision plan schema_version 无效")
    run_date = str(record.get("run_date") or "")
    date.fromisoformat(run_date)
    market = str(record.get("market") or "")
    symbol = str(record.get("symbol") or "")
    expected_id = f"{market}.{symbol}:{run_date}:v1"
    if record.get("plan_id") != expected_id:
        raise ValueError("plan_id 与市场、标的或日期不一致")
    for key in (
        "current_quantity", "current_weight", "current_price", "portfolio_nav", "max_weight",
    ):
        _decimal(record, key, minimum=Decimal("0"))
    effective = datetime.fromisoformat(str(record.get("effective_at") or ""))
    expires = datetime.fromisoformat(str(record.get("expires_at") or ""))
    if effective.tzinfo is None or expires.tzinfo is None or effective >= expires:
        raise ValueError("计划时间区间无效")

    mode = record.get("mode")
    conditions = record.get("conditions")
    if not isinstance(conditions, list):
        raise ValueError("conditions 必须是数组")
    backtests = record.get("backtests")
    if not isinstance(backtests, list):
        raise ValueError("backtests 必须是数组")
    _validate_backtests(backtests)
    if mode == "fallback_advice":
        fallback = record.get("fallback")
        if conditions or not isinstance(fallback, Mapping):
            raise ValueError("fallback_advice 不能包含可执行条件")
        facts = fallback.get("facts")
        if not isinstance(facts, list):
            raise ValueError("fallback facts 必须是数组")
        for fact in facts:
            if (
                not isinstance(fact, Mapping)
                or not fact.get("formula")
                or not isinstance(fact.get("inputs"), Mapping)
                or not fact.get("source_date")
                or fact.get("calculated_value") is None
            ):
                raise ValueError("兜底事实缺少参数来源")
        return
    if mode != "validated_plan" or not isinstance(record.get("strategy"), Mapping):
        raise ValueError("decision plan mode 无效")
    if record.get("fallback") is not None:
        raise ValueError("validated_plan 不能包含 fallback")
    seen: set[str] = set()
    ordinary_seen = False
    for condition in conditions:
        if not isinstance(condition, Mapping):
            raise ValueError("condition 必须是对象")
        condition_id = str(condition.get("condition_id") or "")
        if not condition_id or condition_id in seen:
            raise ValueError("condition_id 缺失或重复")
        seen.add(condition_id)
        priority = condition.get("priority")
        ordinary_seen = ordinary_seen or priority == "ordinary"
        if priority not in {"risk", "ordinary"} or ordinary_seen and priority == "risk":
            raise ValueError("condition 优先级顺序无效")
        for key in ("calculated_value", "target_weight", "target_quantity"):
            _decimal(condition, key, minimum=Decimal("0"))
        if _decimal(condition, "target_weight") > MAX_WEIGHT:
            raise ValueError("condition target_weight 超过 10%")
        if not condition.get("formula") or not isinstance(condition.get("inputs"), Mapping) or not condition.get("source_date"):
            raise ValueError("condition 缺少参数来源")
    if not backtests:
        raise ValueError("validated_plan 缺少回测证据")
    if not all(isinstance(item, Mapping) and isinstance(item.get("gate"), Mapping) and item["gate"].get("passed") is True for item in backtests):
        raise ValueError("validated_plan 包含未通过的回测")


def _validate_backtests(backtests: Sequence[object]) -> None:
    for item in backtests:
        if not isinstance(item, Mapping):
            raise ValueError("backtest 必须是对象")
        strategy = item.get("strategy")
        if not isinstance(strategy, Mapping):
            raise ValueError("backtest strategy 缺失")
        for key in ("total_return_pct", "max_drawdown_pct"):
            _decimal(strategy, key)
        for key in ("sharpe_ratio", "calmar_ratio"):
            if strategy.get(key) is not None:
                _decimal(strategy, key)
        benchmark = item.get("market_benchmark")
        if benchmark is not None:
            if not isinstance(benchmark, Mapping):
                raise ValueError("market_benchmark 必须是对象")
            _decimal(benchmark, "total_return_pct")
        excess = item.get("market_excess_return_pct")
        if excess is not None:
            _decimal(item, "market_excess_return_pct")


def publish_decision_plans(
    *,
    data_dir: Path,
    run_date: str,
    market: str,
    records: Sequence[Mapping[str, object]],
    update_latest: bool,
) -> tuple[Path, Path]:
    date.fromisoformat(run_date)
    normalized_market = market.strip().upper()
    identities: set[tuple[str, str]] = set()
    normalized: list[dict[str, object]] = []
    for record in records:
        validate_decision_plan(record)
        if record.get("run_date") != run_date or record.get("market") != normalized_market:
            raise ValueError("计划与发布日期或市场不一致")
        identity = (str(record["market"]), str(record["symbol"]))
        if identity in identities:
            raise ValueError("同一交易日存在重复标的计划")
        identities.add(identity)
        normalized.append(copy.deepcopy(dict(record)))
    payload = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "run_date": run_date,
        "market": normalized_market,
        "records": normalized,
    }
    run_path = data_dir / "runs" / run_date / normalized_market / "decision_plans.json"
    latest_path = data_dir / "latest" / normalized_market / "decision_plans.json"
    _atomic_json(run_path, payload)
    if update_latest:
        _atomic_json(latest_path, payload)
    return run_path, latest_path


def load_decision_plans(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 decision plans：{path}") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("decision plans artifact schema_version 无效")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("decision plans records 必须是数组")
    seen: set[tuple[str, str, str]] = set()
    output: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("decision plan record 必须是对象")
        validate_decision_plan(record)
        identity = (str(record["run_date"]), str(record["market"]), str(record["symbol"]))
        if identity in seen:
            raise ValueError("decision plans 包含重复标的")
        seen.add(identity)
        output.append(dict(record))
    return output


def _select_strategy(
    snapshots: Sequence[Mapping[str, object]],
    backtests: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], list[Mapping[str, object]]] | None:
    candidates: list[tuple[Decimal, int, Mapping[str, object], list[Mapping[str, object]]]] = []
    for index, snapshot in enumerate(snapshots):
        strategy = snapshot.get("strategy")
        if not isinstance(strategy, Mapping):
            continue
        strategy_id = str(strategy.get("id") or "")
        evidence = [item for item in backtests if item.get("strategy_id") == strategy_id]
        by_range = {str(item.get("range")): item for item in evidence}
        required = {"6M", "1Y"} | ({"5Y"} if "5Y" in by_range else set())
        if not required <= by_range.keys():
            continue
        selected = [by_range[name] for name in sorted(required, key=RANGE_ORDER.__getitem__)]
        if not all(isinstance(item.get("gate"), Mapping) and item["gate"].get("passed") is True for item in selected):
            continue
        try:
            excess = Decimal(str(by_range["1Y"].get("market_excess_return_pct")))
        except InvalidOperation:
            continue
        if not excess.is_finite():
            continue
        candidates.append((excess, -index, snapshot, selected))
    if not candidates:
        return None
    _, _, snapshot, selected = max(candidates, key=lambda item: (item[0], item[1]))
    return snapshot, selected


def _ordered_backtests(backtests: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        copy.deepcopy(dict(item))
        for item in sorted(backtests, key=lambda item: RANGE_ORDER.get(str(item.get("range")), 99))
    ]


def _decimal(
    source: Mapping[str, object],
    key: str,
    *,
    minimum: Decimal | None = None,
    strictly_positive: bool = False,
) -> Decimal:
    raw = source.get(key)
    if not isinstance(raw, str):
        raise ValueError(f"{key} 必须是十进制字符串")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{key} 必须是有效十进制字符串") from exc
    if not value.is_finite() or minimum is not None and value < minimum or strictly_positive and value <= 0:
        raise ValueError(f"{key} 数值无效")
    return value


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
