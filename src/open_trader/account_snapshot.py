from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .account_sync_state import (
    REQUIRED_BROKERS,
    effective_source_status,
    is_valid_account_publication,
)


@dataclass(frozen=True)
class SnapshotResult:
    status_code: int
    payload: dict[str, object]
    etag: str | None


def load_account_snapshot(
    data_dir: Path, *, api_git_sha: str, now: datetime
) -> SnapshotResult:
    account, quotes, worker_sha = _load_stable_publication(data_dir)
    return _build_snapshot(account, quotes, api_git_sha, worker_sha, now)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _opaque_id(prefix: str, values: list[str]) -> str:
    return prefix + hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _load_stable_publication(data_dir: Path) -> tuple[dict[str, object], dict[str, object], str]:
    account_path = data_dir / "latest/account_sync_state.json"
    quotes_path = data_dir / "latest/quotes.json"
    account_bytes = account_path.read_bytes()
    quotes_bytes = quotes_path.read_bytes()
    worker = json.loads((data_dir / "account_sync/controller_status.json").read_text(encoding="utf-8"))
    if account_bytes != account_path.read_bytes() or quotes_bytes != quotes_path.read_bytes():
        raise ValueError("account publication changed during read")
    account = json.loads(account_bytes)
    quotes = json.loads(quotes_bytes)
    if not is_valid_account_publication(account) or not isinstance(quotes, dict):
        raise ValueError("invalid account publication")
    projection = account["dashboard_projection"]
    assert isinstance(projection, dict)
    if projection["quote_as_of"] != quotes.get("last_success_at"):
        raise ValueError("quote publication time mismatch")
    if not isinstance(worker, dict) or not isinstance(worker.get("git_sha"), str):
        raise ValueError("invalid account worker status")
    return account, quotes, worker["git_sha"]


def _build_snapshot(
    account: Mapping[str, object],
    quotes: Mapping[str, object],
    api_git_sha: str,
    worker_sha: str,
    now: datetime,
) -> SnapshotResult:
    projection = account["dashboard_projection"]
    brokers = account["brokers"]
    assert isinstance(projection, dict) and isinstance(brokers, dict)
    summary = _public_summary(projection["summary"])
    broker_summaries = sorted(
        (_public_broker_summary(row) for row in projection["broker_summaries"]),
        key=lambda row: row["broker"],
    )
    positions = sorted(
        (_position_row(row) for row in projection["broker_positions"]),
        key=lambda row: (row["broker"], row["account_alias"], row["market"], row["asset_class"], row["symbol"], row["position_id"]),
    )
    cash_balances = sorted(
        (_public_cash_balance(row) for row in projection["cash_details"]),
        key=lambda row: (row["broker"], row["account_alias"], row["currency"]),
    )
    accepted_account_as_of = max(
        (brokers[broker]["last_success_at"] for broker in REQUIRED_BROKERS),
        key=datetime.fromisoformat,
    )
    sources = {
        "account": {
            "status": "healthy",
            "as_of": accepted_account_as_of,
            "reason": None,
            "brokers": {
                broker: _broker_source(brokers[broker], now)
                for broker in sorted(REQUIRED_BROKERS)
            },
        },
        "quotes": {
            "status": "healthy",
            "as_of": quotes["last_success_at"],
            "reason": None,
        },
    }
    account_generation = _sha256({
        "summary": summary,
        "broker_summaries": broker_summaries,
        "positions": positions,
        "cash_balances": cash_balances,
        "accepted_account_as_of": accepted_account_as_of,
        "accepted_broker_data_as_of": {
            broker: brokers[broker]["data_as_of"] for broker in sorted(REQUIRED_BROKERS)
        },
    })
    payload_without_snapshot_generation = {
        "schema_version": 1,
        "account_generation": account_generation,
        "generated_at": projection["generated_at"],
        "quote_as_of": quotes["last_success_at"],
        "status": "healthy",
        "stale": False,
        "sources": sources,
        "release": {"api_git_sha": api_git_sha, "worker_git_sha": worker_sha},
        "summary": summary,
        "broker_summaries": broker_summaries,
        "positions": positions,
        "cash_balances": cash_balances,
        "errors": [],
    }
    snapshot_generation = _sha256(payload_without_snapshot_generation)
    payload_tail = dict(payload_without_snapshot_generation)
    schema_version = payload_tail.pop("schema_version")
    payload = {
        "schema_version": schema_version,
        "snapshot_generation": snapshot_generation,
        **payload_tail,
    }
    return SnapshotResult(200, payload, f'"account-v1-{snapshot_generation.removeprefix("sha256:")}"')


def _position_row(row: Mapping[str, str]) -> dict[str, str]:
    position = _public_position(row)
    instrument_id = _opaque_id("ins_", [
        position["market"].strip().upper(),
        position["asset_class"].strip().lower(),
        position["symbol"].strip().upper(),
    ])
    position["instrument_id"] = instrument_id
    position["position_id"] = _opaque_id("pos_", [
        position["broker"].strip().lower(), position["account_alias"].strip(), instrument_id,
    ])
    return position


def _public_summary(row: object) -> dict[str, object]:
    assert isinstance(row, Mapping)
    return {
        key: row[key]
        for key in (
            "holding_value_hkd", "cash_like_value_hkd", "portfolio_value_hkd",
            "holding_weight_hkd", "cash_like_weight_hkd", "holding_count", "broker_count",
        )
    }


def _public_broker_summary(row: object) -> dict[str, object]:
    assert isinstance(row, Mapping)
    return {
        key: row[key]
        for key in (
            "broker", "label", "source_kind", "detail_available", "holding_value_hkd",
            "cash_like_value_hkd", "portfolio_value_hkd", "holding_count",
        )
    }


def _public_position(row: Mapping[str, str]) -> dict[str, str]:
    return {
        key: row[key]
        for key in (
            "broker", "account_alias", "market", "asset_class", "symbol", "name", "currency",
            "quantity", "cost_price", "cost_value", "last_price", "price_kind", "price_as_of",
            "market_value", "market_value_usd", "market_value_hkd", "cost_value_hkd",
            "unrealized_pnl", "unrealized_pnl_pct", "account_weight_hkd", "portfolio_weight_hkd",
            "statement_id", "confidence", "notes",
        )
    }


def _public_cash_balance(row: object) -> dict[str, str]:
    assert isinstance(row, Mapping)
    return {
        key: row[key]
        for key in (
            "broker", "account_alias", "currency", "cash_balance", "available_balance",
            "cash_balance_hkd", "available_balance_hkd", "statement_id", "confidence", "notes",
        )
    }


def _broker_source(source: object, now: datetime) -> dict[str, object]:
    assert isinstance(source, Mapping)
    status = effective_source_status(source, now=now)
    return {
        "source_kind": source["source_kind"],
        "status": "healthy" if status == "ok" else "stale",
        "data_as_of": source["data_as_of"],
        "last_success_at": source["last_success_at"],
        "reason": None if status == "ok" else "broker_refresh_failed",
    }
