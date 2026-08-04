from __future__ import annotations

from datetime import datetime
import json
import math
from typing import Mapping
import urllib.error
import urllib.request

from .account_api import ACCOUNT_ROUTE_HEADER, PRODUCTION_ROUTE_MARKER
from .account_sync_state import (
    REQUIRED_BROKERS,
    STATEMENT_BROKERS,
    statement_generation_digest,
)


DEFAULT_ACCOUNT_API_URL = "http://127.0.0.1:8768"
DEFAULT_ACCOUNT_TIMEOUT_SECONDS = 5.0
_SNAPSHOT_FIELDS = frozenset({
    "schema_version", "snapshot_generation", "account_generation", "generated_at",
    "quote_as_of", "status", "stale", "sources", "release", "summary",
    "broker_summaries", "positions", "cash_balances", "errors",
    "accepted_statement_generation",
})
_STATEMENT_FACTS_FIELDS = frozenset({
    "schema_version", "broker", "statement_generation", "statement_period",
    "trade_facts_cutoff_at", "trade_facts_sha256", "facts",
})
_SOURCES_FIELDS = frozenset({"account", "quotes"})
_ACCOUNT_SOURCE_FIELDS = frozenset({"status", "as_of", "reason", "brokers"})
_QUOTE_SOURCE_FIELDS = frozenset({"status", "as_of", "reason"})
_BROKER_SOURCE_FIELDS = frozenset({
    "source_kind", "data_as_of", "last_success_at", "status", "reason",
})


class AccountHttpError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def fetch_account_snapshot(
    base_url: str = DEFAULT_ACCOUNT_API_URL,
    timeout_seconds: float = DEFAULT_ACCOUNT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    payload = _get_json(f"{base_url.rstrip('/')}/api/v1/account/snapshot", timeout_seconds)
    if not _is_valid_snapshot(payload):
        raise AccountHttpError("account_contract_invalid")
    return payload


def fetch_statement_trade_facts(
    base_url: str,
    broker: str,
    statement_generation: str,
    timeout_seconds: float,
) -> dict[str, object]:
    if broker not in STATEMENT_BROKERS or statement_generation_digest(statement_generation) is None:
        raise AccountHttpError("account_contract_invalid")
    payload = _get_json(
        f"{base_url.rstrip('/')}/api/v1/account/statements/{broker}/"
        f"{statement_generation}/trade-facts",
        timeout_seconds,
    )
    if not _is_valid_statement_facts(payload, broker, statement_generation):
        raise AccountHttpError("account_contract_invalid")
    return payload


def _get_json(url: str, timeout_seconds: float) -> dict[str, object]:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    request = urllib.request.Request(
        url, headers={ACCOUNT_ROUTE_HEADER: PRODUCTION_ROUTE_MARKER}
    )
    error_code: str | None = None
    payload: object = None
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if getattr(response, "status", None) != 200:
                error_code = "account_unavailable"
            else:
                payload = json.load(response)
    except urllib.error.HTTPError as error:
        error_code = _safe_http_error_code(error)
    except (OSError, TimeoutError, urllib.error.URLError, UnicodeError, json.JSONDecodeError):
        error_code = "account_unavailable"
    if error_code is not None:
        raise AccountHttpError(error_code)
    if not isinstance(payload, dict):
        raise AccountHttpError("account_contract_invalid")
    return payload


def _safe_http_error_code(error: urllib.error.HTTPError) -> str:
    if error.code != 409:
        return "account_unavailable"
    try:
        payload = json.load(error)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "account_unavailable"
    if isinstance(payload, Mapping) and payload.get("code") == "accepted_statement_generation_changed":
        return "accepted_statement_generation_changed"
    return "account_unavailable"


def _is_valid_snapshot(payload: Mapping[str, object]) -> bool:
    if (
        set(payload) != _SNAPSHOT_FIELDS
        or payload.get("schema_version") != 1
        or payload.get("status") not in {"healthy", "stale"}
        or payload.get("stale") is not (payload.get("status") == "stale")
        or statement_generation_digest(payload.get("snapshot_generation")) is None
        or statement_generation_digest(payload.get("account_generation")) is None
        or not _is_valid_sources(payload.get("sources"))
    ):
        return False
    generations = payload.get("accepted_statement_generation")
    return (
        isinstance(generations, Mapping)
        and set(generations) == set(STATEMENT_BROKERS)
        and all(
            generation == "" or statement_generation_digest(generation) is not None
            for generation in generations.values()
        )
    )


def _is_valid_sources(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _SOURCES_FIELDS:
        return False
    account = value.get("account")
    quotes = value.get("quotes")
    if (
        not isinstance(account, Mapping)
        or not isinstance(quotes, Mapping)
        or set(account) != _ACCOUNT_SOURCE_FIELDS
        or set(quotes) != _QUOTE_SOURCE_FIELDS
        or not isinstance(account.get("status"), str)
        or not isinstance(account.get("as_of"), str)
        or not isinstance(account.get("reason"), (str, type(None)))
        or not isinstance(quotes.get("status"), str)
        or not isinstance(quotes.get("as_of"), str)
        or not isinstance(quotes.get("reason"), (str, type(None)))
    ):
        return False
    brokers = account.get("brokers")
    return (
        isinstance(brokers, Mapping)
        and set(brokers) == set(REQUIRED_BROKERS)
        and all(
            isinstance(source, Mapping)
            and set(source) == _BROKER_SOURCE_FIELDS
            and all(
                isinstance(source.get(field), str)
                for field in _BROKER_SOURCE_FIELDS - {"reason"}
            )
            and isinstance(source.get("reason"), (str, type(None)))
            for source in brokers.values()
        )
    )


def _is_valid_statement_facts(
    payload: Mapping[str, object], broker: str, statement_generation: str
) -> bool:
    if (
        set(payload) != _STATEMENT_FACTS_FIELDS
        or payload.get("schema_version") != "open_trader.account.statement_trade_facts.v1"
        or payload.get("broker") != broker
        or payload.get("statement_generation") != statement_generation
        or not isinstance(payload.get("statement_period"), str)
        or statement_generation_digest(payload.get("trade_facts_sha256")) is None
        or not isinstance(payload.get("facts"), list)
    ):
        return False
    cutoff = payload.get("trade_facts_cutoff_at")
    if not isinstance(cutoff, str):
        return False
    try:
        return datetime.fromisoformat(cutoff).tzinfo is not None
    except ValueError:
        return False
