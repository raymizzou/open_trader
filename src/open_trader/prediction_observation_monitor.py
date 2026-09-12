"""Persistent, read-only monitoring for a bounded observation pool.

The observation monitor deliberately owns only candidate membership and paper
results.  Formal relation selection, solver proofs, and execution remain on
their existing paths.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .prediction_arbitrage_store import PredictionArbitrageStore
from .prediction_n_leg_validation import (
    PAPER_THREE_WAY_TEMPLATE,
    price_observation,
)

logger = logging.getLogger(__name__)

OBSERVATION_POOL_LIMIT = 10
OBSERVATION_TOKEN_LIMIT = 30
OBSERVATION_RECALC_SECONDS = 2.0
OBSERVATION_CATALOG_SECONDS = 5.0
OBSERVATION_BOOK_FRESHNESS_SECONDS = 10.0

_HISTORICAL_RESULT_FIELDS = (
    "quantity_lots",
    "payout_lower_bound_units",
    "cost_upper_bound_units",
    "guaranteed_profit_units",
    "net_roi",
    "qualification_status",
    "evaluated_at",
    "fees",
    "legs",
    "order_rules",
    "capital_release_at",
)

_TERMINAL_SOURCE_STATUSES = frozenset(
    {"RESOLVED", "CLOSED", "INVALID", "TERMINAL", "FINAL", "CANCELLED"}
)
_FORMAL_EXCLUDED_STATUSES = frozenset({"REJECTED", "REVOKED", "EXPIRED"})


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _utc(value: object) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    else:
        raise ValueError("end_date must be an ISO timestamp")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("end_date must include an explicit timezone")
    return moment.astimezone(UTC)


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _status(value: object) -> str:
    return str(value or "").strip().upper()


def _source_condition_ids(row: Mapping[str, object]) -> tuple[str, ...]:
    identifiers: set[str] = set()
    endpoint_identifiers: set[str] = set()
    endpoints = row.get("endpoints")
    if isinstance(endpoints, Sequence) and not isinstance(endpoints, (str, bytes)):
        for endpoint in endpoints:
            if not isinstance(endpoint, Mapping):
                continue
            key = endpoint.get("settlement_observation_key")
            if isinstance(key, str) and key.strip():
                value = key.split("|", 1)[0].strip()
                if value:
                    endpoint_identifiers.add(value)
            else:
                for name in ("condition_id", "market_id"):
                    value = endpoint.get(name)
                    if isinstance(value, str) and value.strip():
                        endpoint_identifiers.add(value.strip())
    identifiers.update(endpoint_identifiers)
    # Compiled terminal-state/action keys are the fallback only when the
    # endpoint itself does not carry a validated source identity.  Native
    # models use token ids as market_contract_id, so never query those.
    if endpoint_identifiers:
        return tuple(sorted(identifiers))
    model = _mapping(row.get("model"))
    problem = _mapping(model.get("problem")) if model else None
    states = problem.get("terminal_state_sets") if problem else None
    if isinstance(states, Sequence) and not isinstance(states, (str, bytes)):
        for state in states:
            if not isinstance(state, Mapping):
                continue
            key = _mapping(state.get("settlement_observation_key"))
            if key:
                value = key.get("condition_id") or key.get("indicator_id")
                if isinstance(value, str) and value.strip():
                    identifiers.add(value.strip())
    return tuple(sorted(identifiers))


def _candidate_end_date(row: Mapping[str, object]) -> datetime:
    endpoints = row.get("endpoints")
    if not isinstance(endpoints, Sequence) or isinstance(endpoints, (str, bytes)):
        raise ValueError("candidate has no endpoint dates")
    dates: list[datetime] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, Mapping):
            raise ValueError("candidate endpoint is invalid")
        value = endpoint.get("end_date")
        if value is None:
            value = endpoint.get("expires_at")
        if value is None:
            value = endpoint.get("market_date")
        dates.append(_utc(value))
    if not dates:
        raise ValueError("candidate has no endpoint dates")
    return max(dates)


def _candidate_tokens(row: Mapping[str, object]) -> tuple[str, ...]:
    model = _mapping(row.get("model"))
    if model is None:
        raise ValueError("candidate has no model")
    problem = _mapping(model.get("problem"))
    actions = problem.get("actions") if problem else None
    contracts: list[str] = []
    if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes)):
        for action in actions:
            if not isinstance(action, Mapping):
                continue
            contract = action.get("market_contract_id")
            if isinstance(contract, str) and contract.strip():
                contracts.append(contract.strip())
    tokens: set[str] = set()
    endpoints = row.get("endpoints")
    if isinstance(endpoints, Sequence) and not isinstance(endpoints, (str, bytes)):
        for endpoint in endpoints:
            if not isinstance(endpoint, Mapping):
                continue
            for key in ("contract_id", "yes_token_id", "no_token_id"):
                value = endpoint.get(key)
                if isinstance(value, str) and value.strip():
                    tokens.add(value.strip())
    model_tokens = model.get("tokens")
    if isinstance(model_tokens, Mapping):
        for value in model_tokens.values():
            if isinstance(value, Mapping):
                for token in value.values():
                    if isinstance(token, str) and token.strip():
                        tokens.add(token.strip())
            elif isinstance(value, str) and value.strip():
                tokens.add(value.strip())
    # Native rows use their token ids as model contract ids.  For a three-way
    # row the model token map is authoritative and includes only executable
    # YES tokens in the observation request.
    if _status(row.get("relation_type")) == "NATIVE_COMPLEMENT":
        tokens.update(contracts)
    elif model.get("template") == PAPER_THREE_WAY_TEMPLATE and isinstance(model_tokens, Mapping):
        tokens = {
            str(value.get("YES"))
            for value in model_tokens.values()
            if isinstance(value, Mapping)
            and isinstance(value.get("YES"), str)
            and value.get("YES")
        }
    if not tokens:
        raise ValueError("candidate has no executable token ids")
    return tuple(sorted(tokens))


def _candidate_source_status(
    row: Mapping[str, object], monitor: object | None
) -> tuple[str | None, str | None]:
    for name in ("source_status", "market_status", "resolution_status"):
        value = _status(row.get(name))
        if value in _TERMINAL_SOURCE_STATUSES:
            return value, "SOURCE_TERMINAL"
    for name in ("resolved", "closed", "invalid"):
        if row.get(name) is True:
            return name.upper(), "SOURCE_TERMINAL"
    status_reader = getattr(monitor, "observation_status", None)
    if callable(status_reader):
        checked = False
        saw_open = False
        for condition_id in _source_condition_ids(row):
            checked = True
            try:
                status = status_reader(condition_id=condition_id)
            except Exception:
                return "UNKNOWN", "SOURCE_UNKNOWN"
            if isinstance(status, Mapping):
                source_status = _status(status.get("status"))
                if source_status in _TERMINAL_SOURCE_STATUSES:
                    return source_status, "SOURCE_TERMINAL"
                if source_status in {"UNKNOWN", "STALE", "ERROR", "UNAVAILABLE"}:
                    return source_status, "SOURCE_UNKNOWN"
                if source_status == "OPEN":
                    saw_open = True
                    continue
                return "UNKNOWN", "SOURCE_UNKNOWN"
            # A source reader that has not published a status yet is an
            # unresolved lifecycle fact. Keep the candidate's structure and
            # subscription identities, but fail closed for this pricing pass.
            if status is None:
                return "UNKNOWN", "SOURCE_UNKNOWN"
        if checked:
            if saw_open:
                return "OPEN", None
            # No condition yielded an explicit OPEN status. Terminal status is
            # handled above; absence is never evidence that a market is live.
            return "UNKNOWN", "SOURCE_UNKNOWN"
        return "UNKNOWN", "SOURCE_UNKNOWN"
    # Synthetic/catalog-only callers have no source status seam. Preserve the
    # existing pure pricing behavior for those tests and offline projections.
    return None, None


def _candidate_source_metadata(
    row: Mapping[str, object], monitor: object | None
) -> tuple[dict[str, object] | None, str | None]:
    """Collect immutable source facts while preserving the candidate shape."""

    reader = getattr(monitor, "observation_source_metadata", None)
    if not callable(reader):
        reader = getattr(monitor, "observation_status", None)
    if not callable(reader):
        return None, None
    by_condition: dict[str, dict[str, object]] = {}
    fingerprints: dict[str, str] = {}
    for condition_id in _source_condition_ids(row):
        try:
            value = reader(condition_id=condition_id)
        except Exception:
            return None, "SOURCE_UNKNOWN"
        if not isinstance(value, Mapping):
            continue
        status = _status(value.get("status"))
        if status in _TERMINAL_SOURCE_STATUSES:
            return None, "SOURCE_TERMINAL"
        facts = value.get("source_facts")
        if isinstance(facts, Mapping) and facts:
            by_condition[condition_id] = copy.deepcopy(dict(facts))
            fingerprint = value.get("source_fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                fingerprints[condition_id] = fingerprint
            else:
                fingerprints[condition_id] = _fingerprint(facts)
    if not by_condition:
        return None, None
    return {
        "by_condition": by_condition,
        "fingerprints": fingerprints,
        "fingerprint": _fingerprint(fingerprints),
    }, None


def _source_endpoint_condition(endpoint: Mapping[str, object]) -> str | None:
    for name in ("condition_id", "conditionId", "market_id", "marketId"):
        value = endpoint.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    key = endpoint.get("settlement_observation_key")
    if isinstance(key, str) and key.strip():
        return key.split("|", 1)[0].strip() or None
    return None


def _row_with_source_facts(
    candidate: Mapping[str, object],
) -> tuple[dict[str, object], str | None]:
    """Apply current source-owned facts to a temporary pricing row."""

    row_value = candidate.get("row")
    row = copy.deepcopy(dict(row_value)) if isinstance(row_value, Mapping) else {}
    metadata = candidate.get("source_metadata")
    by_condition = metadata.get("by_condition") if isinstance(metadata, Mapping) else None
    if not isinstance(by_condition, Mapping):
        return row, None
    model = _mapping(row.get("model"))
    model_copy = copy.deepcopy(dict(model)) if model is not None else None
    model_rules = _mapping(model_copy.get("rules")) if model_copy is not None else None
    model_facts_value = (
        _mapping(model_copy.get("fee_facts")) if model_copy is not None else None
    )
    model_facts = dict(model_facts_value) if model_facts_value is not None else None
    updated_endpoints: list[dict[str, object]] = []
    endpoints = row.get("endpoints")
    if not isinstance(endpoints, Sequence) or isinstance(endpoints, (str, bytes)):
        return row, None
    source_fee_names = ("fees_enabled", "fee_rate", "fee_exponent", "taker_only")
    for raw_endpoint in endpoints:
        if not isinstance(raw_endpoint, Mapping):
            continue
        endpoint = copy.deepcopy(dict(raw_endpoint))
        condition_id = _source_endpoint_condition(endpoint)
        facts = by_condition.get(condition_id) if condition_id is not None else None
        if isinstance(facts, Mapping):
            if facts.get("fee_facts_status") == "UNKNOWN":
                return row, "UNKNOWN_FEE_FACTS"
            source_rules = facts.get("settlement_rules") or facts.get("rules")
            contract = endpoint.get("contract_id")
            catalog_rules = endpoint.get("settlement_rules")
            if catalog_rules is None and isinstance(model_rules, Mapping) and isinstance(contract, str):
                catalog_rules = model_rules.get(contract)
            if isinstance(source_rules, str) and isinstance(catalog_rules, str) and source_rules != catalog_rules:
                return row, "SOURCE_RULES_CHANGED"
            source_rules_hash = facts.get("rules_hash")
            observation_key = endpoint.get("settlement_observation_key")
            if isinstance(source_rules_hash, str) and isinstance(observation_key, str):
                key_parts = observation_key.split("|")
                if len(key_parts) >= 4 and key_parts[-1] != source_rules_hash:
                    return row, "SOURCE_RULES_CHANGED"
            if facts.get("neg_risk") is True:
                return row, "SOURCE_RULES_CHANGED"
            for name in source_fee_names:
                endpoint.pop(name, None)
            for name in (
                *source_fee_names,
                "minimum_order_size",
                "tick_size",
                "minimum_order_notional",
            ):
                if name in facts:
                    endpoint[name] = copy.deepcopy(facts[name])
            if isinstance(source_rules, str):
                endpoint["settlement_rules"] = source_rules
            if model_facts is not None and isinstance(contract, str):
                model_facts[contract] = {
                    target: copy.deepcopy(facts[source])
                    for source, target in (
                        ("fee_exponent", "exponent"),
                        ("taker_only", "taker_only"),
                    )
                    if source in facts
                }
        updated_endpoints.append(endpoint)
    row["endpoints"] = updated_endpoints
    if model_copy is not None:
        if model_rules is not None:
            model_copy["rules"] = dict(model_rules)
        if model_facts is not None:
            model_copy["fee_facts"] = dict(model_facts)
        row["model"] = model_copy
    return row, None


def _snapshot_rows(snapshot: object) -> tuple[dict[str, object], dict[str, object]]:
    metadata: dict[str, object] = {}
    rows_value = snapshot
    if isinstance(snapshot, Mapping) and "rows" in snapshot:
        rows_value = snapshot.get("rows")
        metadata = {
            key: copy.deepcopy(snapshot.get(key))
            for key in ("generation", "generation_fingerprint", "latest")
            if key in snapshot
        }
    rows: dict[str, object] = {}
    if isinstance(rows_value, Mapping):
        rows = {str(identity): value for identity, value in rows_value.items()}
    elif isinstance(rows_value, Sequence) and not isinstance(rows_value, (str, bytes)):
        for index, value in enumerate(rows_value):
            if isinstance(value, Mapping):
                identity = value.get("identity") or value.get("version_id") or index
                rows[str(identity)] = value
    else:
        raise ValueError("observation catalog snapshot rows are invalid")
    if "generation" not in metadata:
        metadata["generation"] = 0
    if "generation_fingerprint" not in metadata:
        metadata["generation_fingerprint"] = _fingerprint(
            sorted((identity, _fingerprint(row)) for identity, row in rows.items())
        )
    return metadata, {identity: dict(row) for identity, row in rows.items() if isinstance(row, Mapping)}


def _book_input_payload(books: Mapping[str, object]) -> dict[str, object]:
    """Strip monitor diagnostics that change with wall-clock age only."""

    volatile = {"fresh", "status", "age_seconds", "current"}
    payload: dict[str, object] = {}
    for token, value in books.items():
        if isinstance(value, Mapping):
            payload[str(token)] = {
                str(key): copy.deepcopy(item)
                for key, item in value.items()
                if str(key) not in volatile
            }
        else:
            payload[str(token)] = copy.deepcopy(value)
    return payload


def _books_expired(books: Mapping[str, object], now: datetime) -> bool:
    for value in books.values():
        if not isinstance(value, Mapping):
            return True
        confirmed_at = value.get("confirmed_at")
        if not isinstance(confirmed_at, datetime):
            return True
        age = (now - confirmed_at.astimezone(UTC)).total_seconds()
        if age < 0 or age > OBSERVATION_BOOK_FRESHNESS_SECONDS:
            return True
    return False


def _latest_watermark(metadata: Mapping[str, object]) -> object:
    latest = metadata.get("latest")
    if isinstance(latest, Mapping):
        return _fingerprint(sorted((str(key), str(value)) for key, value in latest.items()))
    return metadata.get("generation_fingerprint") or metadata.get("fingerprint")


def _first_display_value(
    row: Mapping[str, object], endpoint: Mapping[str, object] | None, *keys: str
) -> object | None:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
        if endpoint is not None:
            value = endpoint.get(key)
            if value is not None and value != "":
                return value
    return None


def _display_fields(
    row: Mapping[str, object],
    *,
    identity: str,
    candidate: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Expose bounded, non-executable candidate facts to the read model."""

    endpoint: Mapping[str, object] | None = None
    endpoints = row.get("endpoints")
    if isinstance(endpoints, Sequence) and not isinstance(endpoints, (str, bytes)):
        endpoint = next(
            (value for value in endpoints if isinstance(value, Mapping)), None
        )
    relation_type = (
        candidate.get("relation_type")
        if isinstance(candidate, Mapping)
        else row.get("relation_type")
    )
    version_id = (
        candidate.get("version_id")
        if isinstance(candidate, Mapping)
        else row.get("version_id") or row.get("id")
    )
    version_fingerprint = (
        candidate.get("version_fingerprint")
        if isinstance(candidate, Mapping)
        else row.get("version_fingerprint") or row.get("fingerprint")
    )
    approval = (
        candidate.get("approval_status")
        if isinstance(candidate, Mapping)
        else row.get("approval_status") or row.get("status")
    )
    lifecycle = (
        candidate.get("lifecycle")
        if isinstance(candidate, Mapping)
        else row.get("lifecycle") or row.get("status")
    )
    model = _mapping(row.get("model"))
    capital_release_at = _first_display_value(
        row,
        endpoint,
        "capital_release_at",
        "capitalReleaseAt",
    )
    if capital_release_at is None and model is not None:
        capital_release_at = model.get("capital_release")
    values: dict[str, object] = {
        "event": _first_display_value(
            row,
            endpoint,
            "event",
            "event_title",
            "event_identity_basis",
            "title",
            "question",
        )
        or identity,
        "title": _first_display_value(row, endpoint, "title", "question"),
        "source_scope": _first_display_value(
            row, endpoint, "source_scope", "discovery_source", "venue"
        ),
        "relation_type": relation_type,
        "version_id": version_id,
        "version_fingerprint": version_fingerprint,
        "approval_status": approval or "UNKNOWN",
        "lifecycle": lifecycle or "UNKNOWN",
        "capital_release_at": capital_release_at,
        "capital_release_status": (
            "UNKNOWN" if capital_release_at in (None, "") else "KNOWN"
        ),
    }
    if isinstance(candidate, Mapping):
        values["source_status"] = candidate.get("source_status")
        values["source_reason"] = candidate.get("source_reason")
        tokens = candidate.get("tokens")
        if isinstance(tokens, Sequence) and not isinstance(tokens, (str, bytes)):
            values["token_count"] = len(tokens)
    return {
        key: value
        for key, value in values.items()
        if value is not None and value != ""
    }


def _oldest_book_timestamp(books: Mapping[str, object]) -> datetime | None:
    timestamps: list[datetime] = []
    for value in books.values():
        if not isinstance(value, Mapping):
            continue
        confirmed_at = value.get("confirmed_at")
        try:
            timestamps.append(_utc(confirmed_at))
        except (TypeError, ValueError):
            continue
    return min(timestamps) if timestamps else None


class PredictionObservationMonitor:
    """One bounded background loop for read-only candidate observation."""

    def __init__(
        self,
        *,
        catalog: object,
        store: PredictionArbitrageStore,
        monitor: object | None = None,
        candidate_source: Callable[[], object] | None = None,
        book_source: Callable[[tuple[str, ...]], Mapping[str, object]] | None = None,
        clock: Callable[[], datetime] | None = None,
        pool_limit: int = OBSERVATION_POOL_LIMIT,
        token_limit: int = OBSERVATION_TOKEN_LIMIT,
    ) -> None:
        if type(pool_limit) is not int or not 1 <= pool_limit <= OBSERVATION_POOL_LIMIT:
            raise ValueError("pool_limit must be between one and ten")
        if type(token_limit) is not int or not 1 <= token_limit <= OBSERVATION_TOKEN_LIMIT:
            raise ValueError("token_limit must be between one and thirty")
        self._store = store
        self._monitor = monitor
        self._book_source = book_source
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pool_limit = pool_limit
        self._token_limit = token_limit
        self._candidate_source = candidate_source
        self._catalog_generation_source = getattr(catalog, "observation_generation_meta", None)
        if not callable(self._catalog_generation_source):
            self._catalog_generation_source = getattr(catalog, "generation_meta", None)
        if self._candidate_source is None:
            reader = getattr(catalog, "observation_snapshot", None)
            if callable(reader):
                self._candidate_source = reader
            elif isinstance(catalog, Mapping):
                self._candidate_source = lambda: catalog
            else:
                raise TypeError("catalog must expose observation_snapshot")
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._membership: dict[str, dict[str, str]] = {}
        self._states: dict[str, dict[str, object]] = {}
        self._catalog_metadata: dict[str, object] = {}
        self._catalog_snapshot: object | None = None
        self._exclusions: dict[str, str] = {}
        self._last_catalog_check: datetime | None = None
        self._persistence_error: str | None = None
        self._membership_load_failed = False
        self._catalog_error: str | None = None
        self._book_error: str | None = None
        self._last_refresh_at: str | None = None
        self._published_snapshot: dict[str, object] = {
            "status": "UNKNOWN",
            "generation": None,
            "generation_fingerprint": None,
            "pool_limit": self._pool_limit,
            "token_limit": self._token_limit,
            "pool_count": 0,
            "token_count": 0,
            "members": [],
            "latest": [],
            "results": [],
            "coverage": {
                "latest": None,
                "pool": 0,
                "pool_count": 0,
                "pool_limit": self._pool_limit,
                "capacity": f"0/{self._pool_limit}",
                "waiting_count": None,
                "pending_preparation_count": None,
                "excluded_count": None,
                "native_count": None,
                "three_way_count": None,
                "subscribed_tokens": 0,
                "all_leg_subscribed_count": 0,
                "fresh": 0,
                "fresh_count": None,
                "computed_count": None,
                "blocked_count": None,
                "positive": 0,
                "positive_count": None,
                "non_positive": 0,
                "non_positive_count": None,
                "source_scope": None,
                "generation": None,
                "updated_at": None,
                "status": "UNKNOWN",
                "status_reason": "NO_OBSERVATION_SNAPSHOT",
                "catalog_error": None,
                "persistence_error": None,
                "book_error": None,
                "exclusions": {},
            },
            "last_refresh_at": None,
        }
        try:
            loaded = store.load_observation_pool_members()
        except Exception as exc:
            loaded = {}
            self._membership_load_failed = True
            self._persistence_error = f"load observation membership failed: {exc}"
        self._membership = {
            str(identity): dict(member)
            for identity, member in loaded.items()
            if isinstance(member, Mapping)
        }

    @property
    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.thread_alive:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="prediction-observation-monitor",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lock:
            if thread is None or not thread.is_alive():
                self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_internal(force_catalog=False)
            except Exception:
                logger.exception("prediction observation refresh failed")
            self._stop.wait(1.0)

    def refresh_once(self) -> dict[str, object]:
        """Refresh candidates and quotes once, returning a copied snapshot."""

        with self._refresh_lock:
            self._refresh_internal(force_catalog=True)
        return self.snapshot()

    def _read_catalog(self, *, force_catalog: bool) -> tuple[dict[str, object], dict[str, object]] | None:
        now = self._clock()
        with self._lock:
            if (
                not force_catalog
                and self._catalog_snapshot is not None
                and self._last_catalog_check is not None
                and (now - self._last_catalog_check).total_seconds() < OBSERVATION_CATALOG_SECONDS
            ):
                try:
                    return _snapshot_rows(self._catalog_snapshot)
                except ValueError:
                    return None
        try:
            source = self._candidate_source
            assert source is not None
            snapshot = source()
            metadata, rows = _snapshot_rows(snapshot)
        except Exception as exc:
            with self._lock:
                self._catalog_error = str(exc)
                self._last_catalog_check = now
            return None
        with self._lock:
            self._catalog_snapshot = copy.deepcopy(snapshot)
            self._catalog_metadata = metadata
            self._last_catalog_check = now
            self._catalog_error = None
        return metadata, rows

    def _invalidate_published_catalog_result(self, detail: str) -> None:
        """Keep historical economics while removing their current validity."""

        with self._lock:
            snapshot = copy.deepcopy(self._published_snapshot)
            snapshot["status"] = "ERROR"
            snapshot["status_reason"] = "CATALOG_UNAVAILABLE"
            snapshot["results"] = []
            for collection_name in ("members", "latest"):
                for item in snapshot.get(collection_name, ()):
                    if not isinstance(item, dict):
                        continue
                    previous = item.get("result")
                    stage = str(item.get("stage") or "").upper()
                    reserved = collection_name == "members" or stage in {
                        "OBSERVING",
                        "IN_POOL",
                        "POOL",
                    }
                    if not isinstance(previous, Mapping) and not reserved:
                        continue
                    result = dict(previous) if isinstance(previous, Mapping) else {}
                    result.update(
                        {
                            "status": "BLOCKED",
                            "reason": "CATALOG_UNAVAILABLE",
                            "detail": detail,
                            "current": False,
                            "order_ready": False,
                            "execution_calls": 0,
                        }
                    )
                    item["result"] = result
            coverage = snapshot.get("coverage")
            if isinstance(coverage, dict):
                from .prediction_n_leg_read_model import project_observation_coverage

                raw_latest = snapshot.get("latest")
                latest = (
                    raw_latest
                    if isinstance(raw_latest, (Mapping, list, tuple))
                    else []
                )
                raw_members = snapshot.get("members")
                members = raw_members if isinstance(raw_members, list) else []
                pool_limit = snapshot.get("pool_limit")
                if type(pool_limit) is not int:
                    pool_limit = self._pool_limit
                reserved_pool_count = snapshot.get("pool_count")
                if type(reserved_pool_count) is not int:
                    reserved_pool_count = len(members)
                projected = project_observation_coverage(
                    latest=latest,
                    pool_limit=pool_limit,
                    subscription={
                        "subscribed_tokens": coverage.get("subscribed_tokens"),
                        "all_leg_subscribed_count": coverage.get(
                            "all_leg_subscribed_count"
                        ),
                        "pending_preparation_count": coverage.get(
                            "pending_preparation_count"
                        ),
                    },
                    reserved_pool_count=reserved_pool_count,
                    pool_members=members,
                )
                coverage.update(projected)
                coverage["status"] = "ERROR"
                coverage["status_reason"] = "CATALOG_UNAVAILABLE"
                coverage["catalog_error"] = detail
            snapshot["last_refresh_at"] = self._clock().astimezone(UTC).isoformat()
            self._published_snapshot = snapshot

    def _decode_candidate(
        self, identity: str, row: Mapping[str, object]
    ) -> tuple[dict[str, object] | None, str | None]:
        lifecycle = _status(row.get("lifecycle") or row.get("status"))
        approval = _status(row.get("approval_status") or row.get("status"))
        activation = _status(row.get("activation_status") or row.get("activation"))
        if lifecycle in _FORMAL_EXCLUDED_STATUSES or approval in _FORMAL_EXCLUDED_STATUSES:
            return None, f"LIFECYCLE_{lifecycle or approval}"
        if activation == "SUPERSEDED":
            return None, "SUPERSEDED"
        try:
            end_date = _candidate_end_date(row)
            tokens = _candidate_tokens(row)
        except (TypeError, ValueError) as exc:
            return None, "INVALID_END_DATE" if "date" in str(exc) else "UNSUPPORTED_STRUCTURE"
        relation_type = str(
            row.get("relation_type")
            or (
                "EXACTLY_ONE"
                if isinstance(_mapping(row.get("model")), Mapping)
                and _mapping(row.get("model")).get("template") == PAPER_THREE_WAY_TEMPLATE
                else ""
            )
        ).strip()
        model = _mapping(row.get("model"))
        if relation_type == "NATIVE_COMPLEMENT":
            if model is None:
                return None, "UNSUPPORTED_STRUCTURE"
        elif not (model and model.get("template") == PAPER_THREE_WAY_TEMPLATE):
            return None, "UNSUPPORTED_STRUCTURE"
        source_status, source_reason = _candidate_source_status(row, self._monitor)
        if source_status in _TERMINAL_SOURCE_STATUSES:
            return None, source_reason
        source_metadata, metadata_reason = _candidate_source_metadata(row, self._monitor)
        if metadata_reason == "SOURCE_TERMINAL":
            return None, "SOURCE_TERMINAL"
        if metadata_reason is not None:
            source_status = source_status or "UNKNOWN"
            source_reason = metadata_reason
        version_id = str(row.get("version_id") or identity)
        version_fingerprint = str(
            row.get("version_fingerprint") or _fingerprint({"version_id": version_id, "row": row})
        )
        rules_fingerprint = _fingerprint(
            {
                "relation_type": relation_type,
                "model": model,
                "endpoints": row.get("endpoints"),
            }
        )
        return {
            "identity": identity,
            "row": copy.deepcopy(dict(row)),
            "relation_type": relation_type,
            "version_id": version_id,
            "version_fingerprint": version_fingerprint,
            "rules_fingerprint": rules_fingerprint,
            "entered_at": None,
            "end_date": end_date,
            "tokens": tokens,
            "approval_status": approval or "UNKNOWN",
            "lifecycle": lifecycle or "UNKNOWN",
            # Preserve a candidate while its external lifecycle is unknown so
            # the next bounded source pass can retry the same subscription.
            "source_status": source_status or "OPEN",
            "source_reason": source_reason,
            "source_metadata": source_metadata,
        }, None

    def _persist_membership(
        self, proposed: dict[str, dict[str, str]], previous: dict[str, dict[str, str]]
    ) -> bool:
        if proposed == previous:
            return True
        try:
            self._store.save_observation_pool_members(proposed)
        except Exception as exc:
            with self._lock:
                self._persistence_error = f"save observation membership failed: {exc}"
            return False
        with self._lock:
            self._persistence_error = None
        return True

    def _refresh_internal(self, *, force_catalog: bool) -> None:
        now = self._clock()
        if self._membership_load_failed:
            try:
                loaded = self._store.load_observation_pool_members()
            except Exception as exc:
                error = f"load observation membership failed: {exc}"
                with self._lock:
                    self._persistence_error = error
                    self._last_refresh_at = now.isoformat()
                    self._published_snapshot = copy.deepcopy(self._published_snapshot)
                    self._published_snapshot["status"] = "ERROR"
                    self._published_snapshot["status_reason"] = "PERSISTENCE_ERROR"
                    coverage = self._published_snapshot.get("coverage")
                    if isinstance(coverage, dict):
                        coverage["status"] = "ERROR"
                        coverage["status_reason"] = "PERSISTENCE_ERROR"
                        coverage["persistence_error"] = error
                return
            with self._lock:
                self._membership = {
                    str(identity): dict(member)
                    for identity, member in loaded.items()
                    if isinstance(member, Mapping)
                }
                self._membership_load_failed = False
                self._persistence_error = None
        read = self._read_catalog(force_catalog=force_catalog)
        if read is None:
            detail = self._catalog_error or "catalog snapshot unavailable"
            self._invalidate_published_catalog_result(detail)
            with self._lock:
                self._last_refresh_at = now.isoformat()
                coverage = self._published_snapshot.get("coverage")
                if isinstance(coverage, dict):
                    coverage["catalog_error"] = self._catalog_error
                    coverage["persistence_error"] = self._persistence_error
            return
        metadata, rows = read
        candidates: dict[str, dict[str, object]] = {}
        exclusions: dict[str, str] = {}
        for identity, row in rows.items():
            if not isinstance(row, Mapping):
                exclusions[identity] = "INVALID_ROW"
                continue
            candidate, reason = self._decode_candidate(identity, row)
            if candidate is None:
                exclusions[identity] = reason or "EXCLUDED"
                continue
            candidates[identity] = candidate

        with self._lock:
            previous_membership = copy.deepcopy(self._membership)
            proposed = copy.deepcopy(previous_membership)
            states = self._states
        # Existing members retain their slots until an explicit terminal or
        # invalid source status is observed.  A missing latest row is unknown,
        # not a resolution signal, so it also retains the slot.
        for identity, member in list(proposed.items()):
            candidate = candidates.get(identity)
            if candidate is None:
                reason = exclusions.get(identity)
                # A present row that is explicitly terminal, revoked, or no
                # longer has a supported shape releases its reserved slot.
                # An absent row has no exclusion entry and therefore remains
                # retained for a later bounded retry.
                if reason is not None and (
                    reason == "SOURCE_TERMINAL"
                    or reason in {
                        "INVALID_ROW",
                        "INVALID_END_DATE",
                        "SUPERSEDED",
                        "UNSUPPORTED_STRUCTURE",
                    }
                    or reason.startswith("LIFECYCLE_")
                ):
                    proposed.pop(identity, None)
                continue
            member["relation_type"] = str(candidate["relation_type"])
            member["version_id"] = str(candidate["version_id"])
            member["version_fingerprint"] = str(candidate["version_fingerprint"])
            member["rules_fingerprint"] = str(candidate["rules_fingerprint"])

        occupied = set(proposed)
        if len(occupied) < self._pool_limit:
            for identity, candidate in sorted(
                candidates.items(), key=lambda item: (item[1]["end_date"], item[0])
            ):
                if identity in occupied:
                    continue
                if len(occupied) >= self._pool_limit:
                    break
                proposed[identity] = {
                    "identity": identity,
                    "relation_type": str(candidate["relation_type"]),
                    "version_id": str(candidate["version_id"]),
                    "version_fingerprint": str(candidate["version_fingerprint"]),
                    "rules_fingerprint": str(candidate["rules_fingerprint"]),
                    "entered_at": now.isoformat(),
                }
                occupied.add(identity)

        if not self._persist_membership(proposed, previous_membership):
            proposed = previous_membership

        with self._lock:
            self._membership = proposed
            self._catalog_metadata = copy.deepcopy(metadata)
            self._exclusions = exclusions
            self._catalog_error = None
            self._last_refresh_at = now.isoformat()

        # Rebind current rows after membership persistence.  Candidate changes
        # invalidate the prior result before any new quote can be published.
        member_candidates: dict[str, dict[str, object]] = {}
        for identity, member in proposed.items():
            candidate = candidates.get(identity)
            member_candidates[identity] = candidate if candidate is not None else {}
            state = states.setdefault(identity, {})
            changed = (
                state.get("version_id") != member.get("version_id")
                or state.get("version_fingerprint") != member.get("version_fingerprint")
                or state.get("rules_fingerprint") != member.get("rules_fingerprint")
            )
            if changed:
                state.clear()
                state.update(
                    {
                        "version_id": member.get("version_id"),
                        "version_fingerprint": member.get("version_fingerprint"),
                        "rules_fingerprint": member.get("rules_fingerprint"),
                        "result": None,
                        "last_success_at": None,
                        "last_attempt_at": None,
                        "last_input_fingerprint": None,
                    }
                )

        token_ids = sorted(
            {
                token
                for candidate in member_candidates.values()
                if candidate
                for token in candidate.get("tokens", ())
            }
        )
        self._book_error = None
        if len(token_ids) > self._token_limit:
            self._book_error = "observation token limit exceeded"
            token_ids = token_ids[: self._token_limit]
        setter = getattr(self._monitor, "set_observation_tokens", None)
        condition_setter = getattr(self._monitor, "set_observation_conditions", None)
        condition_ids = sorted(
            {
                condition
                for candidate in member_candidates.values()
                if candidate
                for condition in _source_condition_ids(candidate["row"])
            }
        )
        if callable(setter):
            try:
                setter(token_ids)
            except Exception as exc:
                with self._lock:
                    self._book_error = f"observation subscription update failed: {exc}"
        if callable(condition_setter):
            try:
                condition_setter(condition_ids)
            except Exception as exc:
                with self._lock:
                    self._book_error = f"observation source update failed: {exc}"

        books: Mapping[str, object] = {}
        if token_ids:
            try:
                if callable(self._book_source):
                    fetched = self._book_source(tuple(token_ids))
                else:
                    reader = getattr(self._monitor, "observation_books", None)
                    fetched = reader(tuple(token_ids)) if callable(reader) else None
                if not isinstance(fetched, Mapping):
                    raise ValueError("observation book source returned no mapping")
                books = fetched
            except Exception as exc:
                self._book_error = str(exc)
        oldest_book_at = _oldest_book_timestamp(books)
        if oldest_book_at is not None:
            for identity in proposed:
                states.setdefault(identity, {})["oldest_book_at"] = (
                    oldest_book_at.isoformat()
                )

        # A quote provider may return after the catalog has advanced.  Verify
        # the generation and every member version before publishing the result
        # so a late old quote cannot masquerade as current economics.  The
        # background loop uses the five-second catalog cache; an explicit
        # refresh_once always performs this second lightweight read.
        post_metadata: dict[str, object] | None = None
        post_rows: dict[str, object] = {}
        post_metadata_error: str | None = None
        if not force_catalog and callable(self._catalog_generation_source):
            try:
                raw_meta = self._catalog_generation_source()
                if isinstance(raw_meta, Mapping):
                    post_metadata = {
                        key: copy.deepcopy(raw_meta.get(key))
                        for key in ("generation", "generation_fingerprint", "fingerprint", "latest")
                        if key in raw_meta
                    }
                    if "generation_fingerprint" not in post_metadata:
                        post_metadata["generation_fingerprint"] = post_metadata.get("fingerprint")
                else:
                    post_metadata_error = "catalog watermark returned no mapping"
            except Exception as exc:
                post_metadata_error = f"catalog watermark unavailable: {exc}"
        else:
            post_read = self._read_catalog(force_catalog=force_catalog)
            if post_read is not None:
                post_metadata, post_rows = post_read
            else:
                detail = self._catalog_error or "catalog snapshot unavailable"
                with self._lock:
                    self._catalog_error = detail
                    for identity, member in proposed.items():
                        state = states.setdefault(identity, {})
                        previous = state.get("result")
                        result = dict(previous) if isinstance(previous, Mapping) else {}
                        result.update(
                            {
                                "status": "BLOCKED",
                                "reason": "CATALOG_UNAVAILABLE",
                                "detail": detail,
                                "current": False,
                                "version_id": member.get("version_id"),
                                "identity": identity,
                                "order_ready": False,
                                "execution_calls": 0,
                            }
                        )
                        state["result"] = result
                        state["last_attempt_at"] = now.isoformat()
                self._publish_snapshot(candidates, rows, metadata, exclusions)
                return
        if post_metadata_error is not None:
            with self._lock:
                self._catalog_error = post_metadata_error
                for identity, member in proposed.items():
                    state = states.setdefault(identity, {})
                    previous = state.get("result")
                    result = dict(previous) if isinstance(previous, Mapping) else {}
                    result.update(
                        {
                            "status": "BLOCKED",
                            "reason": "CATALOG_WATERMARK_UNAVAILABLE",
                            "detail": post_metadata_error,
                            "current": False,
                            "version_id": member.get("version_id"),
                            "identity": identity,
                            "order_ready": False,
                            "execution_calls": 0,
                        }
                    )
                    state["result"] = result
                    state["last_attempt_at"] = now.isoformat()
            self._publish_snapshot(candidates, rows, metadata, exclusions)
            return
        if post_metadata is not None:
            with self._lock:
                self._catalog_error = None
            post_candidates: dict[str, dict[str, object]] = {}
            for identity, row in post_rows.items():
                if isinstance(row, Mapping):
                    candidate, _ = self._decode_candidate(identity, row)
                    if candidate is not None:
                        post_candidates[identity] = candidate
            changed = _latest_watermark(post_metadata) != _latest_watermark(metadata)
            # The lightweight watermark does not contain rows.  A changed
            # generation is already sufficient to invalidate every late
            # result; explicit refreshes additionally compare each version.
            changed = changed or (
                bool(post_rows)
                and any(
                (
                    identity not in post_candidates
                    or post_candidates[identity].get("version_fingerprint")
                    != member_candidates[identity].get("version_fingerprint")
                    or post_candidates[identity].get("rules_fingerprint")
                    != member_candidates[identity].get("rules_fingerprint")
                )
                for identity in proposed
                if member_candidates[identity]
                )
            )
            if changed:
                with self._lock:
                    self._catalog_metadata = copy.deepcopy(post_metadata)
                    for identity, member in proposed.items():
                        state = states.setdefault(identity, {})
                        previous = state.get("result")
                        result = dict(previous) if isinstance(previous, Mapping) else {}
                        result.update(
                            {
                                "status": "BLOCKED",
                                "reason": "CATALOG_CHANGED",
                                "detail": "catalog generation changed before result publication",
                                "current": False,
                                "version_id": member.get("version_id"),
                                "identity": identity,
                                "order_ready": False,
                                "execution_calls": 0,
                            }
                        )
                        state["result"] = result
                        state["last_attempt_at"] = now.isoformat()
                self._publish_snapshot(candidates, rows, post_metadata or metadata, exclusions)
                return

        for identity, member in proposed.items():
            candidate = member_candidates[identity]
            state = states.setdefault(identity, {})
            if not candidate:
                previous = state.get("result")
                reason = exclusions.get(identity, "CATALOG_ROW_MISSING")
                if not isinstance(previous, Mapping):
                    previous = {
                        "status": "BLOCKED",
                        "reason": reason,
                        "detail": "latest catalog row is temporarily unavailable",
                        "execution_calls": 0,
                        "order_ready": False,
                    }
                result = dict(previous)
                result.update({"status": "BLOCKED", "reason": reason, "current": False})
                state["result"] = result
                continue
            source_payload = books
            input_fingerprint = _fingerprint(
                {
                    "candidate": {
                        "version": member.get("version_fingerprint"),
                        "rules": member.get("rules_fingerprint"),
                        "source_status": candidate.get("source_status"),
                        "source_reason": candidate.get("source_reason"),
                        "source_metadata": candidate.get("source_metadata"),
                    },
                    "books": _book_input_payload(source_payload),
                }
            )
            last_attempt = state.get("last_attempt_at")
            last_time = None
            if isinstance(last_attempt, str):
                try:
                    last_time = _utc(last_attempt)
                except ValueError:
                    last_time = None
            prior_result = state.get("result")
            dirty_recovery = isinstance(prior_result, Mapping) and prior_result.get(
                "current"
            ) is not True
            if (
                prior_result is not None
                and last_time is not None
                and (now - last_time).total_seconds() < OBSERVATION_RECALC_SECONDS
                and not dirty_recovery
            ):
                # Quote values may arrive more often than the paper pricing
                # budget.  Keep the last published result until this member's
                # two-second window expires; a later pass will evaluate the
                # newest books and source fingerprint.
                continue
            if state.get("result") is not None and state.get("last_input_fingerprint") == input_fingerprint:
                result = state.get("result")
                should_expire = (
                    isinstance(result, Mapping)
                    and result.get("status") == "PASS"
                    and _books_expired(source_payload, now)
                )
                if not should_expire:
                    # A stable input is dirty only when its value changes or a
                    # previously successful quote crosses the freshness edge.
                    continue
            state["last_attempt_at"] = now.isoformat()
            state["last_input_fingerprint"] = input_fingerprint
            if candidate.get("source_reason") == "SOURCE_UNKNOWN":
                result: dict[str, object] = {
                    "status": "BLOCKED",
                    "reason": "SOURCE_UNKNOWN",
                    "detail": "source lifecycle is not explicitly OPEN",
                    "order_ready": False,
                    "execution_calls": 0,
                }
            elif self._book_error:
                result: dict[str, object] = {
                    "status": "BLOCKED",
                    "reason": "MISSING_BOOKS",
                    "detail": self._book_error,
                    "order_ready": False,
                    "execution_calls": 0,
                }
            else:
                try:
                    pricing_row, source_fact_reason = _row_with_source_facts(candidate)
                    if source_fact_reason is not None:
                        result = {
                            "status": "BLOCKED",
                            "reason": source_fact_reason,
                            "detail": "official source rules differ from the catalog snapshot",
                            "order_ready": False,
                            "execution_calls": 0,
                        }
                    else:
                        result = price_observation(
                            pricing_row, books, as_of=now
                        )
                except Exception as exc:
                    result = {
                        "status": "BLOCKED",
                        "reason": "OBSERVATION_ERROR",
                        "detail": str(exc),
                        "order_ready": False,
                        "execution_calls": 0,
                    }
            if result.get("status") != "PASS" and isinstance(prior_result, Mapping):
                for field in _HISTORICAL_RESULT_FIELDS:
                    if field not in result and field in prior_result:
                        result[field] = copy.deepcopy(prior_result[field])
            prior_success = state.get("last_success_at")
            if result.get("status") == "PASS":
                state["last_success_at"] = now.isoformat()
                result["last_success_at"] = state["last_success_at"]
            elif prior_success is not None:
                result["last_success_at"] = prior_success
            result["last_attempt_at"] = now.isoformat()
            result["current"] = result.get("status") == "PASS"
            result["version_id"] = member.get("version_id")
            result["identity"] = identity
            state["result"] = result

        # Drop state for retired members only after the durable membership has
        # committed, so a failed persistence write cannot erase their result.
        with self._lock:
            for identity in list(self._states):
                if identity not in proposed:
                    self._states.pop(identity, None)

        self._publish_snapshot(candidates, rows, metadata, exclusions)

    def _publish_snapshot(
        self,
        candidates: Mapping[str, Mapping[str, object]],
        rows: Mapping[str, object],
        metadata: Mapping[str, object],
        exclusions: Mapping[str, str],
    ) -> None:
        """Build the immutable HTTP-facing snapshot during refresh only."""

        with self._lock:
            observation_now = self._clock().astimezone(UTC)
            members: list[dict[str, object]] = []
            all_tokens: set[str] = set()
            for identity, member in sorted(self._membership.items()):
                state = self._states.get(identity, {})
                candidate: Mapping[str, object] = candidates.get(identity, {})
                tokens = tuple(candidate.get("tokens", ()))
                all_tokens.update(tokens)
                result = copy.deepcopy(state.get("result"))
                row = candidate.get("row")
                display_fields = (
                    _display_fields(row, identity=identity, candidate=candidate)
                    if isinstance(row, Mapping)
                    else {}
                )
                if isinstance(candidate.get("end_date"), datetime):
                    display_fields["overdue"] = (
                        candidate["end_date"].astimezone(UTC) < observation_now
                    )
                members.append(
                    {
                        **copy.deepcopy(member),
                        **display_fields,
                        "end_date": (
                            candidate["end_date"].isoformat()
                            if isinstance(candidate.get("end_date"), datetime)
                            else None
                        ),
                        "approval_status": candidate.get("approval_status", "UNKNOWN"),
                        "lifecycle": candidate.get("lifecycle", "UNKNOWN"),
                        "tokens": list(tokens),
                        "token_count": len(tokens),
                        "oldest_book_at": state.get("oldest_book_at"),
                        "stage": "OBSERVING" if result is not None else "PENDING_QUOTE",
                        "result": result,
                    }
                )
            latest: list[dict[str, object]] = []
            for identity, raw in sorted(rows.items()):
                candidate = candidates.get(identity)
                if candidate is not None:
                    stage = "OBSERVING" if identity in self._membership else "WAITING"
                    row = candidate.get("row")
                    display_fields = (
                        _display_fields(row, identity=identity, candidate=candidate)
                        if isinstance(row, Mapping)
                        else {}
                    )
                    if isinstance(candidate.get("end_date"), datetime):
                        display_fields["overdue"] = (
                            candidate["end_date"].astimezone(UTC) < observation_now
                        )
                    item: dict[str, object] = {
                        "identity": identity,
                        "stage": stage,
                        **display_fields,
                        "relation_type": candidate.get("relation_type"),
                        "version_id": candidate.get("version_id"),
                        "tokens": list(candidate.get("tokens", ())),
                        "end_date": (
                            candidate["end_date"].isoformat()
                            if isinstance(candidate.get("end_date"), datetime)
                            else None
                        ),
                    }
                    if identity in self._states:
                        item["result"] = copy.deepcopy(
                            self._states[identity].get("result")
                        )
                        item["oldest_book_at"] = self._states[identity].get(
                            "oldest_book_at"
                        )
                    latest.append(item)
                else:
                    raw_row = raw if isinstance(raw, Mapping) else {}
                    latest.append(
                        {
                            "identity": identity,
                            "stage": "EXCLUDED",
                            **_display_fields(raw_row, identity=identity),
                            "reason": exclusions.get(identity, "EXCLUDED"),
                        }
                    )
            from .prediction_n_leg_read_model import project_observation_coverage

            subscription_snapshot: Mapping[str, object] | None = None
            subscription_reader = getattr(
                self._monitor, "observation_subscription_snapshot", None
            )
            if callable(subscription_reader):
                try:
                    raw_subscription = subscription_reader()
                except Exception:
                    raw_subscription = None
                if isinstance(raw_subscription, Mapping):
                    subscription_snapshot = raw_subscription
            coverage_projection = project_observation_coverage(
                latest=latest,
                pool_limit=self._pool_limit,
                subscription=subscription_snapshot,
                reserved_pool_count=len(members),
                pool_members=members,
            )
            ranked = sorted(
                [member for member in members if isinstance(member.get("result"), Mapping)],
                key=lambda member: (
                    0
                    if isinstance(member.get("result"), Mapping)
                    and member["result"].get("current") is True
                    else 1,
                    -float(
                        member["result"].get("net_roi")
                        if isinstance(member.get("result"), Mapping)
                        and member["result"].get("net_roi") is not None
                        else -1e100
                    ),
                    member.get("end_date") or "",
                    member.get("identity") or "",
                ),
            )
            source_scopes = sorted(
                {
                    str(row.get("source_scope"))
                    for row in latest
                    if isinstance(row, Mapping)
                    and row.get("source_scope") not in (None, "")
                }
            )
            has_source_unknown = any(
                candidate.get("source_reason") == "SOURCE_UNKNOWN"
                for candidate in candidates.values()
            ) or any(
                isinstance(member.get("result"), Mapping)
                and member["result"].get("reason") == "SOURCE_UNKNOWN"
                for member in members
            )
            current_results = [
                member.get("result")
                for member in members
                if isinstance(member.get("result"), Mapping)
            ]
            has_current = any(
                isinstance(result, Mapping) and result.get("current") is True
                for result in current_results
            )
            all_stale = bool(current_results) and not has_current and all(
                isinstance(result, Mapping)
                and result.get("reason") in {"STALE_BOOK", "FUTURE_BOOK"}
                for result in current_results
            )
            if self._catalog_error or self._persistence_error or self._book_error:
                observation_status = "ERROR"
                status_reason = (
                    self._catalog_error
                    or self._persistence_error
                    or self._book_error
                )
            elif has_source_unknown:
                observation_status = "UNKNOWN"
                status_reason = "SOURCE_UNKNOWN"
            elif all_stale:
                observation_status = "STALE"
                status_reason = "STALE_BOOK"
            elif not rows and members:
                observation_status = "UNKNOWN"
                status_reason = "CATALOG_ROW_MISSING"
            elif not candidates:
                observation_status = "EMPTY"
                status_reason = "NO_CANDIDATES"
            else:
                observation_status = "READY"
                status_reason = None
            self._published_snapshot = {
                "status": observation_status,
                "status_reason": status_reason,
                "generation": self._catalog_metadata.get("generation"),
                "generation_fingerprint": self._catalog_metadata.get("generation_fingerprint"),
                "pool_limit": self._pool_limit,
                "token_limit": self._token_limit,
                "pool_count": len(members),
                "token_count": len(all_tokens),
                "members": members,
                "latest": latest,
                "results": ranked,
                "coverage": {
                    **coverage_projection,
                    "status": observation_status,
                    "status_reason": status_reason,
                    "source_scope": " · ".join(source_scopes) if source_scopes else None,
                    "generation": self._catalog_metadata.get("generation"),
                    "updated_at": self._last_refresh_at,
                    "subscribed_tokens": coverage_projection.get(
                        "subscribed_tokens"
                    ),
                    "all_leg_subscribed_count": coverage_projection.get(
                        "all_leg_subscribed_count"
                    ),
                    "pending_preparation_count": coverage_projection.get(
                        "pending_preparation_count"
                    ),
                    "fresh": coverage_projection.get("fresh_count", 0),
                    "positive": coverage_projection.get("positive_count", 0),
                    "non_positive": coverage_projection.get("non_positive_count", 0),
                    "catalog_error": self._catalog_error,
                    "persistence_error": self._persistence_error,
                    "book_error": self._book_error,
                    "exclusions": copy.deepcopy(self._exclusions),
                },
                "last_refresh_at": self._last_refresh_at,
            }

    def snapshot(self) -> dict[str, object]:
        """Return the last published immutable snapshot without evaluation."""

        with self._lock:
            return copy.deepcopy(self._published_snapshot)
