from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo


EVIDENCE_SCHEMA_VERSION = "open_trader.trend_review.evidence.v1"
REPLAY_SCHEMA_VERSION = "open_trader.trend_review.replay.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
BENCHMARK_SOURCE_IDS = {
    "CN": "CSI_ALL_SHARE_PRICE",
    "US": "SPY_QFQ",
    "HK": "HSCI_PRICE",
}
BENCHMARK_FUTU_SYMBOLS = {
    "CN": "SH.000985",
    "US": "US.SPY",
    "HK": "HK.800701",
}


class TrendReplayIncompleteError(ValueError):
    pass


class TrendReviewAccountStateError(ValueError):
    pass


def benchmark_fact(
    quote: object, market: str, trading_date: str
) -> dict[str, str]:
    market = _market(market)
    symbol = BENCHMARK_FUTU_SYMBOLS[market]
    bars = quote.get_daily_kline(symbol, start=trading_date, end=trading_date)
    bar = next((item for item in bars if item.date == trading_date), None)
    if bar is None:
        raise ValueError(f"benchmark is missing {trading_date}")
    close = _required_decimal(bar.close, "benchmark close")
    if close <= 0:
        raise ValueError("benchmark close must be positive")
    return {
        "date": trading_date,
        "close": format(close.normalize(), "f"),
        "source_id": BENCHMARK_SOURCE_IDS[market],
        "futu_symbol": symbol,
    }


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            _json_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _write_immutable(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != body:
            raise FileExistsError(f"immutable artifact collision: {path}") from None
        return path
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _market(value: object) -> str:
    market = str(value).upper()
    if market not in {"CN", "US", "HK"}:
        raise ValueError(f"unsupported trend review market: {value}")
    return market


def freeze_trend_evidence(
    data_dir: Path, evidence: Mapping[str, object]
) -> dict[str, str]:
    payload = {"schema_version": EVIDENCE_SCHEMA_VERSION, **dict(evidence)}
    body = _canonical_json_bytes(payload)
    digest = hashlib.sha256(body).hexdigest()
    path = (
        data_dir
        / "trend_review"
        / "evidence"
        / _market(payload.get("market"))
        / f"{digest}.json"
    )
    _write_immutable(path, body)
    return {"path": str(path), "sha256": digest}


def freeze_report_evidence(
    *,
    data_dir: Path,
    report: object,
    candidates: object,
    holding_snapshots: object,
    bars_by_symbol: object,
    prior_state: object,
    watch_events: object,
    query: Mapping[str, object],
    responses: Mapping[str, object],
    candidate_pool_ids: object,
    lot_sizes: Mapping[str, int],
) -> dict[str, str]:
    metadata = getattr(report, "metadata")
    strategy_snapshot = getattr(report, "strategy_snapshot")
    evidence = {
        "market": str(metadata.get("market") or "CN"),
        "report_id": getattr(report, "as_of_date"),
        "query": dict(query),
        "responses": dict(responses),
        "market_data": bars_by_symbol,
        "account": getattr(report, "account"),
        "strategy_snapshot": strategy_snapshot,
        "process_version": str(strategy_snapshot.get("process_version") or ""),
        "rebuild_inputs": {
            "as_of_date": getattr(report, "as_of_date"),
            "execution_date": getattr(report, "execution_date"),
            "account": getattr(report, "account"),
            "candidates": candidates,
            "holding_snapshots": holding_snapshots,
            "bars_by_symbol": bars_by_symbol,
            "prior_state": prior_state,
            "watch_events": watch_events,
            "api_facts": getattr(report, "api_facts"),
            "data_sources": getattr(report, "data_sources"),
            "estimated_api_cost": getattr(report, "estimated_api_cost"),
            "actual_api_cost": getattr(report, "actual_api_cost"),
            "market": str(metadata.get("market") or "CN"),
            "lot_sizes": dict(lot_sizes),
            "position_weight": metadata.get("position_weight"),
            "position_weight_source": metadata.get("position_weight_source"),
            "candidate_pool_ids": candidate_pool_ids,
            "generated_at": getattr(report, "generated_at"),
            "metadata": metadata,
        },
    }
    return freeze_trend_evidence(data_dir, evidence)


def _load_valid_evidence(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid trend review evidence: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION
    ):
        raise ValueError(f"invalid trend review evidence: {path}")
    _market(payload.get("market"))
    return payload


def replay_trend_evidence(
    evidence_path: Path,
    data_dir: Path,
    *,
    fixed_process_version: str,
    rebuild: Callable[[dict[str, object]], dict[str, object]],
    replayed_at: str | None = None,
) -> Path:
    original = _load_valid_evidence(evidence_path)
    replay_input = copy.deepcopy(original)
    replay_input["process_version"] = fixed_process_version
    corrected = rebuild(replay_input)
    payload = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "market": original["market"],
        "original_evidence_path": str(evidence_path),
        "original_evidence_sha256": hashlib.sha256(
            evidence_path.read_bytes()
        ).hexdigest(),
        "fixed_process_version": fixed_process_version,
        "replayed_at": replayed_at
        or datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "corrected_report": corrected,
    }
    body = _canonical_json_bytes(payload)
    digest = hashlib.sha256(body).hexdigest()
    return _write_immutable(
        data_dir
        / "trend_review"
        / "replays"
        / _market(original["market"])
        / f"{digest}.json",
        body,
    )


def _required_decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def _report_hash(report: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(report)).hexdigest()


def _positive_positions(snapshot: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = snapshot.get("positions")
    if not isinstance(raw, list):
        raise TrendReviewAccountStateError("simulate account positions are unavailable")
    positions: list[Mapping[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise TrendReviewAccountStateError("simulate account positions are invalid")
        if _required_decimal(item.get("qty", item.get("quantity", "0")), "position qty") > 0:
            positions.append(item)
    return positions


def _ensure_experiment_account(
    data_dir: Path,
    market: str,
    snapshot: Mapping[str, object],
) -> None:
    root = data_dir / "trend_review" / "ledgers" / market
    started = root / "started.json"
    account_id = int(snapshot.get("acc_id") or 0)
    if account_id <= 0:
        raise TrendReviewAccountStateError("simulate account ID is unavailable")
    positions = _positive_positions(snapshot)
    if not started.exists():
        if positions:
            raise TrendReviewAccountStateError(
                "simulate account must start with zero positions"
            )
        _write_immutable(
            started,
            _canonical_json_bytes(
                {"market": market, "acc_id": account_id, "started_at": "first-open"}
            ),
        )
        return
    existing = json.loads(started.read_text(encoding="utf-8"))
    if existing.get("acc_id") != account_id:
        raise TrendReviewAccountStateError("simulate account changed during experiment")
    known_codes = {
        str(payload.get("request", {}).get("futu_code") or "")
        for path in root.glob("open/*/*-intent.json")
        for payload in [json.loads(path.read_text(encoding="utf-8"))]
        if isinstance(payload, dict) and isinstance(payload.get("request"), dict)
    }
    unexplained = [
        str(item.get("code") or item.get("futu_code") or "")
        for item in positions
        if str(item.get("code") or item.get("futu_code") or "") not in known_codes
    ]
    if unexplained:
        raise TrendReviewAccountStateError(
            "simulate account contains positions outside this experiment"
        )


def execute_trend_review_open(
    *,
    data_dir: Path,
    report: Mapping[str, object],
    client: object,
    prices: Mapping[str, Decimal],
    market: str,
    execution_date: str,
    now: str,
) -> dict[str, object]:
    market = _market(market)
    current = datetime.fromisoformat(now)
    if current.date().isoformat() != execution_date:
        raise ValueError("execution date does not match current time")
    if market in {"CN", "HK"} and not (
        current.time().replace(tzinfo=None)
        >= datetime.strptime("09:30", "%H:%M").time()
        and current.time().replace(tzinfo=None)
        <= datetime.strptime("10:00", "%H:%M").time()
    ):
        return {
            "status": "missed_window",
            "market": market,
            "date": execution_date,
            "submitted_count": 0,
        }
    snapshot = client.account_snapshot()
    if not isinstance(snapshot, Mapping):
        raise TrendReviewAccountStateError("simulate account snapshot is invalid")
    _ensure_experiment_account(data_dir, market, snapshot)
    nav = _required_decimal(snapshot.get("net_value"), "simulate net value")
    if nav <= 0:
        raise TrendReviewAccountStateError("simulate net value must be positive")
    judgments = report.get("strategy_judgments")
    actions = judgments.get("formal_actions") if isinstance(judgments, Mapping) else None
    if not isinstance(actions, list):
        raise ValueError("trend report formal actions are unavailable")

    from .futu_symbols import to_futu_symbol

    report_sha = _report_hash(report)
    submitted = 0
    artifacts: list[str] = []
    root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "open"
        / execution_date
    )
    for index, action in enumerate(actions):
        if not isinstance(action, Mapping) or action.get("action") != "BUY":
            continue
        symbol = str(action.get("symbol") or "").strip()
        price = _required_decimal(prices.get(symbol), f"price for {symbol}")
        weight = _required_decimal(action.get("target_weight"), "target weight")
        lot_size = int(action.get("lot_size") or 0)
        if not symbol or price <= 0 or weight <= 0 or lot_size <= 0:
            raise ValueError("trend review buy action is invalid")
        quantity = int(
            (nav * weight / price / Decimal(lot_size)).to_integral_value(
                rounding=ROUND_DOWN
            )
        ) * lot_size
        if quantity <= 0:
            continue
        request = {
            "market": market,
            "futu_code": to_futu_symbol(market, symbol),
            "side": "buy",
            "order_type": "MARKET",
            "price": "0",
            "qty": str(quantity),
            "remark": f"trend-review:{market}:{execution_date}:{index}",
        }
        stem = f"{report_sha}-{index}"
        intent_path = root / f"{stem}-intent.json"
        if intent_path.exists():
            continue
        _write_immutable(
            intent_path,
            _canonical_json_bytes(
                {
                    "market": market,
                    "date": execution_date,
                    "report_sha256": report_sha,
                    "action_index": index,
                    "request": request,
                    "created_at": now,
                }
            ),
        )
        response = client.place_order(request)
        result_path = root / f"{stem}-result.json"
        _write_immutable(
            result_path,
            _canonical_json_bytes(
                {
                    "market": market,
                    "date": execution_date,
                    "request": request,
                    "response": response,
                    "submitted_at": now,
                }
            ),
        )
        artifacts.append(str(result_path))
        submitted += 1
    return {
        "status": "submitted" if submitted else "unchanged",
        "market": market,
        "date": execution_date,
        "submitted_count": submitted,
        "artifact_paths": artifacts,
    }


def execute_trend_review_stop(
    *,
    data_dir: Path,
    market: str,
    symbol: str,
    trading_date: str,
    event_id: str,
    client: object,
    now: str,
) -> dict[str, object]:
    market = _market(market)
    root = data_dir / "trend_review" / "ledgers" / market / "stops"
    intent_path = root / f"{hashlib.sha256(event_id.encode()).hexdigest()}-intent.json"
    if intent_path.exists():
        return {
            "status": "unchanged",
            "market": market,
            "date": trading_date,
            "submitted_count": 0,
        }
    snapshot = client.account_snapshot()
    if not isinstance(snapshot, Mapping):
        raise TrendReviewAccountStateError("simulate account snapshot is invalid")
    from .futu_symbols import to_futu_symbol

    futu_code = to_futu_symbol(market, symbol)
    position = next(
        (
            item
            for item in _positive_positions(snapshot)
            if str(item.get("code") or item.get("futu_code") or "") == futu_code
        ),
        None,
    )
    if position is None:
        return {
            "status": "no_position",
            "market": market,
            "date": trading_date,
            "submitted_count": 0,
        }
    quantity = _required_decimal(
        position.get("qty", position.get("quantity")), "position qty"
    )
    request = {
        "market": market,
        "futu_code": futu_code,
        "side": "sell",
        "order_type": "MARKET",
        "price": "0",
        "qty": format(quantity, "f"),
        "remark": f"trend-review:{market}:{event_id}",
    }
    _write_immutable(
        intent_path,
        _canonical_json_bytes(
            {
                "market": market,
                "date": trading_date,
                "event_id": event_id,
                "request": request,
                "created_at": now,
            }
        ),
    )
    response = client.place_order(request)
    result_path = intent_path.with_name(intent_path.name.replace("-intent", "-result"))
    _write_immutable(
        result_path,
        _canonical_json_bytes({"request": request, "response": response}),
    )
    return {
        "status": "submitted",
        "market": market,
        "date": trading_date,
        "submitted_count": 1,
        "artifact_path": str(result_path),
    }


def capture_trend_review_close(
    *,
    data_dir: Path,
    market: str,
    trading_date: str,
    report: Mapping[str, object],
    simulate_snapshot: Mapping[str, object],
    orders: list[Mapping[str, object]],
    benchmark: Mapping[str, object],
) -> Path:
    market = _market(market)
    net_value = _required_decimal(
        simulate_snapshot.get("net_value"), "simulate net value"
    )
    discipline_equity = net_value.quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    _validate_benchmark(benchmark, market=market, trading_date=trading_date)
    account = report.get("account")
    if not isinstance(account, Mapping):
        raise ValueError("trend report account is unavailable")
    strategy_snapshot = report.get("strategy_snapshot")
    if (
        not isinstance(strategy_snapshot, Mapping)
        or not strategy_snapshot.get("strategy_id")
        or not strategy_snapshot.get("strategy_version")
        or not strategy_snapshot.get("process_version")
        or not isinstance(strategy_snapshot.get("parameters"), Mapping)
        or not strategy_snapshot.get("parameter_rows")
    ):
        raise ValueError("trend report strategy snapshot is unavailable")
    actual_equity = _required_decimal(account.get("net_value"), "actual net value")
    payload = {
        "schema_version": "open_trader.trend_review.daily.v1",
        "market": market,
        "date": trading_date,
        "simulate_acc_id": simulate_snapshot.get("acc_id"),
        "discipline_equity_after_fees": str(discipline_equity),
        "actual_equity": str(actual_equity),
        "benchmark": dict(benchmark),
        "strategy_snapshot": dict(strategy_snapshot),
        "report_sha256": _report_hash(report),
        "orders": orders,
        "positions": simulate_snapshot.get("positions"),
    }
    path = (
        data_dir
        / "trend_review"
        / "daily"
        / market
        / f"{trading_date}.json"
    )
    return _write_immutable(path, _canonical_json_bytes(payload))


def _validate_benchmark(
    benchmark: object, *, market: str, trading_date: str
) -> Mapping[str, object]:
    if not isinstance(benchmark, Mapping):
        raise ValueError("trend review benchmark must be an object")
    if benchmark.get("date") != trading_date:
        raise ValueError("benchmark date does not match trend review date")
    if benchmark.get("source_id") != BENCHMARK_SOURCE_IDS[market]:
        raise ValueError(
            f"benchmark source_id must be {BENCHMARK_SOURCE_IDS[market]}"
        )
    if benchmark.get("futu_symbol") != BENCHMARK_FUTU_SYMBOLS[market]:
        raise ValueError("benchmark Futu symbol does not match market")
    if _required_decimal(benchmark.get("close"), "benchmark close") <= 0:
        raise ValueError("benchmark close must be positive")
    return benchmark


def _load_daily_facts(data_dir: Path, market: str) -> list[dict[str, object]]:
    root = data_dir / "trend_review" / "daily" / market
    facts: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "open_trader.trend_review.daily.v1"
            or payload.get("market") != market
            or payload.get("date") != path.stem
        ):
            raise ValueError(f"invalid trend review daily fact: {path}")
        _validate_benchmark(payload.get("benchmark"), market=market, trading_date=path.stem)
        facts.append(payload)
    if not facts:
        raise ValueError(f"no trend review daily facts for {market}")
    return facts


def _completed_trades(facts: list[dict[str, object]]) -> list[dict[str, object]]:
    open_by_symbol: dict[str, dict[str, object]] = {}
    completed: list[dict[str, object]] = []
    for fact in facts:
        raw_orders = fact.get("orders")
        if not isinstance(raw_orders, list):
            raise ValueError("trend review daily orders must be a list")
        for order in raw_orders:
            if not isinstance(order, Mapping):
                raise ValueError("trend review order must be an object")
            status = str(order.get("status") or order.get("order_status") or "").upper()
            if "FILLED" not in status and "DEALT_ALL" not in status:
                continue
            side = str(order.get("side") or order.get("trd_side") or "").upper()
            symbol = str(
                order.get("symbol")
                or str(order.get("code") or order.get("futu_code") or "").split(".")[-1]
            )
            quantity = _required_decimal(
                order.get("dealt_qty", order.get("qty")), "filled quantity"
            )
            if not symbol or quantity <= 0 or side not in {"BUY", "SELL"}:
                raise ValueError("filled trend review order is invalid")
            current = open_by_symbol.get(symbol)
            if side == "BUY":
                if current is None:
                    current = {
                        "symbol": symbol,
                        "entry_date": fact["date"],
                        "quantity": Decimal("0"),
                        "entry_quantity": Decimal("0"),
                        "strategy_snapshot": fact.get("strategy_snapshot"),
                        "entry_report_sha256": fact.get("report_sha256"),
                        "orders": [],
                    }
                    open_by_symbol[symbol] = current
                current["quantity"] = _required_decimal(
                    current["quantity"], "open quantity"
                ) + quantity
                current["entry_quantity"] = _required_decimal(
                    current["entry_quantity"], "entry quantity"
                ) + quantity
                current["orders"].append(dict(order))
                continue
            if current is None or _required_decimal(
                current["quantity"], "open quantity"
            ) < quantity:
                raise ValueError("sell fill exceeds experiment position")
            current["quantity"] = _required_decimal(
                current["quantity"], "open quantity"
            ) - quantity
            current["orders"].append(dict(order))
            if current["quantity"] == 0:
                current["exit_date"] = fact["date"]
                current["quantity"] = format(
                    _required_decimal(current.pop("entry_quantity"), "entry quantity"),
                    "f",
                )
                completed.append(current)
                del open_by_symbol[symbol]
    return completed


def _normalized_curve(
    facts: list[dict[str, object]], field: str
) -> list[dict[str, str]] | None:
    if any(field not in fact for fact in facts):
        return None
    values = [_required_decimal(fact[field], field) for fact in facts]
    if not values or values[0] <= 0 or any(value <= 0 for value in values):
        raise ValueError(f"{field} must contain positive values")
    return [
        {
            "date": str(fact["date"]),
            "equity": str(value / values[0] * Decimal("100")),
        }
        for fact, value in zip(facts, values)
    ]


def _metric(value: object, reason: str | None = None) -> dict[str, object]:
    return {"value": value, "reason": reason if value is None else None}


def _curve_metrics(
    curve: list[dict[str, str]] | None,
    rates: Mapping[date, Decimal],
    *,
    missing_reason: str,
) -> dict[str, dict[str, object]]:
    if curve is None:
        return {
            key: _metric(None, missing_reason)
            for key in (
                "total_return_pct",
                "max_drawdown_pct",
                "calmar_ratio",
                "sharpe_ratio",
            )
        }
    from .tiger_long_term_backtest import portfolio_metrics

    values = portfolio_metrics(curve, rates, Decimal("100"))
    return {
        "total_return_pct": _metric(values["total_return_pct"]),
        "max_drawdown_pct": _metric(values["max_drawdown_pct"]),
        "calmar_ratio": _metric(
            values["calmar_ratio"], "最大回撤为零或样本不足"
        ),
        "sharpe_ratio": _metric(
            values["sharpe_ratio"], "收益波动为零或样本不足"
        ),
    }


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(_canonical_json_bytes(payload))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def build_trend_review_projection(
    data_dir: Path, market: str
) -> dict[str, object]:
    market = _market(market)
    facts = _load_daily_facts(data_dir, market)
    trades = _completed_trades(facts)
    from .tiger_long_term_backtest import load_dgs3mo_csv

    rates_path = data_dir / "rates" / "DGS3MO.csv"
    rates = load_dgs3mo_csv(rates_path)
    completed_batches = len(trades) // 30
    if completed_batches == 0:
        reason = "尚未完成 30 笔纪律模拟交易"
        metrics = {
            key: {
                series: _metric(None, reason)
                for series in ("discipline", "actual", "benchmark")
            }
            for key in (
                "period_net_return",
                "market_excess_return",
                "max_drawdown",
                "calmar",
                "sharpe",
            )
        }
        batch = {
            "batch_number": 1,
            "completed_trade_count": len(trades),
            "start_date": trades[0]["entry_date"] if trades else facts[0]["date"],
            "end_date": None,
        }
        batch_path: Path | None = None
    else:
        batch_number = completed_batches
        selected_trades = trades[(batch_number - 1) * 30 : batch_number * 30]
        start_date = str(selected_trades[0]["entry_date"])
        end_date = str(selected_trades[-1]["exit_date"])
        selected_facts = [
            fact for fact in facts if start_date <= str(fact["date"]) <= end_date
        ]
        discipline_curve = _normalized_curve(
            selected_facts, "discipline_equity_after_fees"
        )
        actual_curve = _normalized_curve(selected_facts, "actual_equity")
        benchmark_facts = [
            {
                "date": fact["date"],
                "benchmark_equity": fact["benchmark"]["close"],
            }
            for fact in selected_facts
        ]
        benchmark_curve = _normalized_curve(
            benchmark_facts, "benchmark_equity"
        )
        discipline_metrics = _curve_metrics(
            discipline_curve, rates, missing_reason="纪律模拟日终净值缺失"
        )
        actual_metrics = _curve_metrics(
            actual_curve, rates, missing_reason="实际执行日终净值缺失"
        )
        benchmark_metrics = _curve_metrics(
            benchmark_curve, rates, missing_reason="市场基准缺失"
        )

        def values(metric_name: str) -> dict[str, dict[str, object]]:
            return {
                "discipline": discipline_metrics[metric_name],
                "actual": actual_metrics[metric_name],
                "benchmark": benchmark_metrics[metric_name],
            }

        def excess(
            item: dict[str, object], benchmark_item: dict[str, object]
        ) -> dict[str, object]:
            if item["value"] is None:
                return item
            if benchmark_item["value"] is None:
                return benchmark_item
            return _metric(
                str(
                    _required_decimal(item["value"], "return")
                    - _required_decimal(benchmark_item["value"], "benchmark return")
                )
            )

        metrics = {
            "period_net_return": values("total_return_pct"),
            "market_excess_return": {
                "discipline": excess(
                    discipline_metrics["total_return_pct"],
                    benchmark_metrics["total_return_pct"],
                ),
                "actual": excess(
                    actual_metrics["total_return_pct"],
                    benchmark_metrics["total_return_pct"],
                ),
                "benchmark": _metric("0"),
            },
            "max_drawdown": values("max_drawdown_pct"),
            "calmar": values("calmar_ratio"),
            "sharpe": values("sharpe_ratio"),
        }
        batch = {
            "batch_number": batch_number,
            "completed_trade_count": 30,
            "start_date": start_date,
            "end_date": end_date,
        }
        batch_path = (
            data_dir
            / "trend_review"
            / "batches"
            / market
            / f"{batch_number:04d}.json"
        )
        batch_payload = {
            "schema_version": "open_trader.trend_review.batch.v1",
            "market": market,
            "batch": batch,
            "strategy_snapshot": selected_facts[-1].get("strategy_snapshot"),
            "curves": {
                "discipline": discipline_curve,
                "actual": actual_curve,
                "benchmark": benchmark_curve,
            },
            "metrics": metrics,
            "completed_trades": selected_trades,
            "benchmark_source_id": BENCHMARK_SOURCE_IDS[market],
            "benchmark_sha256": hashlib.sha256(
                _canonical_json_bytes({
                    "benchmarks": [fact["benchmark"] for fact in selected_facts]
                })
            ).hexdigest(),
            "rates_sha256": hashlib.sha256(rates_path.read_bytes()).hexdigest(),
            "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "process_version": (
                selected_facts[-1].get("strategy_snapshot") or {}
            ).get("process_version"),
        }
        if batch_path.exists():
            existing = json.loads(batch_path.read_text(encoding="utf-8"))
            metrics = existing["metrics"]
            batch = existing["batch"]
        else:
            _write_immutable(batch_path, _canonical_json_bytes(batch_payload))

    latest_snapshot = facts[-1].get("strategy_snapshot")
    projection = {
        "schema_version": "open_trader.trend_review.projection.v1",
        "available": True,
        "market": market,
        "market_label": {"CN": "A 股", "US": "美股", "HK": "港股"}[market],
        "broker": {"CN": "eastmoney", "US": "futu", "HK": "phillips"}[market],
        "strategy_snapshot": latest_snapshot,
        "batch": batch,
        "batch_path": None if batch_path is None else str(batch_path),
        "metrics": metrics,
    }
    _write_json_atomic(
        data_dir / "latest" / f"trend_review_{market.lower()}.json",
        projection,
    )
    return projection


def rebuild_trend_report_from_evidence(
    evidence: Mapping[str, object],
) -> dict[str, object]:
    inputs = evidence.get("rebuild_inputs")
    if not isinstance(inputs, Mapping):
        raise TrendReplayIncompleteError("missing original input: rebuild_inputs")
    required = {
        "as_of_date",
        "execution_date",
        "account",
        "candidates",
        "holding_snapshots",
        "bars_by_symbol",
        "prior_state",
        "watch_events",
        "market",
        "candidate_pool_ids",
        "metadata",
    }
    missing = sorted(required - inputs.keys())
    if missing:
        raise TrendReplayIncompleteError(
            f"missing original input: {missing[0]}"
        )
    snapshot = evidence.get("strategy_snapshot")
    if not isinstance(snapshot, Mapping):
        raise TrendReplayIncompleteError(
            "missing original input: strategy_snapshot"
        )

    from .a_share_trend import (
        AccountPosition,
        AccountSnapshot,
        CandidateInput,
        HoldingSnapshot,
        _report_payload,
        build_report,
    )
    from .kline_technical_facts import DailyKlineBar

    def decimal_or_none(value: object) -> Decimal | None:
        return None if value is None or value == "" else Decimal(str(value))

    account_raw = inputs["account"]
    if not isinstance(account_raw, Mapping):
        raise TrendReplayIncompleteError("missing original input: account")
    positions_raw = account_raw.get("positions")
    if not isinstance(positions_raw, list):
        raise TrendReplayIncompleteError("missing original input: account.positions")
    positions = tuple(
        AccountPosition(
            symbol=str(item["symbol"]),
            name=str(item["name"]),
            asset_class=str(item["asset_class"]),
            quantity=Decimal(str(item["quantity"])),
            avg_cost_price=decimal_or_none(item.get("avg_cost_price")),
            market_value=Decimal(str(item.get("market_value", "0"))),
        )
        for item in positions_raw
        if isinstance(item, Mapping)
    )
    account = AccountSnapshot(
        source_date=str(account_raw["source_date"]),
        fresh=account_raw.get("fresh") is True,
        net_value=Decimal(str(account_raw["net_value"])),
        available_cash=Decimal(str(account_raw["available_cash"])),
        positions=positions,
        exceptions=tuple(str(item) for item in account_raw.get("exceptions", [])),
    )

    decimal_fields = {
        "amount",
        "strength",
        "close",
        "atr",
        "filter_price",
        "market_cap",
    }
    candidates_raw = inputs["candidates"]
    if not isinstance(candidates_raw, list):
        raise TrendReplayIncompleteError("missing original input: candidates")
    candidates = []
    for raw in candidates_raw:
        if not isinstance(raw, Mapping):
            raise TrendReplayIncompleteError("missing original input: candidates")
        values = dict(raw)
        for field in decimal_fields:
            values[field] = decimal_or_none(values.get(field))
        values["pools"] = tuple(values.get("pools") or ())
        candidates.append(CandidateInput(**values))

    holdings_raw = inputs["holding_snapshots"]
    if not isinstance(holdings_raw, Mapping):
        raise TrendReplayIncompleteError(
            "missing original input: holding_snapshots"
        )
    holding_snapshots: dict[str, HoldingSnapshot | None] = {}
    for symbol, raw in holdings_raw.items():
        if raw is None:
            holding_snapshots[str(symbol)] = None
            continue
        if not isinstance(raw, Mapping):
            raise TrendReplayIncompleteError(
                "missing original input: holding_snapshots"
            )
        values = dict(raw)
        for field in ("filter_price", "market_cap", "strength"):
            values[field] = decimal_or_none(values.get(field))
        holding_snapshots[str(symbol)] = HoldingSnapshot(**values)

    bars_raw = inputs["bars_by_symbol"]
    if not isinstance(bars_raw, Mapping):
        raise TrendReplayIncompleteError("missing original input: bars_by_symbol")
    bars_by_symbol = {
        str(symbol): (
            None
            if rows is None
            else tuple(DailyKlineBar(**dict(row)) for row in rows)
        )
        for symbol, rows in bars_raw.items()
        if rows is None or isinstance(rows, list)
    }
    process_version = str(evidence.get("process_version") or "")
    report = build_report(
        as_of_date=str(inputs["as_of_date"]),
        execution_date=str(inputs["execution_date"]),
        account=account,
        candidates=candidates,
        holding_snapshots=holding_snapshots,
        bars_by_symbol=bars_by_symbol,
        prior_state=inputs["prior_state"]
        if isinstance(inputs["prior_state"], Mapping)
        else None,
        watch_events=inputs["watch_events"]
        if isinstance(inputs["watch_events"], list)
        else (),
        api_facts=tuple(str(item) for item in inputs.get("api_facts", [])),
        data_sources=tuple(str(item) for item in inputs.get("data_sources", [])),
        estimated_api_cost=decimal_or_none(inputs.get("estimated_api_cost")),
        actual_api_cost=decimal_or_none(inputs.get("actual_api_cost")),
        generated_at=str(inputs.get("generated_at") or "") or None,
        metadata={
            **dict(inputs["metadata"]),
            "process_version": process_version,
        },
        market=str(inputs["market"]),
        lot_sizes={
            str(key): int(value)
            for key, value in dict(inputs.get("lot_sizes") or {}).items()
        },
        position_weight=Decimal(str(inputs.get("position_weight") or "0.04")),
        position_weight_source=str(
            inputs.get("position_weight_source") or "fallback_4pct"
        ),
        process_version=process_version,
        candidate_pool_ids=tuple(int(item) for item in inputs["candidate_pool_ids"]),
        strategy_snapshot=snapshot,
    )
    return _report_payload(report)
