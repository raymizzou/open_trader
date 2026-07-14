from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
import uuid

from .standard_strategies import StrategyBar


STRATEGY_ID = "tiger_sma200_equal_weight/v1"
SYMBOL_CAP = Decimal("0.10")
RISK_GROUP_CAP = Decimal("0.30")
DRIFT_TOLERANCE = Decimal("0.02")
TIGER_STRATEGY_SCHEMA = "open_trader.tiger_long_term_strategy.v1"
COST_MODEL_ID = "tiger_hk_us_online/2026-07-14"


@dataclass(frozen=True)
class TigerLongTermConfig:
    strategy_id: str
    account_alias: str
    members: Mapping[str, str]


@dataclass(frozen=True)
class TigerLongTermResult:
    status: str
    member_count: int
    eligible_count: int
    run_path: Path
    latest_path: Path | None


def load_tiger_long_term_config(path: Path) -> TigerLongTermConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Tiger 长线策略配置无效") from exc
    strategy_id = str(payload.get("strategy_id") or "") if isinstance(payload, dict) else ""
    account_alias = str(payload.get("account_alias") or "") if isinstance(payload, dict) else ""
    members = payload.get("members") if isinstance(payload, dict) else None
    if (
        strategy_id != STRATEGY_ID
        or not account_alias
        or not isinstance(members, dict)
        or not members
    ):
        raise ValueError("Tiger 长线策略配置无效")
    normalized = {
        str(symbol).strip().upper(): str(group).strip()
        for symbol, group in members.items()
    }
    if any(not symbol or not group for symbol, group in normalized.items()):
        raise ValueError("Tiger 长线策略配置无效")
    return TigerLongTermConfig(strategy_id, account_alias, normalized)


def sma200_state(bars: Sequence[StrategyBar]) -> str:
    if len(bars) < 201:
        return "INELIGIBLE"
    sma200 = sum((bar.close for bar in bars[-201:-1]), Decimal("0")) / Decimal(200)
    return "LONG" if bars[-1].close > sma200 else "CASH"


def allocate_target_weights(
    states: Mapping[str, str],
    risk_groups: Mapping[str, str],
) -> dict[str, Decimal]:
    long_symbols = [symbol for symbol, state in states.items() if state == "LONG"]
    if not long_symbols:
        return {symbol: Decimal("0") for symbol in states}
    if any(symbol not in risk_groups for symbol in long_symbols):
        raise ValueError("Tiger 长线策略成员缺少风险组")
    weight = min(SYMBOL_CAP, Decimal("1") / Decimal(len(long_symbols)))
    targets = {
        symbol: weight if symbol in long_symbols else Decimal("0")
        for symbol in states
    }
    by_group: dict[str, list[str]] = {}
    for symbol in long_symbols:
        by_group.setdefault(risk_groups[symbol], []).append(symbol)
    for symbols in by_group.values():
        total = sum((targets[symbol] for symbol in symbols), Decimal("0"))
        if total <= RISK_GROUP_CAP:
            continue
        scale = RISK_GROUP_CAP / total
        for symbol in symbols:
            targets[symbol] *= scale
    return targets


def rebalance_reasons(
    actual: Mapping[str, Decimal],
    target: Mapping[str, Decimal],
    previous_states: Mapping[str, str],
    states: Mapping[str, str],
    risk_groups: Mapping[str, str],
) -> dict[str, str]:
    symbols = set(actual) | set(target) | set(states)
    group_weights: dict[str, Decimal] = {}
    for symbol, weight in actual.items():
        group = risk_groups.get(symbol)
        if group:
            group_weights[group] = group_weights.get(group, Decimal("0")) + weight
    reasons: dict[str, str] = {}
    for symbol in symbols:
        current = actual.get(symbol, Decimal("0"))
        desired = target.get(symbol, Decimal("0"))
        if previous_states.get(symbol) != states.get(symbol):
            reasons[symbol] = "state_change"
        elif current > SYMBOL_CAP:
            reasons[symbol] = "symbol_cap"
        elif group_weights.get(risk_groups.get(symbol, ""), Decimal("0")) > RISK_GROUP_CAP:
            reasons[symbol] = "risk_group_cap"
        elif abs(current - desired) > DRIFT_TOLERANCE:
            reasons[symbol] = "drift"
    return reasons


def generate_tiger_long_term_strategy(
    run_date: str,
    data_dir: Path,
    config_path: Path,
    price_provider: object,
    *,
    update_latest: bool,
) -> TigerLongTermResult:
    run_path = (
        data_dir / "runs" / run_date / "US" / "tiger_long_term_strategy.json"
    )
    latest_path = data_dir / "latest" / "US" / "tiger_long_term_strategy.json"
    try:
        payload = _build_tiger_long_term_payload(
            run_date,
            data_dir,
            config_path,
            price_provider,
            latest_path,
        )
    except Exception as exc:
        failure = {
            "schema_version": TIGER_STRATEGY_SCHEMA,
            "generated_at": _now_iso(),
            "run_date": run_date,
            "status": "failed",
            "error": str(exc) or exc.__class__.__name__,
            "order_requests": [],
        }
        _atomic_json(run_path, failure)
        return TigerLongTermResult("failed", 0, 0, run_path, None)

    _atomic_json(run_path, payload)
    promoted: Path | None = None
    if update_latest:
        _atomic_json(latest_path, payload)
        promoted = latest_path
    return TigerLongTermResult(
        "shadow",
        len(payload["members"]),
        sum(1 for member in payload["members"] if member["eligible"]),
        run_path,
        promoted,
    )


def load_tiger_long_term_strategy(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Tiger 长线策略产物无效") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != TIGER_STRATEGY_SCHEMA
        or payload.get("status") != "shadow"
        or not isinstance(payload.get("members"), list)
        or not isinstance(payload.get("gate"), dict)
        or not isinstance(payload.get("validation"), dict)
        or payload.get("order_requests") != []
    ):
        raise ValueError("Tiger 长线策略产物无效")
    return payload


def _build_tiger_long_term_payload(
    run_date_text: str,
    data_dir: Path,
    config_path: Path,
    price_provider: object,
    latest_path: Path,
) -> dict[str, Any]:
    from .tiger_long_term_backtest import (
        build_validation_gate,
        ensure_dgs3mo_rates,
        run_spy_buy_hold_backtest,
        run_tiger_long_term_backtest,
    )

    run_day = date.fromisoformat(run_date_text)
    config = load_tiger_long_term_config(config_path)
    snapshot_path = data_dir / "runs" / run_date_text / "tiger_account_snapshot.json"
    snapshot = _load_snapshot(snapshot_path)
    nav, market_values = _tiger_account_values(snapshot, config.account_alias)
    start_day = _shift_year(run_day, -6)
    evaluation_start = _shift_year(run_day, -5)

    source_bars: dict[str, list[StrategyBar]] = {}
    validation_bars: dict[str, list[StrategyBar]] = {}
    source_hashes: dict[str, dict[str, str]] = {}
    eligibility: dict[str, tuple[bool, str]] = {}
    fetch_symbols = [*config.members, "SPY"]
    for symbol in fetch_symbols:
        futu_symbol = f"US.{symbol}"
        raw_bars = price_provider.get_daily_kline(
            futu_symbol,
            start=start_day.isoformat(),
            end=run_date_text,
        )
        rehab_rows = price_provider.get_rehab_rows(futu_symbol)
        bars = _to_strategy_bars(raw_bars)
        if not bars:
            raise ValueError(f"{symbol} has no usable QFQ history")
        source_bars[symbol] = bars
        source_hashes[symbol] = {
            "prices_sha256": _json_hash([_bar_payload(bar) for bar in bars]),
            "rehab_sha256": _json_hash(rehab_rows),
        }
        before_evaluation = sum(bar.date < evaluation_start for bar in bars)
        fresh = bars[-1].date >= run_day - timedelta(days=7)
        validation_ok = before_evaluation >= 200 and fresh
        if symbol != "SPY":
            if len(bars) < 201:
                eligibility[symbol] = (False, "insufficient_sma200_history")
            elif not fresh:
                eligibility[symbol] = (False, "stale_price")
            else:
                eligibility[symbol] = (True, "")
            if validation_ok:
                validation_bars[symbol] = bars

    if not validation_bars:
        raise ValueError("Tiger strategy has no members with validation history")
    rates, rates_hash = ensure_dgs3mo_rates(data_dir, run_day)
    validation_inputs = {
        "strategy_id": config.strategy_id,
        "cost_model_id": COST_MODEL_ID,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "prices": source_hashes,
        "dgs3mo_sha256": rates_hash,
    }
    validation_hash = _json_hash(validation_inputs)
    cached_validation = _reusable_validation(latest_path, run_date_text, validation_hash)
    if cached_validation is None:
        backtest = run_tiger_long_term_backtest(
            validation_bars,
            {symbol: config.members[symbol] for symbol in validation_bars},
            rates,
            initial_cash=nav,
        )
        spy_backtest = run_spy_buy_hold_backtest(
            source_bars["SPY"],
            rates,
            initial_cash=nav,
        )
        strategy_summary = _backtest_summary(backtest["strategy"])
        benchmark_summary = _backtest_summary(backtest["benchmark"])
        spy_summary = _backtest_summary(spy_backtest)
        provenance_ok = (
            len(validation_bars) == len(config.members)
            and len(source_hashes) == len(config.members) + 1
        )
        prior_rate_dates = [day for day in rates if day <= evaluation_start]
        if not prior_rate_dates:
            raise ValueError("DGS3MO has no rate on or before evaluation start")
        cash_return = rates[max(prior_rate_dates)]
        gate = build_validation_gate(
            strategy_summary,
            benchmark_summary,
            cash_annualized_return_pct=cash_return,
            provenance_ok=provenance_ok,
        )
        validation = {
            "strategy": strategy_summary,
            "benchmark": benchmark_summary,
            "spy": spy_summary,
            "gate": gate,
            "cash_annualized_return_pct": _decimal_text(cash_return),
            "conditional_universe": True,
            "selection_validated": False,
        }
        validation_reused = False
    else:
        validation = cached_validation
        validation_reused = True

    states = {
        symbol: sma200_state(source_bars[symbol])
        if eligibility[symbol][0]
        else "INELIGIBLE"
        for symbol in config.members
    }
    targets = allocate_target_weights(states, config.members)
    actual = {
        symbol: market_values.get(symbol, Decimal("0")) / nav
        for symbol in config.members
    }
    previous_states = _previous_states(latest_path)
    reasons = rebalance_reasons(actual, targets, previous_states, states, config.members)
    members = []
    for symbol, risk_group in config.members.items():
        eligible, eligibility_reason = eligibility[symbol]
        members.append({
            "symbol": symbol,
            "risk_group": risk_group,
            "eligible": eligible,
            "eligibility_reason": eligibility_reason,
            "validation_eligible": symbol in validation_bars,
            "trend": states[symbol],
            "actual_weight": _decimal_text(actual[symbol]),
            "target_weight": _decimal_text(targets[symbol]),
            "drift": _decimal_text(actual[symbol] - targets[symbol]),
            "rebalance_reason": reasons.get(symbol, ""),
        })

    return {
        "schema_version": TIGER_STRATEGY_SCHEMA,
        "generated_at": _now_iso(),
        "run_date": run_date_text,
        "status": "shadow",
        "strategy_id": config.strategy_id,
        "account_alias": config.account_alias,
        "nav": _decimal_text(nav),
        "validation_hash": validation_hash,
        "validation_reused": validation_reused,
        "sources": {
            "tiger_snapshot": {
                "path": str(snapshot_path),
                "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            },
            "config": {
                "path": str(config_path),
                "sha256": validation_inputs["config_sha256"],
            },
            "qfq_and_rehab": source_hashes,
            "dgs3mo": {
                "path": str(data_dir / "rates" / "DGS3MO.csv"),
                "sha256": rates_hash,
            },
            "cost_model_id": COST_MODEL_ID,
        },
        "members": members,
        "validation": validation,
        "strategy": validation["strategy"],
        "benchmark": validation["benchmark"],
        "spy": validation["spy"],
        "gate": validation["gate"],
        "notes": [
            "条件验证，不含选股",
            "仅供人工复核",
            "实际权重可能在收盘与下一可执行开盘之间短暂越过硬上限",
        ],
        "order_requests": [],
    }


def _load_snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Tiger account snapshot is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("Tiger account snapshot is missing or invalid")
    return payload


def _tiger_account_values(
    snapshot: Mapping[str, Any],
    account_alias: str,
) -> tuple[Decimal, dict[str, Decimal]]:
    cash_records = snapshot.get("cash_records")
    position_records = snapshot.get("position_records")
    if not isinstance(cash_records, list) or not isinstance(position_records, list):
        raise ValueError("Tiger account snapshot records are invalid")
    matches = [
        row for row in cash_records
        if isinstance(row, dict)
        and row.get("record_type") == "account_total"
        and row.get("account_alias") == account_alias
        and row.get("currency") == "USD"
    ]
    if len(matches) != 1:
        raise ValueError(f"Tiger account_total is missing for {account_alias}")
    nav = _positive_decimal(matches[0].get("account_total"), "Tiger account_total")
    values: dict[str, Decimal] = {}
    for row in position_records:
        if (
            not isinstance(row, dict)
            or row.get("account_alias") != account_alias
            or row.get("market") != "US"
        ):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        value = _non_negative_decimal(row.get("market_value"), "Tiger market_value")
        values[symbol] = values.get(symbol, Decimal("0")) + value
    return nav, values


def _to_strategy_bars(raw_bars: Sequence[object]) -> list[StrategyBar]:
    converted: list[StrategyBar] = []
    for row in raw_bars:
        raw_open = getattr(row, "open", None)
        raw_high = getattr(row, "high", None)
        raw_low = getattr(row, "low", None)
        if raw_open is None or raw_high is None or raw_low is None:
            continue
        try:
            converted.append(StrategyBar(
                date=date.fromisoformat(str(getattr(row, "date"))[:10]),
                open=Decimal(str(raw_open)),
                high=Decimal(str(raw_high)),
                low=Decimal(str(raw_low)),
                close=Decimal(str(getattr(row, "close"))),
                volume=Decimal(str(getattr(row, "volume"))),
            ))
        except (ValueError, ArithmeticError, AttributeError):
            continue
    return sorted(converted, key=lambda bar: bar.date)


def _bar_payload(bar: StrategyBar) -> dict[str, str]:
    return {
        "date": bar.date.isoformat(),
        "open": _decimal_text(bar.open),
        "high": _decimal_text(bar.high),
        "low": _decimal_text(bar.low),
        "close": _decimal_text(bar.close),
        "volume": _decimal_text(bar.volume),
    }


def _backtest_summary(payload: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "total_return_pct",
        "annualized_return_pct",
        "max_drawdown_pct",
        "sharpe_ratio",
        "calmar_ratio",
        "cash_interest",
        "fees",
        "slippage_cost",
        "costs",
        "turnover_pct",
        "time_in_market_pct",
        "round_trips",
        "profit_contributions",
        "segments",
    )
    return {key: payload.get(key) for key in keys}


def _reusable_validation(
    latest_path: Path,
    run_date_text: str,
    validation_hash: str,
) -> dict[str, Any] | None:
    if not latest_path.exists():
        return None
    try:
        previous = load_tiger_long_term_strategy(latest_path)
    except ValueError:
        return None
    if (
        str(previous.get("run_date", ""))[:7] != run_date_text[:7]
        or previous.get("validation_hash") != validation_hash
        or not isinstance(previous.get("validation"), dict)
    ):
        return None
    return previous["validation"]


def _previous_states(latest_path: Path) -> dict[str, str]:
    if not latest_path.exists():
        return {}
    try:
        previous = load_tiger_long_term_strategy(latest_path)
    except ValueError:
        return {}
    return {
        str(member.get("symbol")): str(member.get("trend"))
        for member in previous["members"]
        if isinstance(member, dict) and member.get("symbol")
    }


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _json_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_decimal(value: object, label: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return parsed


def _non_negative_decimal(value: object, label: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be non-negative and finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _shift_year(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
