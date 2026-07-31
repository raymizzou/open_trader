from __future__ import annotations

import json
import os
import re
import csv
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Mapping, Sequence

from .csv_io import write_rows
from .fx import StaticMonthEndFxProvider
from .models import AssetClass, CashBalance, Market, Position
from . import pipeline
from .portfolio import (
    PORTFOLIO_FIELDNAMES,
    PortfolioBuildError,
    build_portfolio_rows,
    money,
    recalculate_portfolio_weights,
)


ACCOUNT_STATE_VERSION = 1
REQUIRED_BROKERS = ("futu", "tiger", "phillips", "eastmoney")
LIVE_BROKERS = ("futu", "tiger")
ACCOUNT_STALE_SECONDS = 180
QUOTE_STALE_SECONDS = 15
CONTROLLER_STALE_SECONDS = 15


@dataclass(frozen=True)
class BrokerAccountCandidate:
    broker: str
    source_kind: str
    data_as_of: str
    period: str
    positions: tuple[Position, ...]
    cash: tuple[CashBalance, ...]
    fx_rates: tuple[dict[str, str], ...]
    summary: dict[str, object]


def _source_kind_for_broker(broker: str) -> str:
    return "live" if broker in LIVE_BROKERS else "statement"


def _empty_source(broker: str) -> dict[str, object]:
    return {
        "source_kind": _source_kind_for_broker(broker),
        "status": "unknown",
        "attempted_at": "",
        "last_success_at": "",
        "data_as_of": "",
        "period": "",
        "message": "",
        "positions": [],
        "cash": [],
        "fx_rates": [],
        "summary": {},
    }


def empty_account_sync_state() -> dict[str, object]:
    return {
        "version": ACCOUNT_STATE_VERSION,
        "generation": "",
        "brokers": {broker: _empty_source(broker) for broker in REQUIRED_BROKERS},
    }


def load_account_sync_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_account_sync_state()
    return payload if _is_valid_state(payload) else empty_account_sync_state()


def load_latest_statement_candidate(
    data_dir: Path,
    broker: Literal["phillips", "eastmoney"],
) -> BrokerAccountCandidate | None:
    if broker not in {"phillips", "eastmoney"}:
        raise ValueError(f"unsupported statement broker: {broker}")
    runs_dir = data_dir / "runs"
    if not runs_dir.is_dir():
        return None
    candidates = [
        candidate
        for run_dir in runs_dir.iterdir()
        if run_dir.is_dir()
        if (candidate := _statement_candidate_from_run(run_dir, broker)) is not None
    ]
    return max(candidates, key=lambda item: (item.period, item.data_as_of)) if candidates else None


def _statement_candidate_from_run(
    run_dir: Path, broker: Literal["phillips", "eastmoney"]
) -> BrokerAccountCandidate | None:
    try:
        manifest = _read_detail_rows(run_dir / "manifest.csv")
        positions = _read_detail_rows(run_dir / "extracted_positions.csv")
        cash = _read_detail_rows(run_dir / "extracted_cash.csv")
        position_rows = [row for row in positions if row.get("broker", "").strip().lower() == broker]
        cash_rows = [row for row in cash if row.get("broker", "").strip().lower() == broker]
        detail_rows = [*position_rows, *cash_rows]
        statement_ids = {row.get("statement_id", "") for row in detail_rows}
        if not detail_rows or len(statement_ids) != 1:
            return None
        data_as_of, period = _statement_period(next(iter(statement_ids)), broker)
        if not any(
            row.get("broker", "").strip().lower() == broker
            and row.get("status", "") == "parsed"
            and row.get("month", "") == period[:7]
            for row in manifest
        ):
            return None
        candidate = BrokerAccountCandidate(
            broker=broker,
            source_kind="statement",
            data_as_of=data_as_of,
            period=period,
            positions=tuple(pipeline._position_from_row(row) for row in position_rows),
            cash=tuple(pipeline._cash_from_row(row) for row in cash_rows),
            fx_rates=(),
            summary={
                "position_count": len(position_rows),
                "cash_count": len(cash_rows),
                "is_real_time": False,
            },
        )
        accept_candidate(empty_account_sync_state(), candidate, attempted_at=data_as_of)
        return candidate
    except (OSError, KeyError, TypeError, ValueError, InvalidOperation, csv.Error):
        return None


def _read_detail_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _statement_period(
    statement_id: str, broker: Literal["phillips", "eastmoney"]
) -> tuple[str, str]:
    match = re.fullmatch(rf"(\d{{4}}-\d{{2}}-\d{{2}})-{broker}", statement_id)
    if match is None:
        raise ValueError("invalid statement ID")
    data_as_of = date.fromisoformat(match.group(1)).isoformat()
    return data_as_of, data_as_of if broker == "phillips" else data_as_of[:7]


def accept_candidate(
    state: Mapping[str, object],
    candidate: BrokerAccountCandidate,
    *,
    attempted_at: str,
) -> dict[str, object]:
    if candidate.broker not in REQUIRED_BROKERS:
        raise ValueError(f"unknown broker: {candidate.broker}")
    if candidate.source_kind != _source_kind_for_broker(candidate.broker):
        raise ValueError(f"invalid source_kind: {candidate.source_kind}")
    accepted = deepcopy(state) if _is_valid_state(state) else empty_account_sync_state()
    brokers = accepted["brokers"]
    assert isinstance(brokers, dict)
    brokers[candidate.broker] = {
        "source_kind": candidate.source_kind,
        "status": "ok",
        "attempted_at": attempted_at,
        "last_success_at": attempted_at,
        "data_as_of": candidate.data_as_of,
        "period": candidate.period,
        "message": "",
        "positions": [_serialize_dataclass(item) for item in candidate.positions],
        "cash": [_serialize_dataclass(item) for item in candidate.cash],
        "fx_rates": [dict(item) for item in candidate.fx_rates],
        "summary": deepcopy(candidate.summary),
    }
    accepted["generation"] = attempted_at
    return accepted


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temp_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
        temp_name = ""
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def record_source_failure(
    state: Mapping[str, object],
    broker: str,
    *,
    attempted_at: str,
    message: str,
    sensitive_values: Sequence[str] = (),
    sensitive_roots: Sequence[Path] = (),
) -> dict[str, object]:
    if broker not in REQUIRED_BROKERS:
        raise ValueError(f"unknown broker: {broker}")
    failed = deepcopy(state) if _is_valid_state(state) else empty_account_sync_state()
    brokers = failed["brokers"]
    assert isinstance(brokers, dict)
    source = brokers[broker]
    assert isinstance(source, dict)
    source["status"] = "failed"
    source["attempted_at"] = attempted_at
    source["message"] = sanitize_sync_error(message, sensitive_values, sensitive_roots)
    failed["generation"] = attempted_at
    return failed


def sanitize_sync_error(
    message: str,
    sensitive_values: Sequence[str] = (),
    sensitive_roots: Sequence[Path] = (),
) -> str:
    sanitized = str(message)
    for value in sorted((item for item in sensitive_values if item), key=len, reverse=True):
        sanitized = sanitized.replace(value, "<redacted>")
    for root in sorted((str(item) for item in sensitive_roots), key=len, reverse=True):
        sanitized = sanitized.replace(root, "<path>")
    sanitized = re.sub(
        r"(?i)(\bauthorization\s*:\s*bearer\s+)[^\s,;]+",
        r"\1<redacted>",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)(\b(?:password|passwd|api[_-]?key|(?:access|refresh)[_-]?token|"
        r"client[_-]?secret|private[_-]?key|credential(?:s)?|secret|token)\s*(?:=|:)\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1<redacted>",
        sanitized,
    )
    sanitized = re.sub(r"\d{6,}", "<redacted>", sanitized)
    sanitized = re.sub(r"(?:~|/Users/[^/\s]+|/home/[^/\s]+)(?:/[^\s:]+)+", "<path>", sanitized)
    return re.sub(r"(?i)\b[^\s]*tiger[^\s]*(?:\.json|\.ini|\.cfg|\.config)\b", "<path>", sanitized)


def effective_source_status(
    source: Mapping[str, object], *, now: datetime
) -> str:
    status = source.get("status")
    if status not in {"ok", "failed", "unknown"}:
        return "unknown"
    if status != "ok":
        return str(status)
    source_kind = source.get("source_kind")
    if source_kind not in {"live", "statement"}:
        return "unknown"
    if source_kind == "statement":
        return "ok"
    last_success = _parse_aware_datetime(source.get("last_success_at"))
    if last_success is None:
        return "stale"
    return "stale" if (now - last_success).total_seconds() > ACCOUNT_STALE_SECONDS else "ok"


def project_account_sync_health(
    state: Mapping[str, object],
    controller_status: Mapping[str, object],
    quotes: Mapping[str, object],
    *,
    now: datetime,
) -> dict[str, object]:
    valid_state = state if _is_valid_state(state) else empty_account_sync_state()
    controller = _project_controller_status(controller_status, now)
    brokers = valid_state["brokers"]
    assert isinstance(brokers, dict)
    projected_brokers = {
        broker: _project_source(brokers[broker], now) for broker in REQUIRED_BROKERS
    }
    quote_status = _effective_quote_status(quotes, now)
    reason = ""
    if controller["status"] != "ok":
        reason = f"controller_{controller['status']}"
    else:
        for broker in REQUIRED_BROKERS:
            status = projected_brokers[broker]["status"]
            if status != "ok":
                reason = f"broker_{broker}_{status}"
                break
        if not reason and quote_status != "ok":
            reason = f"quotes_{quote_status}"
        if not reason and not valid_state["generation"]:
            reason = "portfolio_missing"
    return {
        "status": "ok" if not reason else "abnormal",
        "label": "同步正常" if not reason else "同步异常",
        "reason": reason,
        "portfolio_generation": valid_state["generation"],
        "controller": controller,
        "quotes": {"status": quote_status},
        "brokers": projected_brokers,
    }


def accepted_portfolio_rows(state: Mapping[str, object]) -> list[dict[str, str]]:
    valid_state = state if _is_valid_state(state) else empty_account_sync_state()
    brokers = valid_state["brokers"]
    assert isinstance(brokers, dict)
    positions: list[Position] = []
    cash: list[CashBalance] = []
    source_rates: dict[tuple[str, str, str], Decimal] = {}
    month = ""
    for broker in REQUIRED_BROKERS:
        source = brokers[broker]
        assert isinstance(source, dict)
        if source["status"] == "unknown":
            continue
        if not month:
            month = _source_month(source)
        positions.extend(_position_from_row(row) for row in source["positions"])
        cash.extend(_cash_from_row(row) for row in source["cash"])
        for rate in source["fx_rates"]:
            parsed_rate = _rate_from_row(rate)
            if parsed_rate is not None:
                account_alias, currency, value = parsed_rate
                source_rates[(broker, account_alias, currency)] = value
    _reject_duplicate_identities(positions, cash)
    if not positions and not cash:
        return []
    if not month:
        raise PortfolioBuildError("accepted source is missing period")
    rows = build_portfolio_rows(
        month,
        positions,
        cash,
        StaticMonthEndFxProvider(
            month,
            {"HKD": Decimal("1"), "USD": Decimal("7.8"), "CNY": Decimal("1.08")},
        ),
    )
    _apply_accepted_fx_rates(rows, source_rates)
    return rows


def write_portfolio_atomic(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temp_name = handle.name
        write_rows(Path(temp_name), PORTFOLIO_FIELDNAMES, rows)
        os.replace(temp_name, path)
        temp_name = ""
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def _serialize_dataclass(value: Position | CashBalance) -> dict[str, str]:
    return {
        key: _decimal_string(item) if isinstance(item, Decimal) or item is None else str(item)
        for key, item in asdict(value).items()
    }


def _decimal_string(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def _project_controller_status(
    controller_status: Mapping[str, object], now: datetime
) -> dict[str, object]:
    if not _is_valid_controller_status(controller_status):
        return {"status": "unknown"}
    heartbeat_at = str(controller_status["heartbeat_at"])
    heartbeat = _parse_aware_datetime(heartbeat_at)
    assert heartbeat is not None
    return {
        "status": "stale"
        if (now - heartbeat).total_seconds() > CONTROLLER_STALE_SECONDS
        else "ok",
        "pid": controller_status["pid"],
        "git_sha": controller_status["git_sha"],
        "heartbeat_at": heartbeat_at,
    }


def _project_source(source: Mapping[str, object], now: datetime) -> dict[str, str]:
    status = effective_source_status(source, now=now)
    data_as_of = str(source.get("data_as_of", ""))
    if status == "ok":
        display = "同步正常"
    elif status == "failed":
        display = "同步失败" + (f" · 数据截至 {data_as_of}" if data_as_of else "")
    elif status == "stale":
        display = "数据已过期" + (f" · 数据截至 {data_as_of}" if data_as_of else "")
    else:
        display = "同步状态未知 · 数据未验证"
    return {
        "status": status,
        "data_as_of": data_as_of,
        "last_success_at": str(source.get("last_success_at", "")),
        "message": str(source.get("message", "")),
        "display": display,
    }


def _effective_quote_status(quotes: Mapping[str, object], now: datetime) -> str:
    status = quotes.get("status")
    if status == "partial" and quotes.get("missing_count") == 0:
        status = "ok"
    if status != "ok":
        return "failed" if quotes.get("status") == "failed" else "unknown"
    last_success = _parse_aware_datetime(quotes.get("last_success_at"))
    if quotes.get("stale") is True or last_success is None:
        return "stale"
    return "stale" if (now - last_success).total_seconds() > QUOTE_STALE_SECONDS else "ok"


def _is_valid_controller_status(value: Mapping[str, object]) -> bool:
    required = {
        "schema_version": str,
        "working_directory": str,
        "git_sha": str,
        "phase": str,
        "account_loop": dict,
        "quote_loop": dict,
    }
    if value.get("schema_version") != "open_trader.account_sync.controller.v1":
        return False
    if isinstance(value.get("pid"), bool) or not isinstance(value.get("pid"), int):
        return False
    if any(not isinstance(value.get(key), kind) for key, kind in required.items()):
        return False
    if value.get("blocker") is not None and not isinstance(value.get("blocker"), str):
        return False
    return all(
        _parse_aware_datetime(value.get(field)) is not None
        for field in ("started_at", "heartbeat_at")
    )


def _parse_aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _source_month(source: Mapping[str, object]) -> str:
    period = source.get("period")
    if isinstance(period, str) and len(period) >= 7:
        return period[:7]
    data_as_of = source.get("data_as_of")
    return data_as_of[:7] if isinstance(data_as_of, str) else ""


def _position_from_row(row: object) -> Position:
    if not isinstance(row, dict):
        raise PortfolioBuildError("invalid accepted position")
    try:
        return Position(
            statement_id=_required_string(row, "statement_id"),
            broker=_required_string(row, "broker"),
            account_alias=_required_string(row, "account_alias"),
            market=Market(_required_string(row, "market")),
            asset_class=AssetClass(_required_string(row, "asset_class")),
            symbol=_required_string(row, "symbol"),
            name=_required_string(row, "name"),
            currency=_required_string(row, "currency"),
            quantity=_decimal_from_row(row, "quantity"),
            cost_price=_optional_decimal_from_row(row, "cost_price"),
            last_price=_optional_decimal_from_row(row, "last_price"),
            market_value=_optional_decimal_from_row(row, "market_value"),
            cost_value=_optional_decimal_from_row(row, "cost_value"),
            unrealized_pnl=_optional_decimal_from_row(row, "unrealized_pnl"),
            confidence=_required_string(row, "confidence"),  # type: ignore[arg-type]
            notes=_required_string(row, "notes"),
        )
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise PortfolioBuildError("invalid accepted position") from exc


def _cash_from_row(row: object) -> CashBalance:
    if not isinstance(row, dict):
        raise PortfolioBuildError("invalid accepted cash")
    try:
        return CashBalance(
            statement_id=_required_string(row, "statement_id"),
            broker=_required_string(row, "broker"),
            account_alias=_required_string(row, "account_alias"),
            currency=_required_string(row, "currency"),
            cash_balance=_decimal_from_row(row, "cash_balance"),
            available_balance=_optional_decimal_from_row(row, "available_balance"),
            confidence=_required_string(row, "confidence"),  # type: ignore[arg-type]
            notes=_required_string(row, "notes"),
        )
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise PortfolioBuildError("invalid accepted cash") from exc


def _required_string(row: Mapping[str, object], field: str) -> str:
    value = row[field]
    if not isinstance(value, str):
        raise ValueError(field)
    return value


def _decimal_from_row(row: Mapping[str, object], field: str) -> Decimal:
    value = _required_string(row, field)
    decimal = Decimal(value)
    if not decimal.is_finite():
        raise ValueError(field)
    return decimal


def _optional_decimal_from_row(row: Mapping[str, object], field: str) -> Decimal | None:
    value = _required_string(row, field)
    return None if not value else _decimal_from_row(row, field)


def _rate_from_row(row: object) -> tuple[str, str, Decimal] | None:
    if not isinstance(row, dict):
        return None
    try:
        rate = _decimal_from_row(row, "rate_to_hkd")
        if rate <= 0:
            return None
        return _required_string(row, "account_alias"), _required_string(row, "currency").upper(), rate
    except (KeyError, ValueError, InvalidOperation):
        return None


def _reject_duplicate_identities(
    positions: Sequence[Position], cash: Sequence[CashBalance]
) -> None:
    position_keys: set[tuple[str, str, Market, AssetClass, str, str]] = set()
    for position in positions:
        key = (
            position.broker,
            position.account_alias,
            position.market,
            position.asset_class,
            position.symbol.upper(),
            position.currency.upper(),
        )
        if key in position_keys:
            raise PortfolioBuildError("duplicate position identity")
        position_keys.add(key)
    cash_keys: set[tuple[str, str, str]] = set()
    for balance in cash:
        key = (balance.broker, balance.account_alias, balance.currency.upper())
        if key in cash_keys:
            raise PortfolioBuildError("duplicate cash identity")
        cash_keys.add(key)


def _apply_accepted_fx_rates(
    rows: list[dict[str, str]], source_rates: Mapping[tuple[str, str, str], Decimal]
) -> None:
    for row in rows:
        brokers = row["brokers"].split(";")
        accounts = row["accounts"].split(";")
        if len(brokers) != 1 or len(accounts) != 1:
            continue
        rate = source_rates.get((brokers[0], accounts[0], row["currency"].upper()))
        if rate is None:
            continue
        row["fx_source"] = "accepted_source"
        row["fx_to_hkd"] = format(rate, "f")
        if row["market_value"]:
            row["market_value_hkd"] = money(Decimal(row["market_value"]) * rate)
        if row["cost_value"]:
            row["cost_value_hkd"] = money(Decimal(row["cost_value"]) * rate)
    if rows and all(row["market_value_hkd"] for row in rows):
        recalculate_portfolio_weights(rows)


def _is_valid_state(value: object) -> bool:
    if not isinstance(value, dict) or value.get("version") != ACCOUNT_STATE_VERSION:
        return False
    if not isinstance(value.get("generation"), str):
        return False
    brokers = value.get("brokers")
    if not isinstance(brokers, dict) or set(brokers) != set(REQUIRED_BROKERS):
        return False
    return all(
        _is_valid_source(brokers[broker], broker) for broker in REQUIRED_BROKERS
    )


def _is_valid_source(value: object, broker: str) -> bool:
    if not isinstance(value, dict):
        return False
    required_strings = {
        "source_kind",
        "status",
        "attempted_at",
        "last_success_at",
        "data_as_of",
        "period",
        "message",
    }
    if any(not isinstance(value.get(field), str) for field in required_strings):
        return False
    if value["source_kind"] != _source_kind_for_broker(broker):
        return False
    if not (
        value["status"] in {"ok", "failed", "unknown"}
        and isinstance(value.get("positions"), list)
        and isinstance(value.get("cash"), list)
        and isinstance(value.get("fx_rates"), list)
        and isinstance(value.get("summary"), dict)
    ):
        return False
    return all(_is_valid_position_row(row, broker) for row in value["positions"]) and all(
        _is_valid_cash_row(row, broker) for row in value["cash"]
    ) and all(
        isinstance(rate, dict)
        and all(isinstance(key, str) and isinstance(item, str) for key, item in rate.items())
        for rate in value["fx_rates"]
    )


def _is_valid_position_row(row: object, broker: str) -> bool:
    if not isinstance(row, dict) or "account_id" in row:
        return False
    try:
        return _position_from_row(row).broker == broker
    except PortfolioBuildError:
        return False


def _is_valid_cash_row(row: object, broker: str) -> bool:
    if not isinstance(row, dict) or "account_id" in row:
        return False
    try:
        return _cash_from_row(row).broker == broker
    except PortfolioBuildError:
        return False
