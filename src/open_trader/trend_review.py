from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import TradeFill


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
TREND_V1_EFFECTIVE_FROM = {
    "CN": "2026-07-16",
    "US": "2026-07-17",
    "HK": "2026-07-17",
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


def _fact_path(
    data_dir: Path, stream: str, market: str, trading_date: str
) -> Path:
    return (
        data_dir
        / "trend_review"
        / "facts"
        / stream
        / market
        / f"{trading_date}.json"
    )


def freeze_discipline_fact(
    data_dir: Path,
    market: str,
    trading_date: str,
    equity: object,
    orders: Sequence[Mapping[str, object]],
    strategy_snapshot: Mapping[str, object],
) -> Path:
    market = _market(market)
    payload = {
        "schema_version": "open_trader.trend_review.discipline.v1",
        "market": market,
        "date": trading_date,
        "equity_after_fees": str(_required_decimal(equity, "discipline equity")),
        "orders": [dict(order) for order in orders],
        "strategy_snapshot": dict(strategy_snapshot),
    }
    return _write_immutable(
        _fact_path(data_dir, "discipline", market, trading_date),
        _canonical_json_bytes(payload),
    )


def freeze_actual_equity_fact(
    data_dir: Path,
    market: str,
    trading_date: str,
    equity: object,
    opening_positions: Sequence[Mapping[str, object]],
    strategy_snapshot: Mapping[str, object],
) -> Path:
    market = _market(market)
    payload = {
        "schema_version": "open_trader.trend_review.actual_equity.v1",
        "market": market,
        "date": trading_date,
        "equity": str(_required_decimal(equity, "actual equity")),
        "opening_positions": [dict(position) for position in opening_positions],
        "strategy_snapshot": dict(strategy_snapshot),
    }
    return _write_immutable(
        _fact_path(data_dir, "actual_equity", market, trading_date),
        _canonical_json_bytes(payload),
    )


def freeze_benchmark_fact(
    data_dir: Path,
    market: str,
    trading_date: str,
    benchmark: Mapping[str, object],
) -> Path:
    market = _market(market)
    validated = _validate_benchmark(
        benchmark, market=market, trading_date=trading_date
    )
    payload = {
        "schema_version": "open_trader.trend_review.benchmark.v1",
        "market": market,
        "date": trading_date,
        "benchmark": dict(validated),
    }
    return _write_immutable(
        _fact_path(data_dir, "benchmark", market, trading_date),
        _canonical_json_bytes(payload),
    )


def freeze_actual_fill_batch(
    data_dir: Path,
    source_metadata: Mapping[str, object],
    fills: Sequence[TradeFill],
    complete_through: str,
) -> list[Path]:
    raw_market = source_metadata.get("market")
    if raw_market is None and fills:
        raw_market = fills[0].market
    if raw_market is None:
        raw_market = {
            "eastmoney": "CN",
            "tiger": "US",
            "phillips": "HK",
        }.get(str(source_metadata.get("broker") or "").lower())
    market = _market(raw_market)
    paths: list[Path] = []
    identities: list[str] = []
    for fill in fills:
        if _market(fill.market) != market:
            raise ValueError("actual fill market does not match source metadata")
        identity = f"{fill.broker}:{fill.account_alias}:{fill.source_id}"
        digest = hashlib.sha256(f"{fill.broker}:{fill.source_id}".encode()).hexdigest()
        paths.append(
            _write_immutable(
                data_dir
                / "trend_review"
                / "facts"
                / "actual_fills"
                / market
                / f"{digest}.json",
                _canonical_json_bytes(
                    {
                        "schema_version": "open_trader.trend_review.fill.v1",
                        **asdict(fill),
                    }
                ),
            )
        )
        identities.append(identity)
    completeness = {
        "schema_version": "open_trader.trend_review.fill_completeness.v1",
        "market": market,
        "complete_through": complete_through,
        "source_metadata": dict(source_metadata),
        "fill_identities": sorted(identities),
    }
    digest = hashlib.sha256(_canonical_json_bytes(completeness)).hexdigest()
    _write_immutable(
        data_dir
        / "trend_review"
        / "facts"
        / "actual_fill_completeness"
        / market
        / f"{digest}.json",
        _canonical_json_bytes(completeness),
    )
    return paths


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
    price_fx_to_account_currency: Decimal,
    previous_attention_rows: object,
    option_attention_broker_label: str | None,
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
            "price_fx_to_account_currency": price_fx_to_account_currency,
            "option_attention": {
                "previous_rows": previous_attention_rows,
                "broker_label": option_attention_broker_label,
            },
            "candidate_pool_ids": candidate_pool_ids,
            "generated_at": getattr(report, "generated_at"),
            "metadata": metadata,
            "managed_symbols": list(
                getattr(report, "protection_state").get("managed_symbols", [])
            ),
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


def _reconcile_intent(
    intent_path: Path, client: object
) -> tuple[dict[str, object], bool]:
    payload = json.loads(intent_path.read_text(encoding="utf-8"))
    request = payload.get("request") if isinstance(payload, Mapping) else None
    if not isinstance(request, dict):
        raise ValueError("trend review intent request is invalid")
    result_path = intent_path.with_name(
        intent_path.name.replace("-intent", "-result")
    )
    if result_path.exists():
        return request, True
    listed = client.list_orders()
    orders = listed.get("orders") if isinstance(listed, Mapping) else None
    if not isinstance(orders, list):
        raise ValueError("simulate broker orders are unavailable")
    matched = next(
        (
            order for order in orders
            if isinstance(order, Mapping)
            and _order_matches_request(order, request)
        ),
        None,
    )
    if matched is None:
        return request, False
    _write_immutable(
        result_path,
        _canonical_json_bytes(
            {"request": request, "response": matched, "reconciled": True}
        ),
    )
    return request, True


def _order_matches_request(
    order: Mapping[str, object], request: Mapping[str, object]
) -> bool:
    order_side = str(order.get("trd_side", order.get("side", ""))).strip()
    request_side = str(request.get("side") or "").strip()
    try:
        quantity_matches = _required_decimal(
            order.get("qty"), "broker order quantity"
        ) == _required_decimal(request.get("qty"), "request quantity")
    except ValueError:
        return False
    return bool(request.get("remark")) and all(
        (
            str(order.get("remark") or "") == str(request["remark"]),
            str(order.get("code", order.get("futu_code", ""))).strip().upper()
            == str(request.get("futu_code") or "").strip().upper(),
            order_side.rsplit(".", 1)[-1].upper()
            == request_side.rsplit(".", 1)[-1].upper(),
            quantity_matches,
        )
    )


def _open_order_remark(
    market: str, execution_date: str, report_sha: str, action_index: int
) -> str:
    remark = (
        f"trend-review:{market}:{execution_date}:{report_sha[:24]}:{action_index}"
    )
    if len(remark.encode("utf-8")) > 64:
        raise ValueError("trend review order remark exceeds Futu's 64-byte limit")
    return remark


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
        stem = f"{report_sha}-{index}"
        intent_path = root / f"{stem}-intent.json"
        if intent_path.exists():
            request, reconciled = _reconcile_intent(intent_path, client)
            if reconciled:
                continue
        else:
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
                "remark": _open_order_remark(
                    market, execution_date, report_sha, index
                ),
            }
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
        request, reconciled = _reconcile_intent(intent_path, client)
        if reconciled:
            return {
                "status": "unchanged",
                "market": market,
                "date": trading_date,
                "submitted_count": 0,
            }
        response = client.place_order(request)
        result_path = intent_path.with_name(
            intent_path.name.replace("-intent", "-result")
        )
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
    payload = {
        "schema_version": "open_trader.trend_review.daily.v1",
        "market": market,
        "date": trading_date,
        "simulate_acc_id": simulate_snapshot.get("acc_id"),
        "discipline_equity_after_fees": str(discipline_equity),
        "benchmark": dict(benchmark),
        "strategy_snapshot": dict(strategy_snapshot),
        "report_sha256": _report_hash(report),
        "orders": orders,
        "positions": simulate_snapshot.get("positions"),
    }
    if account.get("fresh") is True and account.get("source_date") == trading_date:
        payload["actual_equity"] = str(
            _required_decimal(account.get("net_value"), "actual net value")
        )
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


def _load_dated_fact_stream(
    data_dir: Path,
    stream: str,
    market: str,
    schema_version: str,
) -> list[dict[str, object]]:
    root = data_dir / "trend_review" / "facts" / stream / market
    facts: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != schema_version
            or payload.get("market") != market
            or payload.get("date") != path.stem
        ):
            raise ValueError(f"invalid trend review {stream} fact: {path}")
        facts.append(payload)
    return facts


def _load_actual_fills(
    data_dir: Path, market: str
) -> tuple[list[dict[str, object]], str | None]:
    fills: list[dict[str, object]] = []
    root = data_dir / "trend_review" / "facts" / "actual_fills" / market
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version")
            != "open_trader.trend_review.fill.v1"
            or payload.get("market") != market
        ):
            raise ValueError(f"invalid trend review actual fill fact: {path}")
        fills.append(payload)
    completeness: list[str] = []
    completeness_root = (
        data_dir
        / "trend_review"
        / "facts"
        / "actual_fill_completeness"
        / market
    )
    for path in sorted(completeness_root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version")
            != "open_trader.trend_review.fill_completeness.v1"
            or payload.get("market") != market
        ):
            raise ValueError(f"invalid trend review fill completeness fact: {path}")
        complete_through = str(payload.get("complete_through") or "")
        try:
            date.fromisoformat(complete_through)
        except ValueError:
            raise ValueError(
                f"invalid trend review fill completeness fact: {path}"
            ) from None
        completeness.append(complete_through)
    return fills, max(completeness, default=None)


def _completed_cycles(
    fills: Sequence[Mapping[str, object]],
    opening_positions: Sequence[Mapping[str, object]] = (),
) -> list[dict[str, object]]:
    positions = {
        str(row["symbol"]): {
            "symbol": str(row["symbol"]),
            "entry_date": str(row["opened_at"])[:10],
            "quantity": _required_decimal(row["quantity"], "opening quantity"),
            "fills": [],
        }
        for row in opening_positions
    }
    completed: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for fill in sorted(
        fills, key=lambda row: (str(row["executed_at"]), str(row["source_id"]))
    ):
        identity = (
            str(fill["broker"]),
            str(fill["account_alias"]),
            str(fill["source_id"]),
        )
        if identity in seen:
            continue
        seen.add(identity)
        symbol = str(fill["symbol"])
        quantity = _required_decimal(fill["quantity"], "fill quantity")
        if quantity <= 0:
            raise ValueError("fill quantity must be positive")
        current = positions.get(symbol)
        if fill["side"] == "BUY":
            if current is None:
                current = {
                    "symbol": symbol,
                    "entry_date": str(fill["executed_at"])[:10],
                    "quantity": Decimal("0"),
                    "fills": [],
                }
                positions[symbol] = current
            current["quantity"] = (
                _required_decimal(current["quantity"], "open quantity") + quantity
            )
        elif fill["side"] == "SELL":
            if current is None or _required_decimal(
                current["quantity"], "open quantity"
            ) < quantity:
                raise ValueError("sell fill exceeds actual position")
            current["quantity"] = (
                _required_decimal(current["quantity"], "open quantity") - quantity
            )
        else:
            raise ValueError("actual fill side must be BUY or SELL")
        current["fills"].append(dict(fill))
        if current["quantity"] == 0:
            completed.append(
                {**current, "exit_date": str(fill["executed_at"])[:10]}
            )
            del positions[symbol]
    return completed


def _common_cutoff(
    effective_from: str,
    discipline_dates: set[str],
    actual_dates: set[str],
    benchmark_dates: set[str],
) -> str | None:
    cutoff: str | None = None
    for trading_date in sorted(
        day for day in benchmark_dates if day >= effective_from
    ):
        if trading_date not in discipline_dates or trading_date not in actual_dates:
            break
        cutoff = trading_date
    return cutoff


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


def _load_dgs3mo_csv(path: Path) -> dict[date, Decimal]:
    rates: dict[date, Decimal] = {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            date_field = next(
                (field for field in ("DATE", "observation_date") if field in fields),
                None,
            )
            if date_field is None or "DGS3MO" not in fields:
                raise ValueError(
                    "DGS3MO CSV must contain DATE or observation_date and DGS3MO"
                )
            for row in reader:
                raw_rate = str(row.get("DGS3MO") or "").strip()
                if raw_rate in {"", "."}:
                    continue
                try:
                    observation_date = date.fromisoformat(
                        str(row.get(date_field) or "").strip()
                    )
                    rate = Decimal(raw_rate)
                except (InvalidOperation, ValueError) as exc:
                    raise ValueError(
                        "DGS3MO CSV contains an invalid observation"
                    ) from exc
                if observation_date in rates:
                    raise ValueError("DGS3MO CSV contains a duplicate date")
                if not rate.is_finite() or rate < 0:
                    raise ValueError("DGS3MO rate must be finite and non-negative")
                rates[observation_date] = rate
    except OSError as exc:
        raise ValueError(f"cannot read DGS3MO CSV: {path}") from exc
    if not rates:
        raise ValueError("DGS3MO series has no valid observations")
    return dict(sorted(rates.items()))


def _rate_on_or_before(
    rates: Mapping[date, Decimal], target: date
) -> Decimal:
    ordered_dates = sorted(rates)
    index = bisect_right(ordered_dates, target) - 1
    if index < 0:
        raise ValueError(
            f"DGS3MO has no observation on or before {target.isoformat()}"
        )
    return rates[ordered_dates[index]]


def _annualized_sharpe(excess_returns: Sequence[Decimal]) -> Decimal | None:
    if len(excess_returns) < 2:
        return None
    mean = sum(excess_returns, Decimal("0")) / Decimal(len(excess_returns))
    variance = sum(
        ((value - mean) ** 2 for value in excess_returns), Decimal("0")
    ) / Decimal(len(excess_returns))
    if variance == 0:
        return None
    return mean / variance.sqrt() * Decimal(252).sqrt()


def _portfolio_metrics(
    curve: Sequence[Mapping[str, str]],
    rates: Mapping[date, Decimal],
    initial_cash: Decimal,
) -> dict[str, object]:
    if not curve:
        return {
            "total_return_pct": "0",
            "max_drawdown_pct": "0",
            "sharpe_ratio": None,
            "calmar_ratio": None,
        }
    equities = [Decimal(row["equity"]) for row in curve]
    dates = [date.fromisoformat(row["date"]) for row in curve]
    total_return = (equities[-1] / initial_cash - Decimal("1")) * Decimal("100")
    elapsed_days = max(1, (dates[-1] - dates[0]).days)
    annualized = (
        (equities[-1] / initial_cash) ** (Decimal("365") / Decimal(elapsed_days))
        - Decimal("1")
    ) * Decimal("100") if equities[-1] > 0 else Decimal("-100")
    peak = equities[0]
    max_drawdown = Decimal("0")
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(
                max_drawdown, (peak - equity) / peak * Decimal("100")
            )
    excess_returns: list[Decimal] = []
    for previous, current, previous_date, current_date in zip(
        equities, equities[1:], dates, dates[1:]
    ):
        if previous <= 0:
            continue
        risk_free = (
            Decimal("1") + _rate_on_or_before(rates, previous_date) / Decimal("100")
        ) ** (
            Decimal((current_date - previous_date).days) / Decimal("365")
        ) - Decimal("1")
        excess_returns.append(current / previous - Decimal("1") - risk_free)
    sharpe = _annualized_sharpe(excess_returns)
    calmar = annualized / max_drawdown if max_drawdown else None
    return {
        "total_return_pct": format(total_return, "f"),
        "max_drawdown_pct": format(max_drawdown, "f"),
        "sharpe_ratio": None if sharpe is None else format(sharpe, "f"),
        "calmar_ratio": None if calmar is None else format(calmar, "f"),
    }


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
    values = _portfolio_metrics(curve, rates, Decimal("100"))
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
    effective_from = TREND_V1_EFFECTIVE_FROM[market]
    daily_root = data_dir / "trend_review" / "daily" / market
    legacy = _load_daily_facts(data_dir, market) if any(daily_root.glob("*.json")) else []
    discipline_by_date = {str(fact["date"]): fact for fact in legacy}
    actual_by_date = {
        str(fact["date"]): {
            "date": fact["date"],
            "actual_equity": fact["actual_equity"],
            "strategy_snapshot": fact.get("strategy_snapshot"),
            "opening_positions": [],
            "new_fact": False,
        }
        for fact in legacy
        if "actual_equity" in fact
    }
    benchmark_by_date = {
        str(fact["date"]): dict(fact["benchmark"])
        for fact in legacy
    }
    for fact in _load_dated_fact_stream(
        data_dir,
        "discipline",
        market,
        "open_trader.trend_review.discipline.v1",
    ):
        discipline_by_date[str(fact["date"])] = {
            "date": fact["date"],
            "discipline_equity_after_fees": fact["equity_after_fees"],
            "orders": fact["orders"],
            "strategy_snapshot": fact.get("strategy_snapshot"),
        }
    for fact in _load_dated_fact_stream(
        data_dir,
        "actual_equity",
        market,
        "open_trader.trend_review.actual_equity.v1",
    ):
        actual_by_date[str(fact["date"])] = {
            "date": fact["date"],
            "actual_equity": fact["equity"],
            "strategy_snapshot": fact.get("strategy_snapshot"),
            "opening_positions": fact.get("opening_positions", []),
            "new_fact": True,
        }
    for fact in _load_dated_fact_stream(
        data_dir,
        "benchmark",
        market,
        "open_trader.trend_review.benchmark.v1",
    ):
        benchmark = _validate_benchmark(
            fact.get("benchmark"), market=market, trading_date=str(fact["date"])
        )
        benchmark_by_date[str(fact["date"])] = dict(benchmark)
    if not discipline_by_date and not actual_by_date and not benchmark_by_date:
        raise ValueError(f"no trend review facts for {market}")

    def is_v1(fact: Mapping[str, object]) -> bool:
        snapshot = fact.get("strategy_snapshot")
        return (
            isinstance(snapshot, Mapping)
            and snapshot.get("strategy_version") == "v1"
        )

    discipline_facts = [
        discipline_by_date[trading_date]
        for trading_date in sorted(discipline_by_date)
        if is_v1(discipline_by_date[trading_date])
    ]
    actual_facts = [
        actual_by_date[trading_date]
        for trading_date in sorted(actual_by_date)
        if is_v1(actual_by_date[trading_date])
    ]
    snapshots = [
        fact["strategy_snapshot"]
        for fact in [*discipline_facts, *actual_facts]
        if isinstance(fact.get("strategy_snapshot"), Mapping)
    ]
    snapshot = dict(snapshots[-1]) if snapshots else {}
    snapshot["effective_from"] = effective_from

    fills, actual_complete_through = _load_actual_fills(data_dir, market)
    equity_cutoff = _common_cutoff(
        effective_from,
        {str(fact["date"]) for fact in discipline_facts},
        {str(fact["date"]) for fact in actual_facts},
        set(benchmark_by_date),
    )
    common_cutoff = (
        max(
            (
                trading_date
                for trading_date in benchmark_by_date
                if effective_from <= trading_date <= equity_cutoff
                and trading_date <= actual_complete_through
            ),
            default=None,
        )
        if equity_cutoff is not None and actual_complete_through is not None
        else None
    )
    interval_discipline = [
        fact
        for fact in discipline_facts
        if common_cutoff is not None
        and effective_from <= str(fact["date"]) <= common_cutoff
    ]
    discipline_cycles = _completed_trades(interval_discipline)
    interval_actual = {
        str(fact["date"]): fact
        for fact in actual_facts
        if common_cutoff is not None
        and effective_from <= str(fact["date"]) <= common_cutoff
    }
    interval_fills = [
        fill
        for fill in fills
        if common_cutoff is not None
        and effective_from <= str(fill["executed_at"])[:10] <= common_cutoff
        and str(fill["executed_at"])[:10] in interval_actual
    ]
    opening_fact = next(
        (
            fact
            for fact in interval_actual.values()
            if fact.get("new_fact") is True
        ),
        None,
    )
    opening_positions = (
        [
            {**position, "opened_at": position.get("opened_at", opening_fact["date"])}
            for position in opening_fact.get("opening_positions", [])
            if isinstance(position, Mapping)
        ]
        if opening_fact is not None
        else []
    )
    if not isinstance(opening_positions, list):
        raise ValueError("actual opening positions must be a list")
    actual_cycles = _completed_cycles(interval_fills, opening_positions)

    rates_path = data_dir / "rates" / "DGS3MO.csv"
    rates = _load_dgs3mo_csv(rates_path)
    curve_dates = [
        trading_date
        for trading_date in sorted(benchmark_by_date)
        if common_cutoff is not None
        and effective_from <= trading_date <= common_cutoff
    ]
    discipline_curve = _normalized_curve(
        [discipline_by_date[trading_date] for trading_date in curve_dates],
        "discipline_equity_after_fees",
    ) if curve_dates else None
    actual_curve = _normalized_curve(
        [actual_by_date[trading_date] for trading_date in curve_dates],
        "actual_equity",
    ) if curve_dates else None
    benchmark_curve = _normalized_curve(
        [
            {
                "date": trading_date,
                "benchmark_equity": benchmark_by_date[trading_date]["close"],
            }
            for trading_date in curve_dates
        ],
        "benchmark_equity",
    ) if curve_dates else None
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
            "benchmark": _metric("0") if benchmark_curve else _metric(None, "市场基准缺失"),
        },
        "max_drawdown": values("max_drawdown_pct"),
        "calmar": values("calmar_ratio"),
        "sharpe": values("sharpe_ratio"),
    }
    sample_counts = {
        "discipline": len(discipline_cycles),
        "actual": len(actual_cycles),
        "required": 30,
    }
    for series in ("discipline", "actual"):
        count = sample_counts[series]
        if count < 30:
            for metric in metrics.values():
                metric[series] = _metric(None, f"{count} / 30，数据不足")

    projection = {
        "schema_version": "open_trader.trend_review.projection.v2",
        "available": True,
        "market": market,
        "market_label": {"CN": "A 股", "US": "美股", "HK": "港股"}[market],
        "broker": {"CN": "eastmoney", "US": "tiger", "HK": "phillips"}[market],
        "strategy_snapshot": snapshot,
        "sample_counts": sample_counts,
        "common_cutoff": common_cutoff,
        "interval": {"start": effective_from, "end": common_cutoff},
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
        "price_fx_to_account_currency",
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
        _finalize_market_report,
        _report_payload,
        build_report,
    )
    from .kline_technical_facts import DailyKlineBar

    def decimal_or_none(value: object) -> Decimal | None:
        return None if value is None or value == "" else Decimal(str(value))

    account_raw = inputs["account"]
    if not isinstance(account_raw, Mapping):
        raise TrendReplayIncompleteError("missing original input: account")
    if "position_count" not in account_raw:
        raise TrendReplayIncompleteError(
            "missing original input: account.position_count"
        )
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
    position_count_raw = account_raw["position_count"]
    if (
        position_count_raw is not None
        and (
            isinstance(position_count_raw, bool)
            or not isinstance(position_count_raw, int)
            or position_count_raw < 0
        )
    ):
        raise TrendReplayIncompleteError(
            "invalid original input: account.position_count"
        )
    account = AccountSnapshot(
        source_date=str(account_raw["source_date"]),
        fresh=account_raw.get("fresh") is True,
        net_value=Decimal(str(account_raw["net_value"])),
        available_cash=Decimal(str(account_raw["available_cash"])),
        positions=positions,
        exceptions=tuple(str(item) for item in account_raw.get("exceptions", [])),
        position_count=position_count_raw,
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
    price_fx = decimal_or_none(inputs["price_fx_to_account_currency"])
    if price_fx is None or not price_fx.is_finite() or price_fx <= 0:
        raise TrendReplayIncompleteError(
            "invalid original input: price_fx_to_account_currency"
        )
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
        price_fx_to_account_currency=price_fx,
        process_version=process_version,
        candidate_pool_ids=tuple(int(item) for item in inputs["candidate_pool_ids"]),
        strategy_snapshot=snapshot,
    )
    market = str(inputs["market"]).upper()
    if market in {"US", "HK"}:
        managed_symbols = inputs.get("managed_symbols")
        if not isinstance(managed_symbols, list) or not all(
            isinstance(symbol, str) for symbol in managed_symbols
        ):
            raise TrendReplayIncompleteError(
                "missing original input: managed_symbols"
            )
        report = _finalize_market_report(
            report, managed_symbols=managed_symbols
        )
    payload = _report_payload(report)
    if market in {"US", "HK"}:
        attention_input = inputs.get("option_attention")
        if not isinstance(attention_input, Mapping):
            raise TrendReplayIncompleteError(
                "missing original input: option_attention"
            )
        previous_rows = attention_input.get("previous_rows")
        broker_label = attention_input.get("broker_label")
        if not isinstance(previous_rows, list) or not all(
            isinstance(row, Mapping) for row in previous_rows
        ):
            raise TrendReplayIncompleteError(
                "missing original input: option_attention.previous_rows"
            )
        if not isinstance(broker_label, str) or not broker_label:
            raise TrendReplayIncompleteError(
                "missing original input: option_attention.broker_label"
            )
        from .market_trend import (
            _attention_actions,
            _attention_rows,
            build_option_attention,
        )

        current_rows = _attention_rows(payload.get("signal_snapshots"))
        if current_rows is None:
            raise TrendReplayIncompleteError(
                "missing original input: signal_snapshots"
            )
        payload["option_attention"] = build_option_attention(
            current_rows,
            previous_rows,
            _attention_actions(payload),
            market,
            broker_label,
        )
    return payload
