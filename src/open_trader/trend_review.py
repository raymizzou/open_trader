from __future__ import annotations

import copy
import csv
import fcntl
import hashlib
import json
import os
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo


EVIDENCE_SCHEMA_VERSION = "open_trader.trend_review.evidence.v1"
REPLAY_SCHEMA_VERSION = "open_trader.trend_review.replay.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_TIMEZONES = {
    "CN": SHANGHAI,
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}
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
REJECTED_ORDER_STATUSES = {
    "FAILED",
    "SUBMIT_FAILED",
    "TIMEOUT",
    "DISABLED",
    "DELETED",
    "REJECTED",
}
ACTIVE_ORDER_STATUSES = {
    "SUBMITTING",
    "SUBMITTED",
    "WAITING_SUBMIT",
    "FILLED_PART",
}
TERMINAL_ORDER_STATUSES = REJECTED_ORDER_STATUSES | {
    "CANCELLED",
    "CANCELLED_ALL",
    "CANCELLED_PART",
    "FILLED",
    "FILLED_ALL",
}
RESOLUTION_STATUSES = {
    "confirm-submitted": "resolved_submitted",
    "authorize-retry": "retry_authorized",
    "abandon": "abandoned",
}
PROTECTION_STATE_ROOTS = {
    "CN": "trend_a_share",
    "HK": "trend_hk_phillips",
    "US": "trend_us_tiger",
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
    price_fx_to_account_currency: Decimal,
    previous_attention_rows: object,
    option_attention_broker_label: str | None,
    kelly_rounds: object = (),
    kelly_data_reason: str = "",
) -> dict[str, str]:
    metadata = getattr(report, "metadata")
    strategy_snapshot = getattr(report, "strategy_snapshot")
    risk_summary = getattr(report, "risk_summary")
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
            "normal_cost_rate": risk_summary.get("normal_cost_rate"),
            "drawdown_summary": getattr(report, "drawdown_summary", None),
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
            "kelly_rounds": kelly_rounds,
            "kelly_data_reason": kelly_data_reason,
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


def _result_path(intent_path: Path) -> Path:
    return intent_path.with_name(intent_path.name.replace("-intent", "-result"))


def _intent_path(result_path: Path) -> Path:
    return result_path.with_name(result_path.name.replace("-result", "-intent"))


def _ledger_fact_paths(root: Path) -> list[Path]:
    intents = list(root.glob("*-intent.json"))
    results = [
        path for path in root.glob("*-result.json") if not _intent_path(path).exists()
    ]
    return sorted([*intents, *results])


def _result_request(path: Path, payload: object) -> dict[str, object]:
    request = payload.get("request") if isinstance(payload, Mapping) else None
    response = payload.get("response") if isinstance(payload, Mapping) else None
    try:
        quantity = _required_decimal(
            request.get("qty") if isinstance(request, Mapping) else None,
            "result quantity",
        )
    except ValueError:
        quantity = Decimal("0")
    if (
        not isinstance(request, dict)
        or not isinstance(response, Mapping)
        or not str(request.get("futu_code") or "").strip()
        or not str(request.get("side") or "").strip()
        or not str(request.get("remark") or "").strip()
        or quantity <= 0
    ):
        raise ValueError(f"invalid trend review result: {path}")
    return request


def _ledger_fact_attempt(
    path: Path, payload: Mapping[str, object], request: Mapping[str, object]
) -> int:
    candidates: list[int] = []
    raw_attempt = payload.get("attempt")
    if raw_attempt is not None:
        if isinstance(raw_attempt, bool):
            raise ValueError(f"invalid trend review attempt: {path}")
        try:
            candidates.append(int(raw_attempt))
        except (TypeError, ValueError):
            raise ValueError(f"invalid trend review attempt: {path}") from None
    marker = "-attempt-"
    if marker in path.name:
        try:
            candidates.append(int(path.name.rsplit(marker, 1)[1].split("-", 1)[0]))
        except ValueError:
            raise ValueError(f"invalid trend review attempt: {path}") from None
    remark = str(request.get("remark") or "")
    if remark.startswith("trend:"):
        try:
            candidates.append(int(remark.rsplit(":", 1)[1]))
        except ValueError:
            raise ValueError(f"invalid trend review attempt: {path}") from None
    attempts = set(candidates or [1])
    if len(attempts) != 1 or next(iter(attempts)) <= 0:
        raise ValueError(f"invalid trend review attempt: {path}")
    return attempts.pop()


def trend_action_key(
    market: str, execution_date: str, futu_code: str, side: str
) -> str:
    identity = ":".join(
        (
            _market(market),
            date.fromisoformat(execution_date).isoformat(),
            futu_code.strip().upper(),
            side.strip().lower(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def trend_attempt_remark(
    market: str, execution_date: str, action_key: str, attempt: int
) -> str:
    if attempt <= 0:
        raise ValueError("attempt must be positive")
    remark = f"trend:{_market(market)}:{execution_date}:{action_key[:20]}:{attempt}"
    if len(remark.encode("utf-8")) > 64:
        raise ValueError("trend order remark exceeds Futu's 64-byte limit")
    return remark


def _validate_execution_batch(
    payload: object, *, market: str, execution_date: str
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("trend execution batch must be a JSON object")
    try:
        locked_at = datetime.fromisoformat(str(payload["locked_at"]))
    except (KeyError, ValueError):
        raise ValueError("trend execution batch has an invalid locked_at") from None
    report_sha = payload.get("report_sha256")
    if (
        payload.get("schema_version") != "open_trader.trend_review.batch.v1"
        or payload.get("market") != market
        or payload.get("execution_date") != execution_date
        or not isinstance(payload.get("report_path"), str)
        or not payload["report_path"]
        or not isinstance(report_sha, str)
        or len(report_sha) != 64
        or any(character not in "0123456789abcdef" for character in report_sha)
        or locked_at.tzinfo is None
        or locked_at.utcoffset() is None
    ):
        raise ValueError("trend execution batch is invalid")
    return payload


def lock_trend_execution_batch(
    data_dir: Path,
    *,
    market: str,
    execution_date: str,
    report_path: Path,
    report: Mapping[str, object],
    locked_at: str,
) -> dict[str, object]:
    market = _market(market)
    execution_date = date.fromisoformat(execution_date).isoformat()
    path = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "batches"
        / f"{execution_date}.json"
    )
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid trend execution batch: {path}") from exc
        return _validate_execution_batch(
            existing, market=market, execution_date=execution_date
        )
    legacy_facts: list[tuple[datetime, str]] = []
    ledger_root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "open"
        / execution_date
    )
    for fact_path in _ledger_fact_paths(ledger_root):
        try:
            fact = json.loads(fact_path.read_text(encoding="utf-8"))
            if not isinstance(fact, dict):
                raise TypeError
            timestamp_field = (
                "submitted_at"
                if fact_path.name.endswith("-result.json")
                else "created_at"
            )
            if timestamp_field == "submitted_at":
                _result_request(fact_path, fact)
            created_at = datetime.fromisoformat(str(fact[timestamp_field]))
            report_sha = fact["report_sha256"]
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"trend execution batch is blocked by invalid ledger fact: {fact_path}"
            ) from exc
        if (
            created_at.tzinfo is None
            or created_at.utcoffset() is None
            or not isinstance(report_sha, str)
            or len(report_sha) != 64
            or any(character not in "0123456789abcdef" for character in report_sha)
        ):
            raise ValueError(
                f"trend execution batch is blocked by invalid ledger fact: {fact_path}"
            )
        legacy_facts.append((created_at, report_sha))
    selected_path = report_path
    selected_sha = _report_hash(report)
    if legacy_facts:
        selected_sha = min(legacy_facts, key=lambda item: item[0])[1]
        matches: list[Path] = []
        for candidate in sorted(report_path.parent.glob("*.json")):
            try:
                candidate_report = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(candidate_report, Mapping)
                and _report_hash(candidate_report) == selected_sha
            ):
                matches.append(candidate)
        if not matches:
            raise ValueError(
                "trend execution batch is blocked: no matching report artifact"
            )
        selected_path = matches[0]
    payload = _validate_execution_batch(
        {
            "schema_version": "open_trader.trend_review.batch.v1",
            "market": market,
            "execution_date": execution_date,
            "report_path": str(selected_path),
            "report_sha256": selected_sha,
            "locked_at": locked_at,
        },
        market=market,
        execution_date=execution_date,
    )
    try:
        _write_immutable(path, _canonical_json_bytes(payload))
    except FileExistsError:
        try:
            concurrent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid trend execution batch: {path}") from exc
        return _validate_execution_batch(
            concurrent, market=market, execution_date=execution_date
        )
    return payload


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


def _ensure_discipline_account(
    data_dir: Path,
    market: str,
    snapshot: Mapping[str, object],
) -> None:
    root = data_dir / "trend_review" / "ledgers" / market
    started = root / "started.json"
    account_id = int(snapshot.get("acc_id") or 0)
    if account_id <= 0:
        raise TrendReviewAccountStateError("simulate account ID is unavailable")
    if not started.exists():
        _write_immutable(
            started,
            _canonical_json_bytes(
                {"market": market, "acc_id": account_id, "started_at": "first-open"}
            ),
        )
        return
    existing = json.loads(started.read_text(encoding="utf-8"))
    if existing.get("acc_id") != account_id:
        raise TrendReviewAccountStateError("configured simulate account changed")


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


def _order_has_action_identity(
    order: Mapping[str, object], request: Mapping[str, object]
) -> bool:
    return all(
        (
            bool(request.get("remark")),
            str(order.get("remark") or "") == str(request.get("remark") or ""),
            str(order.get("code", order.get("futu_code", ""))).strip().upper()
            == str(request.get("futu_code") or "").strip().upper(),
            str(order.get("trd_side", order.get("side", "")))
            .strip()
            .rsplit(".", 1)[-1]
            .upper()
            == str(request.get("side") or "").strip().rsplit(".", 1)[-1].upper(),
        )
    )


def _action_facts(
    root: Path, *, futu_code: str, side: str
) -> list[tuple[Path, dict[str, object], dict[str, object], int]]:
    facts: list[tuple[Path, dict[str, object], dict[str, object], int]] = []
    for path in _ledger_fact_paths(root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            kind = "result" if path.name.endswith("-result.json") else "intent"
            raise ValueError(f"invalid trend review {kind}: {path}") from exc
        if not isinstance(payload, dict):
            kind = "result" if path.name.endswith("-result.json") else "intent"
            raise ValueError(f"invalid trend review {kind}: {path}")
        if path.name.endswith("-result.json"):
            request = _result_request(path, payload)
        else:
            request = payload.get("request")
        if not isinstance(request, dict):
            raise ValueError(f"invalid trend review intent: {path}")
        attempt = _ledger_fact_attempt(path, payload, request)
        if (
            str(request.get("futu_code") or "").strip().upper()
            == futu_code.strip().upper()
            and str(request.get("side") or "").strip().rsplit(".", 1)[-1].lower()
            == side.strip().rsplit(".", 1)[-1].lower()
        ):
            facts.append((path, payload, request, attempt))
    return sorted(
        facts,
        key=lambda item: (
            item[3],
            str(item[1].get("created_at") or item[1].get("submitted_at") or ""),
            item[0].name,
        ),
    )


def _listed_orders(
    client: object, *, start: str, end: str
) -> list[Mapping[str, object]]:
    listed = client.list_orders(start=start, end=end)
    orders = listed.get("orders") if isinstance(listed, Mapping) else None
    if not isinstance(orders, list) or not all(
        isinstance(order, Mapping) for order in orders
    ):
        raise ValueError("simulate broker orders are unavailable")
    return orders


def _broker_attempt_fact(
    orders: Sequence[Mapping[str, object]], request: Mapping[str, object]
) -> tuple[str, Mapping[str, object] | None]:
    same_remark = [
        order
        for order in orders
        if str(order.get("remark") or "") == str(request.get("remark") or "")
    ]
    exact = [
        order for order in same_remark if _order_matches_request(order, request)
    ]
    if not same_remark:
        return "absent", None
    if len(same_remark) == len(exact) == 1:
        return "exact", exact[0]
    return "conflict", None


def _write_reconciled_result(
    intent_path: Path,
    *,
    market: str,
    execution_date: str,
    request: Mapping[str, object],
    response: Mapping[str, object],
    report_sha: str,
    action_index: int,
    reconciled_at: str,
) -> Path:
    return _write_immutable(
        _result_path(intent_path),
        _canonical_json_bytes(
            {
                "market": market,
                "date": execution_date,
                "report_sha256": report_sha,
                "action_index": action_index,
                "request": request,
                "response": response,
                "reconciled": True,
                "submitted_at": reconciled_at,
            }
        ),
    )


def _write_action_event(
    *,
    data_dir: Path,
    market: str,
    execution_date: str,
    action_key: str,
    payload: Mapping[str, object],
    recorded_at: str,
) -> Path:
    event = {**payload, "recorded_at": recorded_at}
    body = _canonical_json_bytes(event)
    filename = (
        f"{recorded_at.replace(':', '-')}-{hashlib.sha256(body).hexdigest()[:12]}.json"
    )
    return _write_immutable(
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / execution_date
        / action_key
        / filename,
        body,
    )


def _action_events(root: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid trend action event: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"invalid trend action event: {path}")
        events.append(payload)
    return events


def _write_uncertain_action_event_once(
    *,
    data_dir: Path,
    market: str,
    execution_date: str,
    action_key: str,
    action_root: Path,
    evidence: Mapping[str, object],
    attempt: int,
    reason: str,
    recorded_at: str,
    target_qty: str | None = None,
) -> Path | None:
    if any(
        event.get("status") == "uncertain"
        and int(event.get("attempt") or 1) == attempt
        and event.get("reason") == reason
        for event in _action_events(action_root)
    ):
        return None
    payload = {
        **evidence,
        "status": "uncertain",
        "attempt": attempt,
        "reason": reason,
    }
    if target_qty is not None:
        payload["target_qty"] = target_qty
    return _write_action_event(
        data_dir=data_dir,
        market=market,
        execution_date=execution_date,
        action_key=action_key,
        payload=payload,
        recorded_at=recorded_at,
    )


def _action_resolutions(
    root: Path,
    *,
    market: str,
    execution_date: str,
    action_key: str,
    symbol: str,
    futu_code: str,
    side: str,
) -> list[dict[str, object]]:
    resolutions: list[dict[str, object]] = []
    for path in sorted((root / "resolutions").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError
            resolved = datetime.fromisoformat(str(payload["resolved_at"]))
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(f"invalid trend action resolution: {path}") from exc
        resolution = payload.get("resolution")
        order_id = payload.get("futu_order_id")
        if (
            payload.get("schema_version")
            != "open_trader.trend_review.resolution.v1"
            or payload.get("market") != market
            or payload.get("execution_date") != execution_date
            or payload.get("action_key") != action_key
            or payload.get("symbol") != symbol
            or payload.get("futu_code") != futu_code
            or payload.get("side") != side
            or resolution not in RESOLUTION_STATUSES
            or payload.get("status") != RESOLUTION_STATUSES.get(resolution)
            or not str(payload.get("actor") or "").strip()
            or not str(payload.get("reason") or "").strip()
            or resolved.tzinfo is None
            or resolved.utcoffset() is None
            or resolution == "confirm-submitted"
            and not str(order_id or "").strip()
            or resolution != "confirm-submitted"
            and order_id is not None
        ):
            raise ValueError(f"invalid trend action resolution: {path}")
        resolutions.append(payload)
    return resolutions


def resolve_trend_action(
    data_dir: Path,
    *,
    market: str,
    execution_date: str,
    symbol: str,
    side: str,
    resolution: Literal["confirm-submitted", "authorize-retry", "abandon"],
    actor: str,
    reason: str,
    resolved_at: str,
    futu_order_id: str | None = None,
) -> Path:
    market = _market(market)
    execution_date = date.fromisoformat(execution_date).isoformat()
    symbol = symbol.strip()
    side = side.strip().lower()
    if not symbol or side not in {"buy", "sell"}:
        raise ValueError("trend action identity is invalid")
    if resolution not in RESOLUTION_STATUSES:
        raise ValueError("trend action resolution is invalid")
    actor = actor.strip()
    reason = reason.strip()
    if not actor or not reason:
        raise ValueError("resolution actor and reason are required")
    try:
        resolved = datetime.fromisoformat(resolved_at)
    except ValueError:
        raise ValueError("resolution timestamp is invalid") from None
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("resolution timestamp is invalid")
    order_id = str(futu_order_id or "").strip()
    if resolution == "confirm-submitted" and not order_id:
        raise ValueError("confirm-submitted requires a Futu order ID")
    if resolution != "confirm-submitted" and order_id:
        raise ValueError("only confirm-submitted accepts a Futu order ID")

    from .futu_symbols import to_futu_symbol

    futu_code = to_futu_symbol(market, symbol)
    action_key = trend_action_key(market, execution_date, futu_code, side)
    action_root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / execution_date
        / action_key
    )
    payload = {
        "schema_version": "open_trader.trend_review.resolution.v1",
        "market": market,
        "execution_date": execution_date,
        "action_key": action_key,
        "symbol": symbol,
        "futu_code": futu_code,
        "side": side,
        "resolution": resolution,
        "status": RESOLUTION_STATUSES[resolution],
        "actor": actor,
        "reason": reason,
        "futu_order_id": order_id if resolution == "confirm-submitted" else None,
        "resolved_at": resolved_at,
    }
    body = _canonical_json_bytes(payload)
    path = (
        action_root
        / "resolutions"
        / (
            f"{resolved_at.replace(':', '-')}-"
            f"{hashlib.sha256(body).hexdigest()[:12]}.json"
        )
    )
    action_root.mkdir(parents=True, exist_ok=True)
    lock = os.open(action_root / ".resolution.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and path.read_bytes() == body:
            return path
        resolutions = _action_resolutions(
            action_root,
            market=market,
            execution_date=execution_date,
            action_key=action_key,
            symbol=symbol,
            futu_code=futu_code,
            side=side,
        )
        facts = _action_facts(
            data_dir
            / "trend_review"
            / "ledgers"
            / market
            / "open"
            / execution_date,
            futu_code=futu_code,
            side=side,
        )
        attempt = max((item[3] for item in facts), default=0)
        authorized = sum(
            item.get("resolution") == "authorize-retry" for item in resolutions
        )
        uncertain = any(
            event.get("status") == "uncertain"
            and int(event.get("attempt") or 1) == attempt
            for event in _action_events(action_root)
        )
        if (
            not attempt
            or not uncertain
            or authorized >= attempt
            or any(
                item.get("resolution") in {"confirm-submitted", "abandon"}
                for item in resolutions
            )
        ):
            raise ValueError("trend action is not uncertain or is already resolved")
        return _write_immutable(path, body)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def _floor_to_lot(value: Decimal, lot_size: int) -> int:
    if lot_size <= 0 or not value.is_finite() or value <= 0:
        return 0
    return int(value // Decimal(lot_size)) * lot_size


def _remaining_buy_quantity(
    action: Mapping[str, object],
    report: Mapping[str, object],
    snapshot: Mapping[str, object],
    broker_orders: Sequence[Mapping[str, object]],
    current_price: Decimal,
) -> int:
    try:
        lot_size = int(action.get("lot_size") or 0)
    except (TypeError, ValueError):
        raise ValueError("trend review buy action is invalid") from None
    frozen_quantity = _required_decimal(
        action.get("estimated_shares"), "estimated shares"
    )
    target_amount = _required_decimal(action.get("target_amount"), "target amount")
    current_price = _required_decimal(current_price, "current price")
    metadata = report.get("metadata")
    fx = _required_decimal(
        metadata.get("price_fx_to_account_currency", "1")
        if isinstance(metadata, Mapping)
        else "1",
        "price FX",
    )
    cash = _required_decimal(
        snapshot.get("available_cash", snapshot.get("cash")),
        "simulate available cash",
    )
    if (
        lot_size <= 0
        or frozen_quantity <= 0
        or frozen_quantity != frozen_quantity.to_integral_value()
        or frozen_quantity % lot_size
        or target_amount <= 0
        or current_price <= 0
        or fx <= 0
    ):
        raise ValueError("trend review buy completion inputs are invalid")
    if cash <= 0:
        return 0

    fills: dict[str, tuple[Decimal, Decimal]] = {}
    for order in broker_orders:
        dealt = _required_decimal(
            order.get("dealt_qty", "0"), "broker dealt quantity"
        )
        if dealt < 0:
            raise ValueError("broker dealt quantity must be non-negative")
        if dealt == 0:
            continue
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            raise ValueError("confirmed broker fill requires an order ID")
        price = _required_decimal(
            order.get("dealt_avg_price"), "broker average fill price"
        )
        if price <= 0:
            raise ValueError("broker average fill price must be positive")
        fact = (dealt, price)
        if order_id in fills and fills[order_id] != fact:
            raise ValueError("broker order ID has conflicting fill facts")
        fills[order_id] = fact

    confirmed_quantity = sum(
        (quantity for quantity, _ in fills.values()), Decimal("0")
    )
    confirmed_notional = sum(
        (quantity * price * fx for quantity, price in fills.values()),
        Decimal("0"),
    )
    remaining_quantity = frozen_quantity - confirmed_quantity
    remaining_amount = target_amount - confirmed_notional
    caps = [
        _floor_to_lot(remaining_quantity, lot_size),
        _floor_to_lot(remaining_amount / (current_price * fx), lot_size),
        _floor_to_lot(cash / (current_price * fx), lot_size),
    ]
    strategy_snapshot = report.get("strategy_snapshot")
    version = (
        str(strategy_snapshot.get("strategy_version") or "")
        if isinstance(strategy_snapshot, Mapping)
        else ""
    )
    if version in {"v2", "v3", "v4"}:
        risk_summary = report.get("risk_summary")
        if not isinstance(risk_summary, Mapping):
            raise ValueError("trend review risk summary is unavailable")
        atr = _required_decimal(action.get("atr"), "action ATR")
        planned_risk = _required_decimal(
            action.get("planned_stop_risk"), "planned stop risk"
        )
        cost_rate = _required_decimal(
            risk_summary.get("normal_cost_rate"), "normal cost rate"
        )
        if atr <= 0 or planned_risk <= 0 or cost_rate <= 0:
            raise ValueError("trend review buy completion risk is invalid")
        confirmed_risk = sum(
            (
                quantity
                * (
                    Decimal("2") * atr * fx
                    + price * fx * cost_rate
                )
                for quantity, price in fills.values()
            ),
            Decimal("0"),
        )
        remaining_risk = planned_risk - confirmed_risk
        unit_risk = (
            Decimal("2") * atr * fx
            + current_price * fx * cost_rate
        )
        caps.append(_floor_to_lot(remaining_risk / unit_risk, lot_size))
    return min(caps)


def _activate_fill_protection_line(
    *,
    data_dir: Path,
    market: str,
    symbol: str,
    execution_date: str,
    atr: Decimal,
    active_line: str,
) -> None:
    from .a_share_trend import load_protection_state, write_protection_state

    state_path = (
        data_dir / PROTECTION_STATE_ROOTS[market] / "protection_state.json"
    )
    state = load_protection_state(state_path)
    positions = dict(state["positions"])
    existing = positions.get(symbol)
    prior = dict(existing) if isinstance(existing, Mapping) else {}
    positions[symbol] = {
        **prior,
        "initial_line": str(prior.get("initial_line") or active_line),
        "active_line": active_line,
        "atr14": format(atr, "f"),
        "position_started_for": str(
            prior.get("position_started_for") or execution_date
        ),
        "tracking_active": prior.get("tracking_active") is True,
        "updated_for": execution_date,
    }
    write_protection_state(state_path, {**state, "positions": positions})


def _preflight_open_actions(
    report: Mapping[str, object], market: str
) -> tuple[list[Mapping[str, object]], str]:
    judgments = report.get("strategy_judgments")
    actions = judgments.get("formal_actions") if isinstance(judgments, Mapping) else None
    if not isinstance(actions, list):
        raise ValueError("trend report formal actions are unavailable")
    strategy_snapshot = report.get("strategy_snapshot")
    strategy_version = (
        str(strategy_snapshot.get("strategy_version") or "")
        if isinstance(strategy_snapshot, Mapping)
        else ""
    )
    if not strategy_version:
        raise ValueError("trend report strategy version is unavailable")

    from .futu_symbols import to_futu_symbol

    validated: list[Mapping[str, object]] = []
    for action in actions:
        if not isinstance(action, Mapping):
            raise ValueError("trend review action is invalid")
        action_name = str(action.get("action") or "")
        symbol = str(action.get("symbol") or "").strip()
        if action_name not in {"BUY", "SELL_ALL"} or not symbol:
            raise ValueError("trend review action is invalid")
        to_futu_symbol(market, symbol)
        if action_name == "BUY":
            try:
                target_weight = _required_decimal(
                    action.get("target_weight"), "target weight"
                )
                atr = _required_decimal(action.get("atr"), "action ATR")
                lot_size = int(action.get("lot_size") or 0)
                quantity = _required_decimal(
                    action.get("estimated_shares"), "estimated shares"
                )
            except (TypeError, ValueError):
                raise ValueError("trend review buy action is invalid") from None
            if (
                target_weight <= 0
                or atr <= 0
                or lot_size <= 0
                or quantity <= 0
                or quantity != quantity.to_integral_value()
                or quantity % lot_size
            ):
                raise ValueError("trend review buy action is invalid")
        validated.append(action)
    return validated, strategy_version


def execute_trend_review_open(
    *,
    data_dir: Path,
    report: Mapping[str, object],
    client: object,
    market: str,
    execution_date: str,
    now: str,
    quote_prices: Mapping[str, Decimal],
) -> dict[str, object]:
    market = _market(market)
    actions, strategy_version = _preflight_open_actions(report, market)
    current = datetime.fromisoformat(now)
    local_current = current.astimezone(MARKET_TIMEZONES[market])
    execution_day = date.fromisoformat(execution_date)
    same_day = local_current.date() == execution_day
    buy_window_end = "16:00" if market == "US" else "10:00"
    local_time = local_current.time().replace(tzinfo=None)
    buy_window_open = (
        datetime.strptime("09:30", "%H:%M").time()
        <= local_time
        <= datetime.strptime(buy_window_end, "%H:%M").time()
    )
    market_open = {
        "CN": (
            datetime.strptime("09:30", "%H:%M").time()
            <= local_time
            <= datetime.strptime("11:30", "%H:%M").time()
        ) or (
            datetime.strptime("13:00", "%H:%M").time()
            <= local_time
            <= datetime.strptime("15:00", "%H:%M").time()
        ),
        "HK": (
            datetime.strptime("09:30", "%H:%M").time()
            <= local_time
            <= datetime.strptime("12:00", "%H:%M").time()
        ) or (
            datetime.strptime("13:00", "%H:%M").time()
            <= local_time
            <= datetime.strptime("16:00", "%H:%M").time()
        ),
        "US": datetime.strptime("09:30", "%H:%M").time()
        <= local_time
        <= datetime.strptime("16:00", "%H:%M").time(),
    }[market]
    snapshot = client.account_snapshot()
    if not isinstance(snapshot, Mapping):
        raise TrendReviewAccountStateError("simulate account snapshot is invalid")
    _ensure_discipline_account(data_dir, market, snapshot)
    nav = _required_decimal(snapshot.get("net_value"), "simulate net value")
    if nav <= 0:
        raise TrendReviewAccountStateError("simulate net value must be positive")
    from .futu_symbols import to_futu_symbol

    report_sha = _report_hash(report)
    submitted = 0
    artifacts: list[str] = []
    blocked_status: str | None = None
    sell_symbols = {
        str(action.get("symbol") or "").strip()
        for action in actions
        if isinstance(action, Mapping) and action.get("action") == "SELL_ALL"
    }
    root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "open"
        / execution_date
    )
    ordered_actions = sorted(
        enumerate(actions),
        key=lambda item: not (
            isinstance(item[1], Mapping) and item[1].get("action") == "SELL_ALL"
        ),
    )
    for index, action in ordered_actions:
        if not isinstance(action, Mapping):
            continue
        action_name = str(action.get("action") or "")
        symbol = str(action.get("symbol") or "").strip()
        if action_name not in {"BUY", "SELL_ALL"}:
            continue
        futu_code = to_futu_symbol(market, symbol)
        side = "buy" if action_name == "BUY" else "sell"
        action_key = trend_action_key(market, execution_date, futu_code, side)
        action_evidence = {
            "market": market,
            "date": execution_date,
            "strategy_version": strategy_version,
            "report_sha256": report_sha,
            "action_index": index,
            "symbol": symbol,
            "futu_code": futu_code,
            "side": side,
        }
        stem = action_key
        intent_path = root / f"{stem}-intent.json"
        attempt = 1
        action_events_root = (
            data_dir
            / "trend_review"
            / "ledgers"
            / market
            / "actions"
            / execution_date
            / action_key
        )
        resolutions = _action_resolutions(
            action_events_root,
            market=market,
            execution_date=execution_date,
            action_key=action_key,
            symbol=symbol,
            futu_code=futu_code,
            side=side,
        )
        authorized_retries = sum(
            item.get("resolution") == "authorize-retry" for item in resolutions
        )
        action_facts = _action_facts(root, futu_code=futu_code, side=side)
        if action_name == "SELL_ALL":
            reason_id = str(
                action.get("event_id") or action.get("reason") or ""
            ).strip()
            if reason_id and not any(
                event.get("status") == "reason_added"
                and event.get("reason_id") == reason_id
                for event in _action_events(action_events_root)
            ):
                _write_action_event(
                    data_dir=data_dir,
                    market=market,
                    execution_date=execution_date,
                    action_key=action_key,
                    payload={
                        **action_evidence,
                        "status": "reason_added",
                        "reason_id": reason_id,
                        "reason": str(action.get("reason") or "sell_all"),
                    },
                    recorded_at=now,
                )
        if any(
            item.get("resolution") in {"confirm-submitted", "abandon"}
            for item in resolutions
        ):
            continue
        sell_position = next(
            (
                item
                for item in _positive_positions(snapshot)
                if str(item.get("code") or item.get("futu_code") or "")
                .strip()
                .upper()
                == futu_code.upper()
            ),
            None,
        ) if action_name == "SELL_ALL" else None
        sell_quantity = (
            int(
                _required_decimal(
                    sell_position.get("qty", sell_position.get("quantity")),
                    "position qty",
                )
            )
            if sell_position is not None
            else 0
        )
        if action_name == "SELL_ALL" and action_facts:
            position_zero_complete = any(
                event.get("status") == "filled"
                or (
                    event.get("status") == "incomplete"
                    and event.get("reason") == "position_zero_confirmed"
                )
                for event in _action_events(action_events_root)
            )
            if position_zero_complete:
                continue
        if not same_day and not (
            local_current.date() > execution_day
            and action_name == "SELL_ALL"
            and bool(action_facts)
        ):
            continue
        if action_name == "BUY" and (symbol in sell_symbols or not buy_window_open):
            if symbol not in sell_symbols:
                event_status = (
                    "pending"
                    if local_time < datetime.strptime("09:30", "%H:%M").time()
                    else "missed"
                )
                event_reason = (
                    "buy_window_not_open"
                    if event_status == "pending"
                    else "buy_window_closed"
                )
                if not any(
                    event.get("status") == event_status
                    and event.get("reason") == event_reason
                    for event in _action_events(action_events_root)
                ):
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": event_status,
                            "reason": event_reason,
                        },
                        recorded_at=now,
                    )
            continue
        if action_name == "SELL_ALL" and not market_open and sell_quantity > 0:
            continue
        if action_facts:
            pending_intent = next(
                (
                    item[0]
                    for item in action_facts
                    if not _result_path(item[0]).exists()
                    and item[3] > authorized_retries
                ),
                None,
            )
            if pending_intent is not None and (
                action_name != "SELL_ALL" or sell_quantity > 0
            ):
                request = next(
                    item[2] for item in action_facts if item[0] == pending_intent
                )
                pending_attempt = next(
                    item[3] for item in action_facts if item[0] == pending_intent
                )
                orders = _listed_orders(
                    client,
                    start=execution_date,
                    end=local_current.date().isoformat(),
                )
                broker_fact, broker_order = _broker_attempt_fact(orders, request)
                if broker_fact == "conflict":
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": "conflict",
                            "target_qty": str(request.get("qty") or ""),
                            "reason": "broker order conflicts with immutable intent",
                        },
                        recorded_at=now,
                    )
                    blocked_status = "conflict"
                    break
                rejected_status = next(
                    (
                        str(
                            order.get("order_status")
                            or order.get("status")
                            or ""
                        )
                        .strip()
                        .upper()
                        for order in [broker_order]
                        if order is not None
                        if str(
                            order.get("order_status")
                            or order.get("status")
                            or ""
                        )
                        .strip()
                        .upper()
                        in REJECTED_ORDER_STATUSES
                    ),
                    None,
                )
                if broker_order is not None:
                    _write_reconciled_result(
                        pending_intent,
                        market=market,
                        execution_date=execution_date,
                        request=request,
                        response=broker_order,
                        report_sha=report_sha,
                        action_index=index,
                        reconciled_at=now,
                    )
                if rejected_status is not None:
                    reason = f"simulate {side} order rejected: {rejected_status}"
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": "failed",
                            "target_qty": str(request.get("qty") or ""),
                            "reason": reason,
                        },
                        recorded_at=now,
                    )
                    raise RuntimeError(reason)
                if broker_order is not None:
                    continue
                _write_uncertain_action_event_once(
                    data_dir=data_dir,
                    market=market,
                    execution_date=execution_date,
                    action_key=action_key,
                    action_root=action_events_root,
                    evidence=action_evidence,
                    attempt=pending_attempt,
                    reason="intent has no conclusive broker fact",
                    target_qty=str(request.get("qty") or ""),
                    recorded_at=now,
                )
                blocked_status = "uncertain"
                break
            else:
                request = action_facts[0][2]
                orders = _listed_orders(
                    client,
                    start=execution_date,
                    end=local_current.date().isoformat(),
                )
                requests_by_remark: dict[str, list[dict[str, object]]] = {}
                for _, _, intent_request, _ in action_facts:
                    requests_by_remark.setdefault(
                        str(intent_request.get("remark") or ""), []
                    ).append(intent_request)
                conflicting_order = next(
                    (
                        order
                        for order in orders
                        if str(order.get("remark") or "") in requests_by_remark
                        and not any(
                            _order_matches_request(order, candidate)
                            for candidate in requests_by_remark[
                                str(order.get("remark") or "")
                            ]
                        )
                    ),
                    None,
                )
                if conflicting_order is not None:
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": "conflict",
                            "target_qty": str(request.get("qty") or ""),
                            "reason": "broker order conflicts with immutable intent",
                        },
                        recorded_at=now,
                    )
                    blocked_status = "conflict"
                    break
                matched = [
                    order
                    for order in orders
                    if any(
                        _order_has_action_identity(order, candidate)
                        for candidate in requests_by_remark.get(
                            str(order.get("remark") or ""), []
                        )
                    )
                ]
                ambiguous_attempt = next(
                    (
                        remark
                        for remark, candidates in requests_by_remark.items()
                        if len(
                            {
                                str(order.get("order_id") or f"missing-{position}")
                                for position, order in enumerate(orders)
                                if str(order.get("remark") or "") == remark
                                and any(
                                    _order_has_action_identity(order, candidate)
                                    for candidate in candidates
                                )
                            }
                        )
                        > 1
                    ),
                    None,
                )
                position_zero = action_name == "SELL_ALL" and sell_quantity <= 0
                inconclusive_reason = (
                    "broker action attempt is ambiguous"
                    if ambiguous_attempt is not None
                    else "broker order status is absent"
                    if action_name == "SELL_ALL"
                    and not matched
                    and not position_zero
                    and authorized_retries < max(item[3] for item in action_facts)
                    else None
                )
                if inconclusive_reason is not None:
                    attempt = max(item[3] for item in action_facts)
                    _write_uncertain_action_event_once(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        action_root=action_events_root,
                        evidence=action_evidence,
                        attempt=attempt,
                        reason=inconclusive_reason,
                        recorded_at=now,
                    )
                    blocked_status = "uncertain"
                    break
                if not matched and not position_zero and authorized_retries == 0:
                    continue
                target_quantity = _required_decimal(request.get("qty"), "target quantity")
                dealt_by_order = {
                    str(order.get("order_id") or index): _required_decimal(
                        order.get("dealt_qty", "0"), "broker dealt quantity"
                    )
                    for index, order in enumerate(matched)
                }
                broker_filled = sum(
                    dealt_by_order.values(), start=Decimal("0")
                )
                remaining = target_quantity - broker_filled
                filled = broker_filled
                if position_zero:
                    remaining = Decimal("0")
                elif action_name == "SELL_ALL":
                    remaining = Decimal(sell_quantity)
                order_ids = [
                    str(order.get("order_id"))
                    for order in matched
                    if order.get("order_id") not in {None, ""}
                ]
                weighted_prices = [
                    (
                        _required_decimal(order.get("dealt_qty"), "broker dealt quantity"),
                        _required_decimal(
                            order.get("dealt_avg_price"), "broker average fill price"
                        ),
                    )
                    for order in matched
                    if _required_decimal(
                        order.get("dealt_qty", "0"), "broker dealt quantity"
                    ) > 0
                    and order.get("dealt_avg_price") not in {None, ""}
                ]
                average_price = (
                    sum(
                        (quantity * price for quantity, price in weighted_prices),
                        start=Decimal("0"),
                    )
                    / sum(
                        (quantity for quantity, _ in weighted_prices),
                        start=Decimal("0"),
                    )
                    if weighted_prices
                    else None
                )
                protection_fact = {}
                if action_name == "BUY" and average_price is not None:
                    protection_fact = {
                        "active_protection_line": format(
                            average_price
                            - Decimal("2")
                            * _required_decimal(action.get("atr"), "action ATR"),
                            "f",
                        )
                    }
                if position_zero or any(
                    order.get("order_id")
                    or order.get("order_status")
                    or order.get("dealt_qty")
                    for order in matched
                ):
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": (
                                "filled"
                                if filled >= target_quantity
                                else "incomplete"
                                if position_zero
                                else "partially_filled"
                                if filled > 0
                                else "submitted"
                            ),
                            "filled_qty": format(filled, "f"),
                            "target_qty": format(target_quantity, "f"),
                            "avg_fill_price": (
                                format(average_price, "f")
                                if average_price is not None
                                else ""
                            ),
                            **protection_fact,
                            "order_ids": order_ids,
                            **(
                                {"reason": "position_zero_confirmed"}
                                if position_zero
                                else {}
                            ),
                        },
                        recorded_at=now,
                    )
                    if protection_fact:
                        _activate_fill_protection_line(
                            data_dir=data_dir,
                            market=market,
                            symbol=symbol,
                            execution_date=execution_date,
                            atr=_required_decimal(action.get("atr"), "action ATR"),
                            active_line=protection_fact["active_protection_line"],
                        )
                if remaining <= 0:
                    continue
                broker_statuses = {
                    str(order.get("order_status") or order.get("status") or "")
                    .strip()
                    .upper()
                    for order in matched
                }
                if broker_statuses & ACTIVE_ORDER_STATUSES:
                    continue
                if broker_statuses - TERMINAL_ORDER_STATUSES:
                    attempt = max(item[3] for item in action_facts)
                    reason = "broker order status is inconclusive"
                    _write_uncertain_action_event_once(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        action_root=action_events_root,
                        evidence=action_evidence,
                        attempt=attempt,
                        reason=reason,
                        recorded_at=now,
                    )
                    blocked_status = "uncertain"
                    break
                attempt = max(item[3] for item in action_facts) + 1
                if authorized_retries < attempt - 1:
                    reason = "remaining quantity requires retry authorization"
                    _write_uncertain_action_event_once(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        action_root=action_events_root,
                        evidence=action_evidence,
                        attempt=attempt - 1,
                        reason=reason,
                        recorded_at=now,
                    )
                    blocked_status = "uncertain"
                    break
                if action_name == "BUY":
                    remaining = Decimal(
                        _remaining_buy_quantity(
                            action,
                            report,
                            snapshot,
                            matched,
                            _required_decimal(
                                quote_prices.get(futu_code), "current quote price"
                            ),
                        )
                    )
                    if remaining <= 0:
                        continue
                request = {**request, "qty": format(remaining, "f")}
                request["remark"] = trend_attempt_remark(
                    market, execution_date, action_key, attempt
                )
                broker_fact, broker_order = _broker_attempt_fact(orders, request)
                if broker_fact == "conflict":
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": "conflict",
                            "target_qty": format(remaining, "f"),
                            "reason": "broker order conflicts with proposed attempt",
                        },
                        recorded_at=now,
                    )
                    blocked_status = "conflict"
                    break
                intent_path = root / f"{stem}-attempt-{attempt}-intent.json"
                _write_immutable(
                    intent_path,
                    _canonical_json_bytes(
                        {
                            "market": market,
                            "date": execution_date,
                            "report_sha256": report_sha,
                            "action_index": index,
                            "attempt": attempt,
                            "request": request,
                            "created_at": now,
                        }
                    ),
                )
                if broker_order is not None:
                    _write_reconciled_result(
                        intent_path,
                        market=market,
                        execution_date=execution_date,
                        request=request,
                        response=broker_order,
                        report_sha=report_sha,
                        action_index=index,
                        reconciled_at=now,
                    )
                    continue
        else:
            if action_name == "BUY":
                lot_size = int(action.get("lot_size") or 0)
                frozen_quantity = _required_decimal(
                    action.get("estimated_shares"), "estimated shares"
                )
                if (
                    not symbol
                    or lot_size <= 0
                    or frozen_quantity <= 0
                    or frozen_quantity != frozen_quantity.to_integral_value()
                    or frozen_quantity % lot_size
                ):
                    raise ValueError("trend review buy action is invalid")
                quantity = _remaining_buy_quantity(
                    action,
                    report,
                    snapshot,
                    (),
                    _required_decimal(
                        quote_prices.get(futu_code), "current quote price"
                    ),
                )
            else:
                quantity = sell_quantity
            if quantity <= 0:
                continue
            request = {
                "market": market,
                "futu_code": futu_code,
                "side": side,
                "order_type": "MARKET",
                "price": "0",
                "qty": str(quantity),
                "remark": trend_attempt_remark(
                    market, execution_date, action_key, 1
                ),
            }
            orders = _listed_orders(
                client,
                start=execution_date,
                end=local_current.date().isoformat(),
            )
            same_remark = [
                order
                for order in orders
                if str(order.get("remark") or "") == request["remark"]
            ]
            legacy_prefix = f"trend-review:{market}:{execution_date}:"
            legacy_candidates = [
                order
                for order in orders
                if str(order.get("remark") or "").startswith(legacy_prefix)
                and _order_has_action_identity(
                    order,
                    {
                        **request,
                        "remark": str(order.get("remark") or ""),
                    },
                )
            ]
            candidates = [*same_remark, *legacy_candidates]
            exact = [
                order
                for order in candidates
                if _order_matches_request(
                    order,
                    {
                        **request,
                        "remark": str(order.get("remark") or ""),
                    },
                )
            ]
            if candidates and (len(candidates) != 1 or len(exact) != 1):
                _write_action_event(
                    data_dir=data_dir,
                    market=market,
                    execution_date=execution_date,
                    action_key=action_key,
                    payload={
                        **action_evidence,
                        "status": "conflict",
                        "target_qty": str(quantity),
                        "reason": "broker action candidate is conflicting or ambiguous",
                    },
                    recorded_at=now,
                )
                blocked_status = "conflict"
                break
            if exact:
                request["remark"] = str(exact[0].get("remark") or "")
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
            if exact:
                _write_reconciled_result(
                    intent_path,
                    market=market,
                    execution_date=execution_date,
                    request=request,
                    response=exact[0],
                    report_sha=report_sha,
                    action_index=index,
                    reconciled_at=now,
                )
                continue
        base_request = action_facts[0][2] if action_facts else request
        target_qty = str(base_request.get("qty") or request.get("qty") or "")
        try:
            response = client.place_order(request)
        except Exception as exc:
            _write_action_event(
                data_dir=data_dir,
                market=market,
                execution_date=execution_date,
                action_key=action_key,
                payload={
                    **action_evidence,
                    "status": "failed",
                    "attempt": attempt,
                    "target_qty": target_qty,
                    "reason": str(exc),
                },
                recorded_at=now,
            )
            raise
        result_path = _result_path(intent_path)
        _write_immutable(
            result_path,
            _canonical_json_bytes(
                {
                    "market": market,
                    "date": execution_date,
                    "report_sha256": report_sha,
                    "action_index": index,
                    "request": request,
                    "response": response,
                    "submitted_at": now,
                }
            ),
        )
        order_id = str(response.get("futu_order_id") or "")
        _write_action_event(
            data_dir=data_dir,
            market=market,
            execution_date=execution_date,
            action_key=action_key,
            payload={
                **action_evidence,
                "status": "submitted",
                "attempt": attempt,
                "target_qty": target_qty,
                "order_ids": [order_id] if order_id else [],
            },
            recorded_at=now,
        )
        artifacts.append(str(result_path))
        submitted += 1
    return {
        "status": (
            blocked_status
            if blocked_status is not None
            else "submitted"
            if submitted
            else "unchanged"
            if buy_window_open or market_open
            else "missed_window"
        ),
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
    symbol = symbol.strip()
    event_id = event_id.strip()
    trading_date = date.fromisoformat(trading_date).isoformat()
    if not symbol or not event_id:
        raise ValueError("trend review protection event is invalid")
    return execute_trend_review_open(
        data_dir=data_dir,
        report={
            "strategy_snapshot": {"strategy_version": "protection-v1"},
            "strategy_judgments": {
                "formal_actions": [
                    {
                        "action": "SELL_ALL",
                        "symbol": symbol,
                        "event_id": event_id,
                        "reason": "protection_event",
                    }
                ]
            },
        },
        client=client,
        market=market,
        execution_date=trading_date,
        now=now,
        quote_prices={},
    )


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
    facts = _load_daily_facts(data_dir, market)
    trades = _completed_trades(facts)
    rates_path = data_dir / "rates" / "DGS3MO.csv"
    rates = _load_dgs3mo_csv(rates_path)
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
        start_date = min(str(trade["entry_date"]) for trade in selected_trades)
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
        "broker": {"CN": "eastmoney", "US": "tiger", "HK": "phillips"}[market],
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
    snapshot = evidence.get("strategy_snapshot")
    if not isinstance(snapshot, Mapping):
        raise TrendReplayIncompleteError(
            "missing original input: strategy_snapshot"
        )
    strategy_version = str(snapshot.get("strategy_version") or "")
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
    if strategy_version in {"v2", "v3", "v4"}:
        required.add("normal_cost_rate")
    if strategy_version in {"v3", "v4"}:
        required.update({"kelly_rounds", "kelly_data_reason"})
    if strategy_version == "v4":
        required.add("drawdown_summary")
    missing = sorted(required - inputs.keys())
    if missing:
        raise TrendReplayIncompleteError(
            f"missing original input: {missing[0]}"
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
    from .trend_kelly import (
        TREND_API_STATS_SCHEMA_VERSION,
        trend_kelly_rounds_from_payload,
    )

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
    normal_cost_rate = decimal_or_none(inputs.get("normal_cost_rate"))
    if strategy_version in {"v2", "v3", "v4"} and (
        normal_cost_rate is None
        or not normal_cost_rate.is_finite()
        or normal_cost_rate < 0
    ):
        raise TrendReplayIncompleteError("invalid original input: normal_cost_rate")
    kelly_rounds_raw = inputs.get("kelly_rounds", [])
    if not isinstance(kelly_rounds_raw, list):
        raise TrendReplayIncompleteError("invalid original input: kelly_rounds")
    try:
        kelly_rounds = trend_kelly_rounds_from_payload(
            {
                "schema_version": TREND_API_STATS_SCHEMA_VERSION,
                "rounds": kelly_rounds_raw,
            }
        )
    except ValueError:
        raise TrendReplayIncompleteError(
            "invalid original input: kelly_rounds"
        ) from None
    if len(kelly_rounds) != len(kelly_rounds_raw):
        raise TrendReplayIncompleteError("invalid original input: kelly_rounds")
    kelly_data_reason = inputs.get("kelly_data_reason", "")
    if not isinstance(kelly_data_reason, str):
        raise TrendReplayIncompleteError(
            "invalid original input: kelly_data_reason"
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
        normal_cost_rate=normal_cost_rate or Decimal("0"),
        process_version=process_version,
        candidate_pool_ids=tuple(int(item) for item in inputs["candidate_pool_ids"]),
        strategy_snapshot=snapshot,
        kelly_rounds=kelly_rounds,
        kelly_data_reason=kelly_data_reason,
        drawdown_summary=(
            inputs["drawdown_summary"]
            if strategy_version == "v4"
            and isinstance(inputs.get("drawdown_summary"), Mapping)
            else None
        ),
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
