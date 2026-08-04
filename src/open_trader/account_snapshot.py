from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping
from zoneinfo import ZoneInfo

from .account_sync_state import (
    ACCOUNT_STATE_VERSION,
    REQUIRED_BROKERS,
    build_dashboard_projection,
    effective_source_status,
    is_valid_account_publication,
)
from .futu_universe import build_account_quote_universe


MAX_STABLE_READ_ATTEMPTS = 3
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_FUTU_NAIVE_PRICE_TIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\Z"
)
_US_MARKET_TIMEZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class SnapshotResult:
    status_code: int
    payload: dict[str, object]
    etag: str | None


class PublicationUnavailable(Exception):
    def __init__(self, code: str, worker_git_sha: str = "") -> None:
        self.code = code
        self.worker_git_sha = worker_git_sha


def load_worker_git_sha(data_dir: Path) -> str:
    try:
        return _worker_sha(_read_bytes(data_dir / "account_sync/controller_status.json"))
    except (OSError, PublicationUnavailable):
        return ""


def load_account_snapshot(
    data_dir: Path, *, api_git_sha: str, now: datetime
) -> SnapshotResult:
    worker_sha = ""
    try:
        account, quotes, worker_sha = _load_stable_publication(data_dir)
        if not _is_git_sha(api_git_sha) or api_git_sha != worker_sha:
            return _unavailable("account_release_mismatch", api_git_sha, worker_sha)
        return _build_snapshot(account, quotes, api_git_sha, worker_sha, now)
    except PublicationUnavailable as error:
        return _unavailable(
            error.code,
            api_git_sha,
            error.worker_git_sha or worker_sha,
        )


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


def build_instrument_id(market: str, asset_class: str, symbol: str) -> str:
    return _opaque_id("ins_", [
        market.strip().upper(),
        asset_class.strip().lower(),
        symbol.strip().upper(),
    ])


def build_position_id(broker: str, account_alias: str, instrument_id: str) -> str:
    return _opaque_id("pos_", [
        broker.strip().lower(),
        account_alias.strip(),
        instrument_id,
    ])


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _read_required(path: Path, publication: str) -> bytes:
    try:
        return _read_bytes(path)
    except OSError as error:
        code = "account_release_mismatch" if publication == "heartbeat" else f"{publication}_publication_missing"
        raise PublicationUnavailable(code) from error


def _load_stable_publication(data_dir: Path) -> tuple[dict[str, object], dict[str, object], str]:
    account_path = data_dir / "latest/account_sync_state.json"
    quotes_path = data_dir / "latest/quotes.json"
    heartbeat_path = data_dir / "account_sync/controller_status.json"
    for _attempt in range(MAX_STABLE_READ_ATTEMPTS):
        account_first = _read_required(account_path, "account")
        quotes_first = _read_required(quotes_path, "quotes")
        heartbeat = _read_required(heartbeat_path, "heartbeat")
        worker_sha = _worker_sha(heartbeat)
        try:
            account_second = _read_required(account_path, "account")
            quotes_second = _read_required(quotes_path, "quotes")
            if account_first != account_second or quotes_first != quotes_second:
                continue
            account = _parse_account(account_first)
            quotes = _parse_quotes(quotes_first)
            projection = account["dashboard_projection"]
            assert isinstance(projection, dict)
            if projection["quote_as_of"] != quotes["last_success_at"]:
                continue
            brokers = account["brokers"]
            assert isinstance(brokers, dict)
            incomplete_refresh = (
                quotes["status"] == "partial" and quotes["missing_count"] > 0
            ) or (
                quotes["status"] == "failed"
                and not _has_complete_quote_coverage(brokers, quotes)
            )
            if not incomplete_refresh and not _projection_is_paired(account, quotes):
                continue
            return account, quotes, worker_sha
        except PublicationUnavailable as error:
            raise PublicationUnavailable(error.code, worker_sha) from error
    raise PublicationUnavailable("account_publication_unstable", worker_sha)


def _parse_account(raw: bytes) -> dict[str, object]:
    try:
        account = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationUnavailable("account_publication_invalid") from error
    if not isinstance(account, dict):
        raise PublicationUnavailable("account_publication_invalid")
    if account.get("version") != ACCOUNT_STATE_VERSION:
        raise PublicationUnavailable("account_schema_unsupported")
    if not is_valid_account_publication(account):
        raise PublicationUnavailable("account_publication_invalid")
    return account


def _parse_quotes(raw: bytes) -> dict[str, object]:
    try:
        quotes = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationUnavailable("quotes_publication_invalid") from error
    if not isinstance(quotes, dict):
        raise PublicationUnavailable("quotes_publication_invalid")
    required = {
        "status": str,
        "requested_count": int,
        "quote_count": int,
        "missing_count": int,
        "fetched_at": str,
        "last_success_at": str,
        "stale": bool,
        "quotes": dict,
        "diagnostic": dict,
    }
    if (
        quotes.get("status") not in {"ok", "partial", "failed"}
        or any(
            not isinstance(quotes.get(field), kind)
            or (kind is int and isinstance(quotes.get(field), bool))
            for field, kind in required.items()
        )
        or any(
            not isinstance(symbol, str) or not isinstance(row, dict)
            for symbol, row in quotes["quotes"].items()
        )
        or any(
            count < 0
            for count in (
                quotes["requested_count"],
                quotes["quote_count"],
                quotes["missing_count"],
            )
        )
        or quotes["quote_count"] + quotes["missing_count"]
        != quotes["requested_count"]
        or not _is_aware_timestamp(quotes["fetched_at"])
        or (
            quotes["last_success_at"]
            and not _is_aware_timestamp(quotes["last_success_at"])
        )
        or any(
            not _is_valid_quote_row_payload(symbol, row)
            for symbol, row in quotes["quotes"].items()
        )
    ):
        raise PublicationUnavailable("quotes_publication_invalid")
    rows = quotes["quotes"]
    assert isinstance(rows, dict)
    ok_count = sum(row["status"] == "ok" for row in rows.values())
    missing_count = sum(row["status"] == "missing_quote" for row in rows.values())
    retained_failure = (
        quotes["status"] == "failed"
        and quotes["requested_count"] == 0
        and quotes["quote_count"] == 0
        and quotes["missing_count"] == 0
        and bool(rows)
        and bool(quotes["last_success_at"])
    )
    if not retained_failure and (
        quotes["status"] in {"ok", "partial"} or rows
    ) and (
        len(rows) != quotes["requested_count"]
        or ok_count != quotes["quote_count"]
        or missing_count != quotes["missing_count"]
    ):
        raise PublicationUnavailable("quotes_publication_invalid")
    return quotes


def _is_valid_quote_row_payload(symbol: object, row: object) -> bool:
    if not isinstance(symbol, str) or not isinstance(row, dict):
        return False
    required = {
        "market": str,
        "symbol": str,
        "status": str,
        "last_price": str,
        "price_session": str,
        "price_time": str,
        "fetched_at": str,
        "stale": bool,
    }
    if any(
        not isinstance(row.get(field), kind)
        for field, kind in required.items()
    ):
        return False
    if (
        not row["market"]
        or not row["symbol"]
        or row["market"] + "." + row["symbol"] != symbol
        or row["status"] not in {"ok", "missing_quote"}
        or not _is_aware_timestamp(row["fetched_at"])
        or not _is_valid_quote_price_time(row["market"], row["price_time"])
    ):
        return False
    if row["status"] == "ok":
        try:
            price = Decimal(row["last_price"])
        except InvalidOperation:
            return False
        return price.is_finite() and price > 0
    return not row["last_price"]


def _is_valid_quote_price_time(market: object, value: object) -> bool:
    if not isinstance(market, str) or not isinstance(value, str):
        return False
    if not value or _is_aware_timestamp(value):
        return True
    if market != "US" or _FUTU_NAIVE_PRICE_TIME_RE.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return False
    return True


def _worker_sha(raw: bytes) -> str:
    try:
        heartbeat = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationUnavailable("account_release_mismatch") from error
    if not _is_valid_heartbeat(heartbeat):
        raise PublicationUnavailable("account_release_mismatch")
    git_sha = heartbeat.get("git_sha")
    if not _is_git_sha(git_sha):
        raise PublicationUnavailable("account_release_mismatch")
    return git_sha


def _is_valid_heartbeat(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    required = {
        "schema_version": str,
        "working_directory": str,
        "git_sha": str,
        "phase": str,
        "account_loop": dict,
        "quote_loop": dict,
    }
    return (
        value.get("schema_version") == "open_trader.account_sync.controller.v1"
        and not isinstance(value.get("pid"), bool)
        and isinstance(value.get("pid"), int)
        and all(isinstance(value.get(field), kind) for field, kind in required.items())
        and (value.get("blocker") is None or isinstance(value.get("blocker"), str))
        and all(_is_aware_timestamp(value.get(field, "")) for field in ("started_at", "heartbeat_at"))
    )


def _is_git_sha(value: object) -> bool:
    return isinstance(value, str) and _GIT_SHA_RE.fullmatch(value) is not None


def _unavailable(code: str, api_git_sha: str, worker_git_sha: str) -> SnapshotResult:
    source = "release" if code == "account_release_mismatch" else "quotes" if code.startswith("quotes_") else "account"
    message = (
        "Account API and Account Sync Worker releases differ"
        if code == "account_release_mismatch"
        else "Quotes publication is unavailable"
        if source == "quotes"
        else "Account publication is unstable"
        if code == "account_publication_unstable"
        else "Account publication is unavailable"
    )
    return SnapshotResult(
        503,
        {
            "schema_version": 1,
            "status": "unavailable",
            "release": {"api_git_sha": api_git_sha, "worker_git_sha": worker_git_sha},
            "errors": [{
                "code": code,
                "source": source,
                "message": message,
                "retryable": True,
            }],
        },
        None,
    )


def _projection_is_paired(
    account: Mapping[str, object], quotes: Mapping[str, object]
) -> bool:
    projection = account.get("dashboard_projection")
    if not isinstance(projection, dict):
        return False
    generated_at = projection.get("generated_at")
    if not isinstance(generated_at, str):
        return False
    try:
        expected = build_dashboard_projection(
            account,
            quotes,
            generated_at=generated_at,
        )
    except Exception:
        return False
    return _projection_contract_view(expected) == _projection_contract_view(projection)


def _projection_contract_view(projection: Mapping[str, object]) -> dict[str, object]:
    return {
        "generated_at": projection["generated_at"],
        "quote_as_of": projection["quote_as_of"],
        "summary": _public_summary(projection["summary"]),
        "broker_summaries": [
            _public_broker_summary(row) for row in projection["broker_summaries"]
        ],
        "broker_positions": [
            _public_position(row) for row in projection["broker_positions"]
        ],
        "cash_details": [
            _public_cash_balance(row) for row in projection["cash_details"]
        ],
    }


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
    accepted_account_as_of = _accepted_account_as_of(brokers)
    if accepted_account_as_of is None:
        raise PublicationUnavailable("account_publication_missing")
    if not _has_complete_quote_coverage(brokers, quotes):
        raise PublicationUnavailable("quotes_publication_missing")
    quote_as_of = quotes["last_success_at"]
    assert isinstance(quote_as_of, str)
    if not _is_aware_timestamp(quote_as_of):
        raise PublicationUnavailable("quotes_publication_missing")
    quotes_status = quotes["status"]
    missing_count = quotes["missing_count"]
    assert isinstance(quotes_status, str) and isinstance(missing_count, int)
    if quotes_status == "partial" and missing_count > 0:
        raise PublicationUnavailable("quotes_publication_missing")
    if quotes_status == "failed" and not quote_as_of:
        raise PublicationUnavailable("quotes_publication_missing")
    broker_stale = {
        broker: _is_broker_stale(brokers[broker], now)
        for broker in REQUIRED_BROKERS
    }
    quotes_stale = quotes_status == "failed"
    account_stale = any(broker_stale.values())
    stale = account_stale or quotes_stale
    sources = {
        "account": {
            "status": "stale" if account_stale else "healthy",
            "as_of": accepted_account_as_of,
            "reason": "broker_refresh_failed" if account_stale else None,
            "brokers": {
                broker: _broker_source(brokers[broker], broker_stale[broker])
                for broker in sorted(REQUIRED_BROKERS)
            },
        },
        "quotes": {
            "status": "stale" if quotes_stale else "healthy",
            "as_of": quote_as_of,
            "reason": "quotes_refresh_failed" if quotes_stale else None,
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
        "quote_as_of": quote_as_of,
        "status": "stale" if stale else "healthy",
        "stale": stale,
        "sources": sources,
        "release": {"api_git_sha": api_git_sha, "worker_git_sha": worker_sha},
        "summary": summary,
        "broker_summaries": broker_summaries,
        "positions": positions,
        "cash_balances": cash_balances,
        "errors": _stale_errors(broker_stale, quotes_stale),
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
    instrument_id = build_instrument_id(
        position["market"], position["asset_class"], position["symbol"]
    )
    position["instrument_id"] = instrument_id
    position["position_id"] = build_position_id(
        position["broker"], position["account_alias"], instrument_id
    )
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


def _public_position(row: Mapping[str, object]) -> dict[str, object]:
    position = {
        key: row[key]
        for key in (
            "broker", "account_alias", "market", "asset_class", "symbol", "name", "currency",
            "quantity", "cost_price", "cost_value", "last_price", "price_kind", "price_as_of",
            "market_value", "market_value_usd", "market_value_hkd", "cost_value_hkd",
            "unrealized_pnl", "unrealized_pnl_pct", "account_weight_hkd", "portfolio_weight_hkd",
            "statement_id", "confidence", "notes",
        )
    }
    position["price_as_of"] = _normalize_public_price_as_of(
        position["market"], position["price_as_of"]
    )
    if isinstance(row.get("current_valuation"), Mapping):
        position["current_valuation"] = dict(row["current_valuation"])
        position["current_valuation"]["price_as_of"] = _normalize_public_price_as_of(
            position["market"], position["current_valuation"]["price_as_of"]
        )
    return position


def _normalize_public_price_as_of(market: str, value: str) -> str:
    if market != "US" or _FUTU_NAIVE_PRICE_TIME_RE.fullmatch(value) is None:
        return value
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
    return parsed.replace(tzinfo=_US_MARKET_TIMEZONE).isoformat(timespec="milliseconds")


def _public_cash_balance(row: object) -> dict[str, str]:
    assert isinstance(row, Mapping)
    return {
        key: row[key]
        for key in (
            "broker", "account_alias", "currency", "cash_balance", "available_balance",
            "cash_balance_hkd", "available_balance_hkd", "statement_id", "confidence", "notes",
        )
    }


def _accepted_account_as_of(brokers: Mapping[str, object]) -> str | None:
    accepted: list[str] = []
    for broker in REQUIRED_BROKERS:
        source = brokers[broker]
        if not isinstance(source, Mapping) or source.get("status") == "unknown":
            return None
        last_success_at = source.get("last_success_at")
        if not isinstance(last_success_at, str) or not last_success_at:
            return None
        if not _is_aware_timestamp(last_success_at):
            return None
        accepted.append(last_success_at)
    return max(accepted, key=datetime.fromisoformat)


def _is_aware_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _is_broker_stale(source: object, now: datetime) -> bool:
    assert isinstance(source, Mapping)
    return source["status"] == "failed" or effective_source_status(source, now=now) == "stale"


def _has_complete_quote_coverage(
    brokers: Mapping[str, object], quotes: Mapping[str, object]
) -> bool:
    published = quotes["quotes"]
    assert isinstance(published, dict)
    required = {
        (item.market, item.symbol)
        for item in build_account_quote_universe({"brokers": brokers}).items
    }
    return all(
        any(_is_valid_quote_row(row, market, symbol) for row in published.values())
        for market, symbol in required
    )


def _is_valid_quote_row(row: object, market: str, symbol: str) -> bool:
    return (
        _is_valid_quote_row_payload(f"{market}.{symbol}", row)
        and isinstance(row, Mapping)
        and row.get("market") == market
        and row.get("symbol") == symbol
        and row.get("status") == "ok"
    )


def _stale_errors(
    broker_stale: Mapping[str, bool], quotes_stale: bool
) -> list[dict[str, object]]:
    errors = [
        {
            "code": "broker_refresh_failed",
            "source": broker,
            "message": "Latest broker refresh failed; serving last accepted account facts",
            "retryable": True,
        }
        for broker in sorted(REQUIRED_BROKERS)
        if broker_stale[broker]
    ]
    if quotes_stale:
        errors.append({
            "code": "quotes_refresh_failed",
            "source": "quotes",
            "message": "Latest quote refresh failed; serving last accepted quotes",
            "retryable": True,
        })
    return errors


def _broker_source(source: object, stale: bool) -> dict[str, object]:
    assert isinstance(source, Mapping)
    return {
        "source_kind": source["source_kind"],
        "status": "stale" if stale else "healthy",
        "data_as_of": source["data_as_of"],
        "last_success_at": source["last_success_at"],
        "reason": "broker_refresh_failed" if stale else None,
    }
