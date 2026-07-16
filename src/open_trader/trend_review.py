from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


EVIDENCE_SCHEMA_VERSION = "open_trader.trend_review.evidence.v1"
REPLAY_SCHEMA_VERSION = "open_trader.trend_review.replay.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class TrendReplayIncompleteError(ValueError):
    pass


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
    buy_cost_bps: Decimal | None,
    sell_cost_bps: Decimal | None,
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
        "fees": {
            "buy_cost_bps": buy_cost_bps,
            "sell_cost_bps": sell_cost_bps,
        },
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
            "buy_cost_bps": buy_cost_bps,
            "sell_cost_bps": sell_cost_bps,
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
        buy_cost_bps=decimal_or_none(inputs.get("buy_cost_bps")),
        sell_cost_bps=decimal_or_none(inputs.get("sell_cost_bps")),
        strategy_snapshot=snapshot,
    )
    return _report_payload(report)
